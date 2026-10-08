"""The MCP server: Keycloak bearer tokens, per-user access to knowledge bases, and the tools.

Runs the real MCP server (streamable HTTP, in a thread) against a stand-in Keycloak realm (real HTTP, real
RS256 tokens), real Postgres and the real TurboQuant store. Neo4j calls go to a stand-in that still applies the real
read-only check and KB scoping, so the safety path is the production one.
"""

import hashlib
import json
import math
import re
from pathlib import Path

import anyio
import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from app import extraction, kb, mcp_server, rag
from app.auth import CurrentUser
from app.cli import main as cli
from app.db import get_conn
from app.graphstore import check_read_only, scope_cypher
from app.tabular import read_table_file

from .fixtures import SAMPLES
from .keycloak_mock import MockKeycloak, ServerThread

GRAPH, DOCS = "t_mcp_graph_kg", "t_mcp_docs_rag"


# ------------------------------------------------------------------ stand-ins
class HashEmbeddings:
    """Deterministic bag-of-words embeddings (no model needed); similar words -> similar vectors."""

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * 256
        for w in re.findall(r"[a-z0-9]+", text.lower()):
            v[int(hashlib.md5(w.encode()).hexdigest(), 16) % 256] += 1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]

    def embed_documents(self, texts):
        return [self._vec(t) for t in texts]

    def embed_query(self, text):
        return self._vec(text)


class FakeGraphStore:
    """Neo4j stand-in that keeps the production safety path: check_read_only + KB scoping."""

    executed: list[str] = []

    def __init__(self, kb_name: str):
        self.kb_name = kb_name

    def run_readonly(self, cypher, params=None, limit=200, timeout=30.0):
        check_read_only(cypher)
        scoped = scope_cypher(cypher, f"KB_{self.kb_name}")
        FakeGraphStore.executed.append(scoped)
        rows = [{"supplier": f"Supplier {i}", "products": 10 - i} for i in range(10)]
        return scoped, rows[:limit]

    def counts(self):
        return {"nodes": {"Supplier": 48}, "relationships": {"HAS_SUPPLIER": 307}}


# ------------------------------------------------------------------ fixtures
@pytest.fixture
def keycloak():
    kc = MockKeycloak(users={"priya.nair": "pw", "meera.s": "pw"})
    with kc.serve():
        yield kc


@pytest.fixture
def world(settings, keycloak, monkeypatch):
    """Users, a ready graph KB and a ready RAG KB owned by priya.nair; meera.s may use the RAG KB only."""
    settings(
        KEYCLOAK_URL=keycloak.base_url,
        KEYCLOAK_INTERNAL_URL=keycloak.base_url,
        KEYCLOAK_REALM="graphbase",
        MCP_AUDIENCE="graphbase-mcp",
    )
    monkeypatch.setattr(rag, "get_embeddings", lambda: HashEmbeddings())
    monkeypatch.setattr(mcp_server, "GraphStore", FakeGraphStore)
    monkeypatch.setattr(extraction, "ask_json", lambda *a, **k: (_ for _ in ()).throw(ValueError("no LLM")))
    cli(["seed-demo-users"])
    priya = CurrentUser("priya.nair", "Priya Nair", None)

    sheets = read_table_file(SAMPLES / "supplier_orders.xlsx")
    schema = extraction.extract(sheets, "supplier_orders.xlsx")
    for p in schema["pii"]:  # a reviewer dismissed one detected column
        if p["column"] == "Contact Email":
            p["status"] = "dismissed"
    kb.create(priya, GRAPH, "graph", "Retail", "Supply chain", f"label:KB_{GRAPH}")
    kb.set_status(GRAPH, "ready", None, approved_schema=json.dumps(schema, default=str), approved_by="priya.nair")
    with get_conn() as conn:
        conn.execute("UPDATE kb_catalog SET approved_at = now() WHERE kb_name = %s", (GRAPH,))

    kb.create(priya, DOCS, "rag", "Retail", "Policies", f"turboquant:{DOCS}")
    rag.drop_index(DOCS)
    for name in ("returns_policy.pdf", "warehouse_sop.txt"):
        rag.store_chunks(DOCS, name, rag.chunk(rag.extract_text(Path(SAMPLES / name), name)))
    kb.set_status(DOCS, "ready")
    kb.grant(priya, DOCS, "meera.s")
    yield {"schema": schema}
    rag.drop_index(DOCS)


@pytest.fixture
def mcp_url(world):
    with ServerThread(mcp_server.build_server().streamable_http_app()) as srv:
        yield srv.url


