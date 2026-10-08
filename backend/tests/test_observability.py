"""Langfuse tracing: every user operation is exactly one trace, named after the operation, carrying the user and
session, with every LLM call / retrieval inside it, and it reaches Langfuse on its own (no shutdown needed).

Runs the real Langfuse SDK against a stand-in ingestion endpoint (tests/langfuse_mock.py) and the real app
(FastAPI, background jobs, MCP server) with a scripted chat model.
"""

import concurrent.futures as cf
import re
import time
import uuid

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app import extraction, jobs, llm, observability, rag

from . import test_mcp_server as mcp_tests
from .conftest import login
from .fixtures import SAMPLES
from .keycloak_mock import ServerThread
from .langfuse_mock import MockLangfuse

DOCS = mcp_tests.DOCS
keycloak, world, mcp_url = mcp_tests.keycloak, mcp_tests.world, mcp_tests.mcp_url  # shared fixtures


class ScriptedModel(BaseChatModel):
    """Answers JSON prompts with {} and text prompts with a fixed sentence; reports token usage like Azure."""

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        text = " ".join(str(m.content) for m in messages)
        content = "{}" if "JSON" in text or "json" in text else "Items can be returned within 30 days."
        usage = {"input_tokens": 120, "output_tokens": 12, "total_tokens": 132}
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=content, usage_metadata=usage))])


def _use_scripted_models(monkeypatch):
    monkeypatch.setattr(llm, "get_llm", lambda *a, **k: ScriptedModel())
    monkeypatch.setattr(rag, "get_embeddings", lambda: llm.observed_embeddings(mcp_tests.HashEmbeddings()))


@pytest.fixture
def langfuse(settings, monkeypatch):
    sink = MockLangfuse(public_key=f"pk-lf-{uuid.uuid4().hex[:8]}")
    with ServerThread(sink.app) as srv:
        settings(LANGFUSE_PUBLIC_KEY=sink.public_key, LANGFUSE_SECRET_KEY=sink.secret_key, LANGFUSE_BASE_URL=srv.url)
        _use_scripted_models(monkeypatch)
        yield sink
        observability.flush_langfuse()
    assert sink.errors == []


def wait_for(sink: MockLangfuse, check, timeout: float = 20.0) -> dict:
    """Export is asynchronous (background thread): poll until check(traces) passes. The test never flushes."""
    deadline = time.time() + timeout
    while True:
        traces = sink.traces()
        try:
            check(traces)
            return traces
        except AssertionError:
            if time.time() > deadline:
                raise
            time.sleep(0.2)


def by_name(traces: dict, name: str) -> list[dict]:
    return [t for t in traces.values() if t["name"] == name]


def has(name: str, count: int = 1):
    def check(traces):
        assert len(by_name(traces, name)) >= count, [t["name"] for t in traces.values()]

    return check


def assert_well_formed(t: dict, name: str, user: str):
    assert t["root"] is not None, "root span missing: the trace would show up without name/user"
    assert t["name"] == name and t["user"] == user and t["session"], (t["name"], t["user"], t["session"])
    assert t["names_set_by_children"] == [], f"children renamed the trace: {t['names_set_by_children']}"


# ------------------------------------------------------------------ chat requests
def test_rag_chat_is_one_named_trace_with_user_session_and_children(client, world, langfuse):
    priya = login(client)
    for _ in range(2):  # the second request runs after Langfuse is initialised (FastAPI would trace it)
        r = client.post(f"/api/kbs/{DOCS}/chat", headers=priya, json={"question": "What is the return window?"})
        assert r.status_code == 200, r.text
    for _ in range(5):  # what the UI polls; must never reach Langfuse
        assert client.get("/api/kbs", headers=priya).status_code == 200

    wait_for(langfuse, has("RAG Chat", 2))
    time.sleep(2.5)  # one more export interval: nothing else may arrive
    traces = langfuse.traces()
    assert sorted(t["name"] for t in traces.values()) == ["RAG Chat", "RAG Chat"]
    for t in by_name(traces, "RAG Chat"):
        assert_well_formed(t, "RAG Chat", "priya.nair")
        assert re.fullmatch(r"chat-\d+", t["session"])  # one Langfuse session per saved conversation
        names = {s["name"] for s in t["spans"]}
        assert {"TurboQuant Retrieval", "embedding_query", "llm_text"} <= names, names
        gen = next(s for s in t["spans"] if s["name"] == "llm_text")
        assert langfuse.observation_type(gen) == "generation"
        assert gen["attrs"].get("langfuse.observation.usage_details"), sorted(gen["attrs"])
        assert "30 days" in str(langfuse.payload(gen, "langfuse.observation.output"))


def test_parallel_chats_from_two_users_stay_separate(client, world, langfuse):
    users = {"priya.nair": login(client), "meera.s": login(client, "meera.s")}
    asks = [h for h in users.values() for _ in range(4)]

    def ask(headers):
        r = client.post(f"/api/kbs/{DOCS}/chat", headers=headers, json={"question": "dock hours?"})
        assert r.status_code == 200, r.text

    with cf.ThreadPoolExecutor(8) as pool:
        list(pool.map(ask, asks))

    traces = wait_for(langfuse, has("RAG Chat", 8))
    assert len(traces) == 8, [t["name"] for t in traces.values()]
    chats = by_name(traces, "RAG Chat")
    assert sorted(t["user"] for t in chats) == ["meera.s"] * 4 + ["priya.nair"] * 4
    for t in chats:
        assert_well_formed(t, "RAG Chat", t["user"])
        assert sum(s["name"] == "llm_text" for s in t["spans"]) == 1  # each trace holds its own call only


