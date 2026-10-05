"""Chat over a knowledge base.

Graph KBs: a LangGraph flow  generate_cypher -> run (read-only, KB-scoped) -> [repair once] -> answer.
RAG KBs:   retrieve from Chroma -> answer with sources.
Access is checked by the API layer before either runs.
"""

import contextlib
import datetime as dt
import json
import re
from typing import TypedDict

from langgraph.graph import END, StateGraph

from app import graph_schema as gs
from app.cypher_repair import error_hint, lint, repair
from app.graphstore import GraphStore, UnsafeQueryError
from app.llm import ask_json, ask_text
from app.rag import retrieve

MAX_ROWS_TO_LLM = 60


# ------------------------------------------------------------------ graph chat
class GraphState(TypedDict, total=False):
    question: str
    history: list
    schema_text: tuple
    cypher: str
    executed: str
    rows: list
    error: str
    attempts: int
    answer: str


CYPHER_SYSTEM = """You translate questions into Neo4j Cypher for the graph described below.
Rules:
- Use only the node labels, relationship types, directions and properties listed. Never invent any.
- Read-only: MATCH / OPTIONAL MATCH / WHERE / WITH / RETURN / ORDER BY / LIMIT only.
- Compare text case-insensitively: toLower(n.prop) = toLower('value'), or CONTAINS for partial names.
- Return readable values (names, ids, numbers) with clear aliases, not whole nodes.
- For "how many" use count(DISTINCT x). For totals use sum(). Add LIMIT 50 when listing.
- If the question follows up an earlier one ("those", "them"), reuse the earlier query's pattern and filters.
- Write relationship arrows exactly as listed. Relationship properties belong to the relationship variable.
- Write id values in full, in the same format as the example ids listed in the schema (never just the digits).
- When returning an entity, return both its id and its name.
- For a follow-up question, reuse the earlier query's MATCH and filters and add the new condition in WHERE;
  do not copy names or values from the earlier answer into the query.
- For the largest/smallest value use ORDER BY ... DESC/ASC LIMIT 1, never max()/min() inside WHERE.
Reply with JSON: {{"cypher": "..."}}

Graph schema:
{schema}

Examples for this graph:
{examples}"""

ANSWER_SYSTEM = """You answer questions using only the database results given. Be concise and specific: give the
names and numbers from the results. If the results are empty, say that no matching data was found.
Do not mention Cypher or databases."""


