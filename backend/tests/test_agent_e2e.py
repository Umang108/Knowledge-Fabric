"""The agent end to end: Keycloak sign-in (device code / password) -> LangGraph agent -> MCP server.

Uses the same running stand-in Keycloak, real MCP server, Postgres and TurboQuant store as test_mcp_server.py.
The LLM is a scripted tool-calling model so the test checks the plumbing (tokens, refresh, per-user access, tool calls)
without depending on a hosted model.
"""

import json
import os
import re
import stat
import sys
import time
from pathlib import Path

import anyio
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from .test_mcp_server import DOCS, keycloak, mcp_url, world  # noqa: F401  (fixtures)

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "agent"))
from graphbase_agent import __main__ as cli  # noqa: E402
from graphbase_agent import agent as agent_mod  # noqa: E402
from graphbase_agent.keycloak import KeycloakSession, LoginError  # noqa: E402


def _text(content) -> str:
    if isinstance(content, str):
        return content
    return "".join(c.get("text", "") if isinstance(c, dict) else str(c) for c in content)


class ScriptedModel(BaseChatModel):
    """Behaves like a tool-calling LLM: list KBs -> search the first RAG store -> answer from the passage."""

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        question = next(m.content for m in reversed(messages) if isinstance(m, HumanMessage))
        last = messages[-1]
        if not isinstance(last, ToolMessage):
            msg = AIMessage("", tool_calls=[{"name": "list_knowledge_bases", "args": {}, "id": "c1"}])
        elif last.name == "list_knowledge_bases":
            kbs = json.loads(_text(last.content))["knowledge_bases"]
            rag = [k["kb_name"] for k in kbs if k["kb_type"] == "rag"]
            if not rag:
                msg = AIMessage("You have no knowledge bases I can search.")
            else:
                msg = AIMessage(
                    "",
                    tool_calls=[
                        {"name": "search_documents", "id": "c2", "args": {"kb_name": rag[0], "query": question, "k": 3}}
                    ],
                )
        else:
            data = json.loads(_text(last.content))
            if "passages" not in data:
                msg = AIMessage(f"Tool failed: {_text(last.content)}")
            else:  # quote the passage that best matches the question, as an LLM would
                words = set(re.findall(r"[a-z]+", question.lower()))
                best = max(data["passages"], key=lambda p: len(words & set(re.findall(r"[a-z]+", p["text"].lower()))))
                msg = AIMessage(f"From {DOCS}: {best['text']}")
        return ChatResult(generations=[ChatGeneration(message=msg)])


def _session(kc, **kw):
    return KeycloakSession(kc.base_url, "graphbase", "graphbase-agent", **kw)


def _ask(session, url, question):
    bot = agent_mod.GraphbaseAgent(session, f"{url}/mcp", ScriptedModel())
    return anyio.run(bot.ask, question), bot


def test_device_code_sign_in_then_answer_from_the_users_rag_store(mcp_url, keycloak):  # noqa: F811
    session = _session(keycloak)
    shown = []

    def show(message, link, code):  # the person opens the link and signs in as meera.s
        shown.append(message)
        keycloak.approve(code, "meera.s")

    assert session.login_device(show=show)["user_id"] == "meera.s"
    assert "open http" in shown[0] and "confirm the code" in shown[0]
    answer, bot = _ask(session, mcp_url, "What is the restocking fee for electronics?")
    assert answer.startswith(f"From {DOCS}:") and "restocking" in answer.lower()
    assert {t.name for t in bot.tools} >= {"list_knowledge_bases", "search_documents", "query_graph"}


def test_agent_only_sees_what_the_user_may_use(mcp_url, keycloak):  # noqa: F811
    keycloak.users["new.joiner"] = "pw"
    session = _session(keycloak)
    session.login_password("new.joiner", "pw")
    answer, _ = _ask(session, mcp_url, "What is the restocking fee?")
    assert answer == "You have no knowledge bases I can search."


def test_expired_access_tokens_are_refreshed(mcp_url, keycloak):  # noqa: F811
    keycloak.token_lifetime = 2
    session = _session(keycloak)
    session.login_password("meera.s", "pw")
    time.sleep(2.5)  # the first access token has expired
    answer, _ = _ask(session, mcp_url, "restocking fee")
    assert "restocking" in answer.lower()
    assert any(r["grant_type"] == "refresh_token" for r in keycloak.token_requests)


def test_a_401_triggers_one_refresh_and_retry(mcp_url, keycloak):  # noqa: F811
    session = _session(keycloak)
    session.login_password("meera.s", "pw")
    forged = keycloak.mint("meera.s", key=rsa.generate_private_key(public_exponent=65537, key_size=2048))
    session._tokens["access_token"] = forged  # the server will reject it
    answer, _ = _ask(session, mcp_url, "restocking fee")
    assert "restocking" in answer.lower() and session._tokens["access_token"] != forged


def test_failed_sign_in_and_ended_session(keycloak):  # noqa: F811
    with pytest.raises(LoginError, match="Invalid user credentials"):
        _session(keycloak).login_password("meera.s", "wrong")
    session = _session(keycloak)
    session.login_password("meera.s", "pw")
    keycloak.refresh.clear()  # e.g. the admin ended the Keycloak session
    session._expires_at = 0
    with pytest.raises(LoginError, match="sign in again"):
        session.access_token()
    assert not session.signed_in


def test_remembered_session_is_private_and_logout_removes_it(keycloak, tmp_path):  # noqa: F811
    cache = tmp_path / "agent-session.json"
    _session(keycloak, token_cache=cache).login_password("meera.s", "pw")
    assert stat.S_IMODE(os.stat(cache).st_mode) == 0o600
    again = _session(keycloak, token_cache=cache)
    assert again.signed_in and again.user["user_id"] == "meera.s"
    again.logout()
    assert not cache.exists()


def test_command_line(mcp_url, keycloak, monkeypatch, tmp_path):  # noqa: F811
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KEYCLOAK_URL", keycloak.base_url)
    monkeypatch.setenv("KEYCLOAK_REALM", "graphbase")
    monkeypatch.setenv("GRAPHBASE_MCP_URL", f"{mcp_url}/mcp")
    monkeypatch.setattr(agent_mod, "get_llm", lambda: ScriptedModel())
    out = []
    code = cli.main(
        ["--login", "password", "--username", "meera.s", "--password", "pw", "What", "is", "the", "restocking", "fee?"],
        out=out.append,
    )
    assert code == 0 and out[0] == "Signed in as meera.s." and "restocking" in out[1].lower()

    answers = iter(["restocking fee for electronics", "exit"])
    out.clear()
    code = cli.main(
        ["--login", "password", "--username", "meera.s", "--password", "pw"],
        input_fn=lambda _: next(answers),
        out=out.append,
    )
    assert code == 0 and "restocking" in out[-1].lower()

    out.clear()
    assert cli.main(["--login", "password", "--username", "meera.s", "--password", "bad"], out=out.append) == 2
    assert out[0].startswith("Sign-in failed")
