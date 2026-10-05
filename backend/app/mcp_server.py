"""Graphbase as an MCP server (streamable HTTP), for agents.

    python -m app.mcp_server            # serves http://MCP_HOST:MCP_PORT/mcp

Every request must carry a Keycloak access token (see app/mcp_auth.py). The token's user is the Graphbase
user, and each tool goes through kb.require_access, so an agent sees exactly the knowledge bases its user
owns or was granted in the web app, and nothing else. Graph queries are read-only and scoped to the KB
(GraphStore.run_readonly), the same safety path as the chat screen.

Tools:
  whoami                   the user the token belongs to
  list_knowledge_bases     KBs the user can use (graph and RAG), with role and status
  describe_knowledge_base  graph schema (node types, relationships, PII properties) or RAG documents
  ask_knowledge_base       natural-language question, answered from the KB (graph: Cypher; RAG: passages)
  query_graph              run a read-only Cypher query on a graph KB
  search_documents         semantic search over a RAG KB, returns the matching passages
"""

import logging
from typing import Annotated, Any

import anyio
from fastapi import HTTPException
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from app import chat, extraction, kb, rag
from app.auth import CurrentUser, keycloak_issuer
from app.config import Settings, get_settings
from app.db import open_pool, run_migrations
from app.graphstore import GraphStore, UnsafeQueryError
from app.mcp_auth import KeycloakTokenVerifier, required_scopes, user_for_claims

log = logging.getLogger("graphbase.mcp")

INSTRUCTIONS = """Graphbase knowledge bases: knowledge graphs (Neo4j) built from spreadsheets and RAG stores
built from documents. You only see the knowledge bases the signed-in user owns or was given access to.
Start with list_knowledge_bases. For a graph, describe_knowledge_base shows its node types and relationships;
use ask_knowledge_base for questions in plain language, or query_graph for exact read-only Cypher. For a RAG
store, use search_documents to read the relevant passages, or ask_knowledge_base for a written answer.
Properties marked as PII hold personal data: only return them when the user's question needs them."""


# ------------------------------------------------------------------ helpers
def _claims() -> dict:
    token = get_access_token()
    claims = getattr(token, "claims", None)
    if not claims:
        raise ToolError("Not authenticated")
    return claims


def _user(claims: dict) -> CurrentUser:
    try:
        return user_for_claims(claims)
    except PermissionError as exc:
        raise ToolError(str(exc)) from None


def _kb(user: CurrentUser, kb_name: str, kind: str | None = None, ready: bool = False) -> dict:
    try:
        cat = kb.require_access(user, kb_name)
    except HTTPException as exc:
        raise ToolError(str(exc.detail)) from None
    if kind and cat["kb_type"] != kind:
        other = "search_documents or ask_knowledge_base" if cat["kb_type"] == "rag" else "query_graph"
        raise ToolError(f"{kb_name} is a {'RAG store' if cat['kb_type'] == 'rag' else 'knowledge graph'}; use {other}")
    if ready and cat["status"] != "ready":
        raise ToolError(f"{kb_name} is {cat['status'].replace('_', ' ')}, not ready yet")
    return cat


async def _call(tool: str, fn, *args) -> Any:
    """Run a tool body (blocking DB/graph/LLM calls) in a worker thread, as the token's user."""
    claims = _claims()

    def body():
        user = _user(claims)
        log.info("MCP %s by %s %s", tool, user.user_id, args[:1] if args else "")
        return fn(user, *args)

    return await anyio.to_thread.run_sync(body)


def _iso(v):
    return v.isoformat() if hasattr(v, "isoformat") else v


# ------------------------------------------------------------------ tool bodies (sync, testable directly)
def whoami(user: CurrentUser) -> dict:
    return {"user_id": user.user_id, "display_name": user.display_name, "email": user.email}


def list_knowledge_bases(user: CurrentUser) -> dict:
    return {
        "knowledge_bases": [
            {
                "kb_name": r["kb_name"],
                "kb_type": r["kb_type"],
                "domain": r["domain"],
                "sub_domain": r["sub_domain"],
                "role": r["role"],
                "status": r["status"],
                "owner": r["owner_id"],
                "updated_at": _iso(r["updated_at"]),
            }
            for r in kb.list_for_user(user.user_id)
        ]
    }


def describe_knowledge_base(user: CurrentUser, kb_name: str) -> dict:
    cat = _kb(user, kb_name)
    out = {k: cat[k] for k in ("kb_name", "kb_type", "domain", "sub_domain", "status", "role")}
    if cat["kb_type"] == "graph":
        schema = cat["approved_schema"] or cat["draft_schema"]
        if not schema:
            return {**out, "note": "The graph has not been extracted yet."}
        pii = {
            (t["node_label"], t["property_name"]): t["category"]
            for t in extraction.pii_targets({**schema, "pii": extraction.active_pii(schema)})
        }

        def props(owner, items):
            out = []
            for p in items:
                item = {"name": p["name"], "type": p.get("type", "string")}
                if (owner, p["name"]) in pii:
                    item["pii"] = pii[(owner, p["name"])]
                out.append(item)
            return out

        out["node_types"] = [
            {
                "label": n["label"],
                "key": n["key"]["name"],
                "properties": props(n["label"], [{"name": n["key"]["name"]}] + n.get("properties", [])),
            }
            for n in schema["nodes"]
        ]
        out["relationship_types"] = [
            {
                "from": r["from"]["label"],
                "type": r["type"],
                "to": r["to"]["label"],
                "properties": props(r["type"], r.get("properties", [])),
            }
            for r in schema["relationships"]
        ]
        if cat["status"] == "ready":
            try:
                counts = GraphStore(kb_name).counts()
                out["counts"] = {"nodes": counts["nodes"], "relationships": counts["relationships"]}
            except Exception as exc:  # the schema is still useful without counts
                log.warning("counts unavailable for %s: %s", kb_name, exc)
    else:
        docs = rag.documents(kb_name) if cat["status"] in ("ready", "ingesting") else {}
        out["documents"] = [{"name": d, "chunks": n} for d, n in sorted(docs.items())]
    return out