# ------------------------------------------------------------------ background jobs
def test_graph_extraction_job_is_one_trace(client, world, langfuse, monkeypatch):
    monkeypatch.setattr(extraction, "ask_json", llm.ask_json)  # the world fixture switched the LLM off
    priya = login(client)
    with open(SAMPLES / "supplier_orders.xlsx", "rb") as f:
        r = client.post(
            "/api/kbs",
            headers=priya,
            data={"kb_name": "t_obs_extract_kg", "kb_type": "graph", "domain": "Retail", "sub_domain": "Orders"},
            files={"files": ("supplier_orders.xlsx", f)},
        )
    assert r.status_code == 201, r.text
    job = jobs.wait(r.json()["job_id"], timeout=120)
    assert job["status"] == "succeeded", job["error"]

    wait_for(langfuse, has("KG Extraction"))
    time.sleep(2.5)
    traces = langfuse.traces()
    assert len(traces) == 1, [t["name"] for t in traces.values()]
    t = by_name(traces, "KG Extraction")[0]
    assert_well_formed(t, "KG Extraction", "priya.nair")
    assert sum(s["name"] == "llm_json" for s in t["spans"]) >= 3
    assert str(t["root"]["attrs"].get("langfuse.trace.metadata.job_id")) == str(job["id"])


def test_huge_prompts_are_trimmed_so_the_export_is_not_rejected(world, langfuse):
    """Ingestion rejects oversized exports, and a rejected batch takes every span in it (whole traces) along."""
    langfuse.max_body = 1_000_000
    with observability.trace_context("priya.nair", "s1", "chat", "kb"):
        reply = llm.ask_text("system", "row data " * 400_000)  # a 3.6 MB prompt
    assert "30 days" in reply
    traces = wait_for(langfuse, has("Chat"))
    gen = next(s for s in by_name(traces, "Chat")[0]["spans"] if s["name"] == "llm_text")
    assert len(str(langfuse.payload(gen))) < 30_000 and "more characters" in str(langfuse.payload(gen))
    assert all(req["bytes"] <= 1_000_000 for req in langfuse.requests)


def test_content_capture_can_be_switched_off(world, langfuse, settings):
    settings(LANGFUSE_CAPTURE_CONTENT="false")
    with observability.trace_context("priya.nair", "s1", "chat", "kb"):
        llm.ask_text("system", "secret customer data")
    traces = wait_for(langfuse, has("Chat"))
    gen = next(s for s in by_name(traces, "Chat")[0]["spans"] if s["name"] == "llm_text")
    assert "secret customer data" not in str(gen["attrs"]) and "30 days" not in str(gen["attrs"])
    assert gen["attrs"].get("langfuse.observation.usage_details")  # usage and timing still recorded


def test_failed_step_is_marked_as_error_and_still_exported(world, langfuse, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(llm, "get_llm", lambda *a, **k: type("M", (), {"invoke": broken})())
    with pytest.raises(RuntimeError), observability.trace_context("priya.nair", "s1", "chat", "kb"):
        llm.ask_text("system", "q")
    traces = wait_for(langfuse, has("Chat"))
    spans = {s["name"]: s for s in by_name(traces, "Chat")[0]["spans"]}
    assert spans["llm_text"]["attrs"].get("langfuse.observation.level") == "ERROR"
    assert "model unavailable" in spans["Chat"]["attrs"].get("langfuse.observation.status_message", "")


# ------------------------------------------------------------------ MCP server
def test_mcp_tool_calls_are_traced_as_the_signed_in_user(keycloak, mcp_url, langfuse):
    token = keycloak.mint("meera.s")
    question = {"kb_name": DOCS, "question": "What is the return window?"}
    mcp_tests.ok(mcp_tests.call(mcp_url, token, "ask_knowledge_base", question))
    traces = wait_for(langfuse, has("MCP ask_knowledge_base"))
    assert len(traces) == 1, [t["name"] for t in traces.values()]
    t = by_name(traces, "MCP ask_knowledge_base")[0]
    assert_well_formed(t, "MCP ask_knowledge_base", "meera.s")
    assert any(s["name"] == "llm_text" for s in t["spans"])


# ------------------------------------------------------------------ switched off / unreachable
def test_without_keys_nothing_is_sent_and_nothing_breaks(client, world, settings, monkeypatch):
    sink = MockLangfuse()
    with ServerThread(sink.app) as srv:
        settings(LANGFUSE_PUBLIC_KEY="", LANGFUSE_SECRET_KEY="", LANGFUSE_BASE_URL=srv.url)
        _use_scripted_models(monkeypatch)
        r = client.post(f"/api/kbs/{DOCS}/chat", headers=login(client), json={"question": "return window?"})
        assert r.status_code == 200
        time.sleep(2.5)
    assert sink.requests == []


def test_unreachable_langfuse_never_slows_or_breaks_a_request(client, world, settings, monkeypatch):
    key = f"pk-lf-{uuid.uuid4().hex[:8]}"
    settings(LANGFUSE_PUBLIC_KEY=key, LANGFUSE_SECRET_KEY="sk", LANGFUSE_BASE_URL="http://10.255.255.1")
    _use_scripted_models(monkeypatch)
    headers = login(client)
    started = time.time()
    for _ in range(3):
        r = client.post(f"/api/kbs/{DOCS}/chat", headers=headers, json={"question": "return window?"})
        assert r.status_code == 200 and "30 days" in r.json()["answer"]
    assert time.time() - started < 5  # exporting happens in the background, never inside the request
