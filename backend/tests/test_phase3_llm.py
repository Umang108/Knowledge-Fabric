"""Phase 3: LLM / embedding factory."""

import pytest
from langchain_core.embeddings import Embeddings
from langchain_ollama import ChatOllama

from app import llm


def test_factory_builds_configured_ollama_models(settings):
    s = settings(LLM_PROVIDER="ollama", OLLAMA_CHAT_MODEL="qwen2.5:3b", OLLAMA_EMBED_MODEL="nomic-embed-text")
    chat = llm.get_llm(json_mode=True)
    assert isinstance(chat, ChatOllama) and chat.model == "qwen2.5:3b" and chat.format == "json"
    assert chat.temperature == 0 and chat.base_url == s.ollama_base_url
    assert llm.get_llm().format is None
    emb = llm.get_embeddings()  # traced wrapper (Langfuse) around the nomic prefixing wrapper
    assert isinstance(emb, llm._ObservedEmbeddings) and isinstance(emb.inner, llm._PrefixedEmbeddings)
    assert emb.inner.inner.model == "nomic-embed-text"


def test_switching_provider_is_one_env_var(settings):
    settings(LLM_PROVIDER="azure", AZURE_OPENAI_ENDPOINT="", AZURE_OPENAI_API_KEY="")
    with pytest.raises(ValueError, match="AZURE_OPENAI_ENDPOINT"):  # says which setting is missing
        llm.get_llm()
    with pytest.raises(ValueError, match="AZURE_OPENAI_EMBED_DEPLOYMENT"):
        llm.get_embeddings()
    settings(LLM_PROVIDER="azure", AZURE_OPENAI_ENDPOINT="https://x.openai.azure.com", AZURE_OPENAI_API_KEY="k")
    assert type(llm.get_llm()).__name__ == "AzureChatOpenAI"
    settings(LLM_PROVIDER="bedrock")
    with pytest.raises(ValueError, match="Unsupported LLM_PROVIDER"):
        llm.get_llm()


@pytest.mark.parametrize(
    "text,expected",
    [
        ('{"a": 1}', {"a": 1}),
        ('```json\n{"a": 1}\n```', {"a": 1}),
        ('Sure! Here it is: {"a": [1, 2]} hope that helps', {"a": [1, 2]}),
    ],
)
def test_parse_json_tolerates_model_formatting(text, expected):
    assert llm.parse_json(text) == expected


def test_parse_json_rejects_non_json():
    with pytest.raises(llm.LLMOutputError):
        llm.parse_json("I cannot do that")


def test_ask_json_retries_once_with_the_error(monkeypatch):
    calls = []

    class Fake:
        def invoke(self, messages):
            calls.append(messages)
            return type("R", (), {"content": "not json" if len(calls) == 1 else '{"ok": true}'})()

    monkeypatch.setattr(llm, "get_llm", lambda **kw: Fake())
    assert llm.ask_json("sys", "user") == {"ok": True}
    assert len(calls) == 2 and "not valid JSON" in calls[1][-1][1]


def test_nomic_prefixes_are_added():
    class Inner(Embeddings):
        def embed_documents(self, texts):
            self.docs = texts
            return [[0.0]] * len(texts)

        def embed_query(self, text):
            self.query = text
            return [0.0]

    inner = Inner()
    emb = llm._PrefixedEmbeddings(inner, "search_document: ", "search_query: ")
    emb.embed_documents(["a"]), emb.embed_query("b")
    assert inner.docs == ["search_document: a"] and inner.query == "search_query: b"


def test_real_embeddings_available():
    v = llm.get_embeddings().embed_query("Which suppliers deliver to Chennai?")
    assert len(v) == 768


@pytest.mark.llm
def test_real_chat_model_returns_json():
    assert llm.ask_json("Reply with JSON only.", 'Return {"answer": 4} for 2+2.') == {"answer": 4}


def test_reasoning_blocks_are_removed(monkeypatch):
    assert llm.parse_json('<think>{"draft": 1} maybe</think>\n{"answer": 2}') == {"answer": 2}

    class Fake:
        def invoke(self, messages):
            return type("R", (), {"content": "<think>let me reason</think>\nThe answer is 48."})()

    monkeypatch.setattr(llm, "get_llm", lambda **kw: Fake())
    assert llm.ask_text("sys", "q") == "The answer is 48."
