"""LLM / embedding factory. LLM_PROVIDER selects the backend; nothing else imports a provider."""

import json
import re

from langchain_core.embeddings import Embeddings
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_ollama import ChatOllama, OllamaEmbeddings
from langchain_openai import AzureChatOpenAI, AzureOpenAIEmbeddings

from app.config import get_settings
from app.observability import observe_llm, record_output, update_llm_usage


def get_llm(json_mode: bool = False, temperature: float = 0.0) -> BaseChatModel:
    s = get_settings()
    if s.llm_provider == "ollama":
        llm = ChatOllama(
            base_url=s.ollama_base_url,
            model=s.ollama_chat_model,
            temperature=temperature,
            format="json" if json_mode else None,
            num_ctx=s.ollama_num_ctx,
            keep_alive="30m",
            client_kwargs={"timeout": s.llm_timeout_seconds},
        )
        return llm
    if s.llm_provider == "azure":
        if not (s.azure_openai_endpoint and s.azure_openai_api_key and s.azure_openai_chat_deployment):
            raise ValueError(
                "LLM_PROVIDER=azure needs AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY and AZURE_OPENAI_CHAT_DEPLOYMENT"
            )
        llm = AzureChatOpenAI(
            azure_endpoint=s.azure_openai_endpoint,
            api_key=s.azure_openai_api_key,
            api_version=s.azure_openai_api_version,
            azure_deployment=s.azure_openai_chat_deployment,  # gpt-4.1
            temperature=temperature,
            timeout=s.llm_timeout_seconds,
        )
        return llm.bind(response_format={"type": "json_object"}) if json_mode else llm
    raise ValueError(f"Unsupported LLM_PROVIDER: {s.llm_provider}")


class _PrefixedEmbeddings(Embeddings):
    """nomic-embed-text expects task prefixes; retrieval quality drops noticeably without them."""

    def __init__(self, inner: Embeddings, doc_prefix: str, query_prefix: str):
        self.inner, self.doc_prefix, self.query_prefix = inner, doc_prefix, query_prefix

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self.inner.embed_documents([self.doc_prefix + t for t in texts])

    def embed_query(self, text: str) -> list[float]:
        return self.inner.embed_query(self.query_prefix + text)


class _ObservedEmbeddings(Embeddings):
    """Records each embedding request in Langfuse (counts only, never the text)."""

    def __init__(self, inner: Embeddings):
        self.inner = inner

    @staticmethod
    def _model() -> str:
        s = get_settings()
        return s.ollama_embed_model if s.llm_provider == "ollama" else s.azure_openai_embed_deployment

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        with observe_llm("embedding_documents", {"count": len(texts)}, model=self._model(), as_type="embedding") as obs:
            result = self.inner.embed_documents(texts)
            record_output(obs, {"count": len(result)})
            return result

    def embed_query(self, text: str) -> list[float]:
        with observe_llm("embedding_query", {"length": len(text)}, model=self._model(), as_type="embedding") as obs:
            result = self.inner.embed_query(text)
            record_output(obs, {"dimensions": len(result)})
            return result


def observed_embeddings(inner: Embeddings) -> Embeddings:
    return _ObservedEmbeddings(inner)


def get_embeddings() -> Embeddings:
    s = get_settings()
    if s.llm_provider == "ollama":
        emb = OllamaEmbeddings(base_url=s.ollama_base_url, model=s.ollama_embed_model)
        if s.ollama_embed_model.startswith("nomic-embed-text"):
            emb = _PrefixedEmbeddings(emb, "search_document: ", "search_query: ")
        return _ObservedEmbeddings(emb)
    if s.llm_provider == "azure":
        if not (s.azure_openai_endpoint and s.azure_openai_api_key and s.azure_openai_embed_deployment):
            raise ValueError(
                "LLM_PROVIDER=azure needs AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY and AZURE_OPENAI_EMBED_DEPLOYMENT"
            )
        return _ObservedEmbeddings(
            AzureOpenAIEmbeddings(
                azure_endpoint=s.azure_openai_endpoint,
                api_key=s.azure_openai_api_key,
                api_version=s.azure_openai_api_version,
                azure_deployment=s.azure_openai_embed_deployment,
                # chunks are ~1,000 characters, far below the model limit: skip token counting, which would
                # download tiktoken files from the internet on first use (fails on servers without internet access)
                check_embedding_ctx_length=False,
            )
        )
    raise ValueError(f"Unsupported LLM_PROVIDER: {s.llm_provider}")


# ------------------------------------------------------------------ helpers used by every LLM step
class LLMOutputError(ValueError):
    pass


_THINKING = re.compile(r"<think>.*?</think>", re.S | re.I)


def strip_thinking(text: str) -> str:
    """Reasoning models (e.g. Qwen3) may prefix answers with a <think>...</think> block; users never see it."""
    return _THINKING.sub("", text or "").strip()


def parse_json(text: str):
    """Parse a JSON object from model output, tolerating code fences and surrounding prose."""
    text = strip_thinking(text)
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                pass
    raise LLMOutputError(f"Model did not return valid JSON: {text[:200]!r}")


def ask_json(system: str, user: str, retries: int = 1) -> dict:
    """Ask for a JSON object; on invalid JSON retry once, telling the model what went wrong."""
    llm = get_llm(json_mode=True)
    messages = [("system", system), ("human", user)]
    last_error = None
    for _ in range(retries + 1):
        with observe_llm("llm_json", list(messages)) as generation:
            response = llm.invoke(messages)
            reply = response.content
            record_output(generation, reply)
            update_llm_usage(generation, response)
        try:
            data = parse_json(reply)
            if not isinstance(data, dict):
                raise LLMOutputError("Expected a JSON object")
            return data
        except LLMOutputError as exc:
            last_error = exc
            messages += [("ai", reply), ("human", f"That was not valid JSON ({exc}). Reply with one JSON object only.")]
    raise last_error


def ask_text(system: str, user: str) -> str:
    messages = [("system", system), ("human", user)]
    with observe_llm("llm_text", messages) as generation:
        response = get_llm().invoke(messages)
        reply = response.content
        record_output(generation, reply)
        update_llm_usage(generation, response)
    return strip_thinking(reply)