# ------------------------------------------------------------------ client helpers
def call(url: str, token: str, tool: str | None = None, args: dict | None = None):
    async def go():
        headers = {"Authorization": f"Bearer {token}"}
        async with (
            streamablehttp_client(f"{url}/mcp", headers=headers) as (r, w, _),
            ClientSession(r, w) as s,
        ):
            await s.initialize()
            if tool is None:
                return await s.list_tools()
            return await s.call_tool(tool, args or {})

    return anyio.run(go)


def ok(result):
    assert not result.isError, result.content[0].text
    data = result.structuredContent
    if data is None:  # tools returning a plain dict: the JSON is in the text content
        return json.loads(result.content[0].text) if result.content else None
    return data["result"] if set(data) == {"result"} else data


def err(result) -> str:
    assert result.isError, result.structuredContent
    return result.content[0].text


# ------------------------------------------------------------------ authentication
def test_requests_without_a_valid_keycloak_token_get_401(mcp_url, keycloak):
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}},
    }
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    bad = {
        "none": None,
        "garbage": "not-a-jwt",
        "expired": keycloak.mint("priya.nair", exp=1_000_000_000, iat=999_999_000),
        "wrong audience (web app's token)": keycloak.mint("priya.nair", client_id="graphbase-app"),
        "wrong issuer": keycloak.mint("priya.nair", iss="https://evil.example/realms/graphbase"),
        "forged signature": keycloak.mint("priya.nair", key=other_key),
        "id token": keycloak.mint("priya.nair", typ="ID"),
        "no username": keycloak.mint("priya.nair", preferred_username=None),
    }
    for why, token in bad.items():
        h = {**headers, **({"Authorization": f"Bearer {token}"} if token else {})}
        r = httpx.post(f"{mcp_url}/mcp", json=body, headers=h)
        assert r.status_code == 401, why
        assert "resource_metadata" in r.headers.get("www-authenticate", ""), why
    good = httpx.post(
        f"{mcp_url}/mcp", json=body, headers={**headers, "Authorization": f"Bearer {keycloak.mint('priya.nair')}"}
    )
    assert good.status_code == 200


def test_protected_resource_metadata_points_agents_to_keycloak(mcp_url, keycloak):
    meta = httpx.get(f"{mcp_url}/.well-known/oauth-protected-resource/mcp").json()
    assert meta["authorization_servers"] == [keycloak.issuer]
    assert httpx.get(f"{mcp_url}/health").json() == {"ok": True}


def test_required_scope_is_enforced(world, keycloak, settings):
    settings(MCP_REQUIRED_SCOPES="graphbase")
    with ServerThread(mcp_server.build_server().streamable_http_app()) as srv:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {keycloak.mint('priya.nair')}",
        }
        assert httpx.post(f"{srv.url}/mcp", json={}, headers=headers).status_code == 401
        token = keycloak.mint("priya.nair", scope="openid graphbase")
        assert ok(call(srv.url, token, "whoami"))["user_id"] == "priya.nair"


# ------------------------------------------------------------------ per-user access
def test_tools_are_listed(mcp_url, keycloak):
    names = {t.name for t in call(mcp_url, keycloak.mint("priya.nair")).tools}
    assert names == {
        "whoami",
        "list_knowledge_bases",
        "describe_knowledge_base",
        "ask_knowledge_base",
        "query_graph",
        "search_documents",
    }


def test_each_user_sees_only_their_knowledge_bases(mcp_url, keycloak):
    priya = {
        k["kb_name"]: k
        for k in ok(call(mcp_url, keycloak.mint("priya.nair"), "list_knowledge_bases"))["knowledge_bases"]
    }
    assert {GRAPH, DOCS} <= set(priya) and priya[GRAPH]["role"] == "owner"
    meera = ok(call(mcp_url, keycloak.mint("meera.s"), "list_knowledge_bases"))["knowledge_bases"]
    assert [(k["kb_name"], k["role"]) for k in meera] == [(DOCS, "user")]
    assert "do not have access" in err(
        call(mcp_url, keycloak.mint("meera.s"), "describe_knowledge_base", {"kb_name": GRAPH})
    )
    assert "not found" in err(
        call(mcp_url, keycloak.mint("meera.s"), "describe_knowledge_base", {"kb_name": "no_such_kb"})
    )


def test_revoked_access_takes_effect_immediately(mcp_url, keycloak):
    token = keycloak.mint("meera.s")
    assert ok(call(mcp_url, token, "search_documents", {"kb_name": DOCS, "query": "return window"}))["passages"]
    kb.revoke(CurrentUser("priya.nair", "Priya Nair", None), DOCS, "meera.s")
    assert "do not have access" in err(call(mcp_url, token, "search_documents", {"kb_name": DOCS, "query": "x"}))
    assert ok(call(mcp_url, token, "list_knowledge_bases")) == {"knowledge_bases": []}