def ask_knowledge_base(user: CurrentUser, kb_name: str, question: str) -> dict:
    cat = _kb(user, kb_name, ready=True)
    if cat["kb_type"] == "graph":
        r = chat.graph_answer(GraphStore(kb_name), cat["approved_schema"], cat["approved_at"], question, [])
        return {"answer": r["answer"], "cypher": r["cypher"], "rows": r["rows"], "row_count": r["row_count"]}
    r = chat.rag_answer(kb_name, question, [])
    return {"answer": r["answer"], "sources": r["sources"]}


def query_graph(user: CurrentUser, kb_name: str, cypher: str, limit: int) -> dict:
    _kb(user, kb_name, kind="graph", ready=True)
    cap = max(1, min(int(limit), get_settings().mcp_max_rows))
    try:
        _, rows = GraphStore(kb_name).run_readonly(cypher.strip().rstrip(";"), limit=cap)
    except UnsafeQueryError as exc:
        raise ToolError(f"Refused: {exc}") from None
    except Exception as exc:  # syntax errors, unknown functions, timeouts
        raise ToolError(f"Cypher failed: {type(exc).__name__}: {str(exc)[:300]}") from None
    rows = chat._json_safe(rows)
    return {"rows": rows, "row_count": len(rows), "truncated": len(rows) >= cap}


def search_documents(user: CurrentUser, kb_name: str, query: str, k: int) -> dict:
    _kb(user, kb_name, kind="rag", ready=True)
    passages = rag.retrieve(kb_name, query, k=max(1, min(int(k), 20)))
    return {"passages": passages}


# ------------------------------------------------------------------ server
def build_server(s: Settings | None = None) -> FastMCP:
    s = s or get_settings()
    if not (s.keycloak_url and s.keycloak_realm):
        raise SystemExit("The MCP server needs KEYCLOAK_URL and KEYCLOAK_REALM: agents sign in through Keycloak.")
    public = s.mcp_public_url.rstrip("/")
    mcp = FastMCP(
        "graphbase",
        instructions=INSTRUCTIONS,
        token_verifier=KeycloakTokenVerifier(),
        auth=AuthSettings(
            issuer_url=keycloak_issuer(s),
            resource_server_url=f"{public}/mcp",
            required_scopes=required_scopes(s) or None,
        ),
        host=s.mcp_host,
        port=s.mcp_port,
        stateless_http=True,
        json_response=True,
    )

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    @mcp.tool(name="whoami", description="The Graphbase user this session acts as.")
    async def _whoami() -> dict:
        return await _call("whoami", whoami)

    @mcp.tool(name="list_knowledge_bases", description="Knowledge bases (graph and RAG) the user can use.")
    async def _list() -> dict:
        return await _call("list_knowledge_bases", list_knowledge_bases)

    @mcp.tool(
        name="describe_knowledge_base",
        description="A knowledge graph's node types, relationships and PII properties, or a RAG store's documents.",
    )
    async def _describe(kb_name: Annotated[str, Field(description="Name from list_knowledge_bases")]) -> dict:
        return await _call("describe_knowledge_base", describe_knowledge_base, kb_name)

    @mcp.tool(
        name="ask_knowledge_base",
        description="Answer a question in plain language from one knowledge base (graph or RAG).",
    )
    async def _ask(
        kb_name: Annotated[str, Field(description="Name from list_knowledge_bases")],
        question: Annotated[str, Field(description="The question", max_length=2000)],
    ) -> dict:
        return await _call("ask_knowledge_base", ask_knowledge_base, kb_name, question)

    @mcp.tool(
        name="query_graph",
        description="Run a read-only Cypher query on a knowledge graph. Use the labels, relationship types and "
        "properties from describe_knowledge_base. Writes and procedure calls are refused.",
    )
    async def _query(
        kb_name: Annotated[str, Field(description="Name of a knowledge graph")],
        cypher: Annotated[str, Field(description="One read-only Cypher statement", max_length=5000)],
        limit: Annotated[int, Field(description="Maximum rows to return", ge=1, le=1000)] = 50,
    ) -> dict:
        return await _call("query_graph", query_graph, kb_name, cypher, limit)

    @mcp.tool(name="search_documents", description="Semantic search over a RAG store; returns matching passages.")
    async def _search(
        kb_name: Annotated[str, Field(description="Name of a RAG store")],
        query: Annotated[str, Field(description="What to look for", max_length=2000)],
        k: Annotated[int, Field(description="Number of passages", ge=1, le=20)] = 5,
    ) -> dict:
        return await _call("search_documents", search_documents, kb_name, query, k)

    return mcp


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    run_migrations()
    open_pool()
    build_server().run(transport="streamable-http")


if __name__ == "__main__":
    main()