def _json_safe(v):
    if isinstance(v, dt.date | dt.datetime):
        return v.isoformat()
    if hasattr(v, "iso_format"):
        return v.iso_format()
    if isinstance(v, dict):
        return {k: _json_safe(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_json_safe(x) for x in v]
    return v


def _history_text(history: list) -> str:
    turns = []
    for h in (history or [])[-3:]:
        line = f"Q: {h.get('question', '')}"
        if h.get("cypher"):
            line += f"\nCypher: {h['cypher']}"
        if h.get("answer"):
            line += f"\nA: {h['answer'][:300]}"
        turns.append(line)
    return "\n\n".join(turns)


def _display(node: dict) -> str:
    names = [p["name"] for p in node.get("properties", []) if p["type"] == "string"]
    return next((n for n in names if "name" in n), names[0] if names else node["key"]["name"])


def examples(schema: dict, hints: dict) -> str:
    """Worked examples built from this knowledge base's own schema (small models copy their shape)."""
    nodes = {n["label"]: n for n in schema["nodes"]}
    out = []
    for r in schema["relationships"]:
        a, b = nodes[r["from"]["label"]], nodes[r["to"]["label"]]
        if a["label"] == b["label"]:
            continue
        text_prop = next(
            ((b["label"], p["name"]) for p in b.get("properties", []) if (b["label"], p["name"]) in hints), None
        )
        if text_prop and len(out) < 1:
            v = hints[text_prop][0]
            out.append(
                f"Q: Which {a['label']} are linked to the {b['label']} with {text_prop[1]} {v}?\n"
                f"Cypher: MATCH (a:{a['label']})-[:{r['type']}]->(b:{b['label']}) "
                f"WHERE toLower(b.{text_prop[1]}) = toLower('{v}') "
                f"RETURN DISTINCT a.{_display(a)} AS {a['label'].lower()} LIMIT 50"
            )
    num_rel = next(
        ((r, p) for r in schema["relationships"] for p in r["properties"] if p["type"] in ("integer", "float")), None
    )
    if num_rel:
        r, p = num_rel
        a, b = nodes[r["from"]["label"]], nodes[r["to"]["label"]]
        key = a["key"]["name"]
        sample = (hints.get((a["label"], key)) or ["X"])[0]
        out.append(
            f"Q: What is the total {p['name']} for {a['label']} {sample}?\n"
            f"Cypher: MATCH (a:{a['label']} {{{key}: '{sample}'}})-[r:{r['type']}]->(b:{b['label']}) "
            f"RETURN sum(r.{p['name']}) AS total_{p['name']}"
        )
        out.append(
            f"Q: Which {a['label']} has the highest total {p['name']}?\n"
            f"Cypher: MATCH (a:{a['label']})-[r:{r['type']}]->(b:{b['label']}) WITH a, sum(r.{p['name']}) AS total "
            f"RETURN a.{_display(a)} AS {a['label'].lower()}, total ORDER BY total DESC LIMIT 1"
        )
    num_node = next(
        ((n, p) for n in schema["nodes"] for p in n.get("properties", []) if p["type"] in ("integer", "float")), None
    )
    if num_node:
        n, p = num_node
        out.append(
            f"Q: Which {n['label']} has the largest {p['name']}?\n"
            f"Cypher: MATCH (n:{n['label']}) RETURN n.{_display(n)} AS {n['label'].lower()}, n.{p['name']} "
            f"ORDER BY n.{p['name']} DESC LIMIT 1"
        )
    first = schema["nodes"][0]
    out.append(f"Q: How many {first['label']} are there?\nCypher: MATCH (n:{first['label']}) RETURN count(n) AS count")
    return "\n\n".join(out)


def build_graph_chat(store: GraphStore, schema: dict):
    def generate(state: GraphState) -> GraphState:
        prompt = ""
        if state.get("history"):
            prompt += f"Earlier in this conversation:\n{_history_text(state['history'])}\n\n"
        prompt += f"Question: {state['question']}"
        if state.get("error"):
            prompt += (
                f"\n\nYour previous query:\n{state['cypher']}\nfailed with: {state['error']}\n"
                "Write a corrected query."
            )
        schema_text, example_text = state["schema_text"]
        data = ask_json(CYPHER_SYSTEM.format(schema=schema_text, examples=example_text), prompt)
        cypher = str(data.get("cypher") or "").strip().rstrip(";")
        # repair is best effort; the query still goes through the safety checks
        with contextlib.suppress(Exception):
            cypher = repair(cypher, schema)  # directions, names, misplaced properties
        return {"cypher": cypher, "attempts": state.get("attempts", 0) + 1, "error": ""}

    def run(state: GraphState) -> GraphState:
        if not state["cypher"]:
            return {"error": "empty query", "rows": []}
        problems = lint(state["cypher"], schema)
        if problems and state.get("attempts", 0) < 2:  # let the model fix schema mistakes before running
            return {"error": "the query does not match the graph schema: " + " ".join(problems), "rows": []}
        try:
            executed, rows = store.run_readonly(state["cypher"])
            return {"executed": executed, "rows": _json_safe(rows), "error": ""}
        except UnsafeQueryError as exc:
            return {"error": f"refused: {exc}", "rows": []}
        except Exception as exc:  # syntax errors, unknown functions, timeouts
            message = f"{type(exc).__name__}: {str(exc)[:300]}"
            hint = error_hint(message)
            return {"error": message + (f" Hint: {hint}" if hint else ""), "rows": []}

    def answer(state: GraphState) -> GraphState:
        if state.get("error"):
            if state["error"].startswith("refused"):
                return {"answer": "I can only read from this knowledge base, so I can't do that."}
            return {
                "answer": "I couldn't turn that question into a working query. Try rephrasing it, "
                "for example by naming the entity you're asking about."
            }
        rows = state.get("rows") or []
        if not rows:
            return {"answer": "No matching data was found in this knowledge graph."}
        text = ask_text(
            ANSWER_SYSTEM,
            f"Question: {state['question']}\n\n"
            f"Results ({len(rows)} rows):\n{json.dumps(rows[:MAX_ROWS_TO_LLM], default=str)}",
        )
        return {"answer": text}

    def route(state: GraphState) -> str:
        err = state.get("error", "")
        if err and not err.startswith("refused") and state.get("attempts", 0) < 2:
            return "generate"
        return "answer"

    g = StateGraph(GraphState)
    g.add_node("generate", generate)
    g.add_node("run", run)
    g.add_node("answer", answer)
    g.set_entry_point("generate")
    g.add_edge("generate", "run")
    g.add_conditional_edges("run", route, {"generate": "generate", "answer": "answer"})
    g.add_edge("answer", END)
    return g.compile()


def value_hints(store: GraphStore, schema: dict, max_distinct: int = 12) -> dict:
    """Low-cardinality text values (cities, statuses) so the model filters on real values,
    plus two example ids per node type so it writes them in full."""
    hints = {}
    for n in schema["nodes"]:
        key = n["key"]["name"]
        rows = store.read_internal(f"MATCH (n:{store.label(n['label'])}) RETURN n.`{key}` AS v LIMIT 2")
        if rows:
            hints[(n["label"], key)] = [r["v"] for r in rows if r["v"] is not None]
        for p in n.get("properties", []):
            if p["type"] != "string":
                continue
            rows = store.read_internal(
                f"MATCH (n:{store.label(n['label'])}) WHERE n.`{p['name']}` IS NOT NULL "
                f"WITH DISTINCT n.`{p['name']}` AS v LIMIT {max_distinct + 1} RETURN v"
            )
            vals = [r["v"] for r in rows if isinstance(r["v"], str) and len(r["v"]) <= 40]
            if 0 < len(vals) <= max_distinct:
                hints[(n["label"], p["name"])] = vals
    return hints


_schema_text_cache: dict[str, tuple] = {}


def schema_text(store: GraphStore, schema: dict, version) -> tuple[str, str]:
    """(schema description, worked examples), cached per KB until the approved schema changes."""
    cached = _schema_text_cache.get(store.kb_name)
    if cached and cached[0] == version:
        return cached[1]
    hints = value_hints(store, schema)
    text = (gs.describe_for_llm(schema, hints), examples(schema, hints))
    _schema_text_cache[store.kb_name] = (version, text)
    return text


def graph_path(cypher: str) -> list[dict]:
    """Chips for the UI: the first MATCH pattern as [node, rel, node, ...]."""
    m = re.search(
        r"MATCH\s+(?:\w+\s*=\s*)?(\(.*?\))(?=\s+(?:WHERE|RETURN|WITH|OPTIONAL|MATCH|ORDER)\b|\s*$)", cypher, re.I | re.S
    )
    if not m:
        return []
    pattern = m.group(1)
    parts = []
    for tok in re.finditer(
        r"\((\w*)\s*(?::\s*`?(\w+)`?)?[^)]*?(\{[^}]*\})?\)|\[\s*\w*\s*:\s*`?(\w+)`?[^\]]*\]", pattern
    ):
        if tok.group(4):
            parts.append({"kind": "rel", "text": tok.group(4)})
        elif tok.group(2):
            text = tok.group(2)
            props = tok.group(3)
            if props:
                vals = re.findall(r":\s*'([^']*)'", props)
                if vals:
                    text += f": {vals[0]}"
            parts.append({"kind": "node", "text": text})
    # a WHERE equality on the last node reads nicely as "Warehouse: Chennai"
    return parts


def graph_answer(store: GraphStore, schema: dict, schema_version, question: str, history: list) -> dict:
    flow = build_graph_chat(store, schema)
    state = flow.invoke(
        {"question": question, "history": history or [], "schema_text": schema_text(store, schema, schema_version)}
    )
    return {
        "answer": state.get("answer", ""),
        "cypher": state.get("cypher", ""),
        "path": graph_path(state.get("cypher", "")),
        "rows": (state.get("rows") or [])[:20],
        "row_count": len(state.get("rows") or []),
        "error": state.get("error") or None,
    }


# ------------------------------------------------------------------ RAG chat
RAG_SYSTEM = """You answer questions using only the numbered context passages from company documents.
- Quote exact figures (days, percentages, times) from the passages.
- If a passage says a rule was superseded or is historical, use the current rule and say so only if useful.
- If the passages don't contain the answer, say you couldn't find it in the documents.
- Cite passages like [1] or [2]. Be concise."""


def rag_answer(kb_name: str, question: str, history: list) -> dict:
    query = question
    if history and len(question.split()) <= 8:  # short follow-ups need the earlier topic to retrieve well
        query = f"{history[-1].get('question', '')} {question}"
    passages = retrieve(kb_name, query, k=6)
    if not passages:
        return {"answer": "This knowledge base has no documents yet.", "sources": []}
    context = "\n\n".join(
        f"[{i}] ({p['source']}{', page ' + str(p['page']) if p['page'] else ''})\n{p['text']}"
        for i, p in enumerate(passages, 1)
    )
    prompt = ""
    if history:
        prompt += f"Earlier in this conversation:\n{_history_text(history)}\n\n"
    prompt += f"Context:\n{context}\n\nQuestion: {question}"
    return {
        "answer": ask_text(RAG_SYSTEM, prompt),
        "sources": [
            {"n": i, "source": p["source"], "page": p["page"], "score": p["score"], "snippet": p["text"][:280]}
            for i, p in enumerate(passages, 1)
        ],
    }