def test_new_keycloak_user_is_provisioned_without_access(mcp_url, keycloak):
    me = ok(call(mcp_url, keycloak.mint("new.joiner"), "whoami"))
    assert me["user_id"] == "new.joiner"
    assert ok(call(mcp_url, keycloak.mint("new.joiner"), "list_knowledge_bases")) == {"knowledge_bases": []}
    with get_conn() as conn:
        row = conn.execute("SELECT auth_source, modified_by FROM users WHERE user_id = 'new.joiner'").fetchone()
    assert (row["auth_source"], row["modified_by"]) == ("keycloak", "mcp")


def test_disabled_user_is_refused(mcp_url, keycloak):
    cli(["deactivate-user", "meera.s"])
    assert "disabled" in err(call(mcp_url, keycloak.mint("meera.s"), "list_knowledge_bases"))


def test_unknown_user_refused_when_auto_provisioning_is_off(world, keycloak, settings):
    settings(MCP_AUTO_PROVISION_USERS="false")
    with ServerThread(mcp_server.build_server().streamable_http_app()) as srv:
        assert "not a TCS Knowledge Fabric user" in err(call(srv.url, keycloak.mint("stranger"), "whoami"))


# ------------------------------------------------------------------ tools
def test_describe_graph_shows_schema_and_active_pii(mcp_url, keycloak, world):
    d = ok(call(mcp_url, keycloak.mint("priya.nair"), "describe_knowledge_base", {"kb_name": GRAPH}))
    supplier = next(n for n in d["node_types"] if n["label"] == "Supplier")
    props = {p["name"]: p.get("pii") for p in supplier["properties"]}
    assert props["contact_phone"] == "phone"
    assert props["contact_email"] is None  # dismissed by the reviewer: not reported as PII
    assert d["relationship_types"] and d["counts"]["nodes"] == {"Supplier": 48}


def test_query_graph_is_read_only_and_scoped(mcp_url, keycloak):
    token = keycloak.mint("priya.nair")
    FakeGraphStore.executed.clear()
    r = ok(
        call(
            mcp_url,
            token,
            "query_graph",
            {"kb_name": GRAPH, "cypher": "MATCH (s:Supplier) RETURN s.name AS supplier LIMIT 3;", "limit": 4},
        )
    )
    assert r["row_count"] == 4 and r["truncated"] is True
    assert FakeGraphStore.executed == [f"MATCH (s:Supplier:`KB_{GRAPH}`) RETURN s.name AS supplier LIMIT 3"]
    for cypher in (
        "MATCH (n) DETACH DELETE n",
        "CALL db.labels()",
        "MATCH (n:KB_other) RETURN n",
        "MATCH (n WHERE size(keys(n)) > 0) RETURN n",
    ):
        assert "Refused" in err(call(mcp_url, token, "query_graph", {"kb_name": GRAPH, "cypher": cypher})), cypher
    assert "RAG store" in err(call(mcp_url, token, "query_graph", {"kb_name": DOCS, "cypher": "MATCH (n) RETURN n"}))


def test_search_documents_uses_the_real_vector_store(mcp_url, keycloak):
    r = ok(
        call(
            mcp_url,
            keycloak.mint("meera.s"),
            "search_documents",
            {"kb_name": DOCS, "query": "restocking fee electronics", "k": 3},
        )
    )
    assert len(r["passages"]) == 3
    assert any("restocking" in p["text"].lower() for p in r["passages"])
    assert {p["source"] for p in r["passages"]} <= {"returns_policy.pdf", "warehouse_sop.txt"}
    d = ok(call(mcp_url, keycloak.mint("meera.s"), "describe_knowledge_base", {"kb_name": DOCS}))
    assert {x["name"] for x in d["documents"]} == {"returns_policy.pdf", "warehouse_sop.txt"}


def test_ask_knowledge_base_answers_from_retrieved_passages(mcp_url, keycloak, monkeypatch):
    from app import chat

    seen = {}

    def fake_llm(system, prompt):
        seen["prompt"] = prompt
        return "The restocking fee is in passage [1]."

    monkeypatch.setattr(chat, "ask_text", fake_llm)
    r = ok(
        call(
            mcp_url,
            keycloak.mint("meera.s"),
            "ask_knowledge_base",
            {"kb_name": DOCS, "question": "What is the restocking fee for electronics?"},
        )
    )
    assert r["answer"].startswith("The restocking fee") and r["sources"]
    assert "restocking" in seen["prompt"].lower()


def test_kb_not_ready_is_reported(mcp_url, keycloak):
    kb.set_status(GRAPH, "building")
    assert "building, not ready" in err(
        call(mcp_url, keycloak.mint("priya.nair"), "query_graph", {"kb_name": GRAPH, "cypher": "MATCH (n) RETURN n"})
    )
