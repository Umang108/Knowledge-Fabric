"""A stand-in OpenAI-compatible API (chat completions + embeddings) for tests that need a model, such as the RAGAS
judge. It answers every structured-output request with an object built from the JSON schema in the request, so
any client (instructor, openai SDK, Ollama's /v1 or Azure's deployment paths) works without a real model.

    mock = MockOpenAI(); app = mock.app   # run with tests.keycloak_mock.ServerThread
"""

import hashlib
import json
import math
import re
import threading

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route


def _instance(schema: dict, defs: dict, depth: int = 0):
    """A small valid value for a JSON schema: verdicts and flags 1/true, one item per list, short strings."""
    if depth > 12:
        return None
    if "$ref" in schema:
        return _instance(defs.get(schema["$ref"].rsplit("/", 1)[-1], {}), defs, depth + 1)
    for key in ("anyOf", "oneOf", "allOf"):
        if key in schema:
            options = [o for o in schema[key] if o.get("type") != "null"] or schema[key]
            return _instance(options[0], defs, depth + 1)
    if "enum" in schema:
        return schema["enum"][0]
    if "const" in schema:
        return schema["const"]
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((k for k in kind if k != "null"), "string")
    if kind == "object" or "properties" in schema:
        return {k: _instance(v, defs, depth + 1) for k, v in schema.get("properties", {}).items()}
    if kind == "array":
        return [_instance(schema.get("items", {}), defs, depth + 1)]
    if kind == "integer":
        return max(1, int(schema.get("minimum", 1))) if schema.get("maximum", 1) >= 1 else 0
    if kind == "number":
        return 1.0 if schema.get("maximum", 1) >= 1 else float(schema.get("maximum", 0))
    if kind == "boolean":
        return True
    return "The answer is supported by the passages."


def _schema_from(body: dict) -> dict | None:
    fmt = body.get("response_format") or {}
    if isinstance(fmt.get("json_schema"), dict) and fmt["json_schema"].get("schema"):
        return fmt["json_schema"]["schema"]
    for tool in body.get("tools") or []:
        params = (tool.get("function") or {}).get("parameters")
        if params:
            return params
    decoder = json.JSONDecoder()
    for msg in body.get("messages", []):
        text = msg.get("content") if isinstance(msg.get("content"), str) else json.dumps(msg.get("content"))
        for m in re.finditer(r"\{", text or ""):
            try:
                obj, _ = decoder.raw_decode(text, m.start())
            except ValueError:
                continue
            if isinstance(obj, dict) and ("properties" in obj or "$defs" in obj):
                return obj
    return None


def _embedding(text: str, dim: int = 64) -> list[float]:
    v = [0.0] * dim
    for w in re.findall(r"[a-z0-9]+", str(text).lower()):
        v[int(hashlib.md5(w.encode()).hexdigest(), 16) % dim] += 1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


class MockOpenAI:
    def __init__(self):
        self.requests: list[dict] = []
        self._lock = threading.Lock()

        async def chat(request: Request):
            body = await request.json()
            with self._lock:
                self.requests.append({"path": request.url.path, "kind": "chat", "model": body.get("model")})
            schema = _schema_from(body)
            content = json.dumps(_instance(schema, schema.get("$defs", {}))) if schema else "OK"
            message = {"role": "assistant", "content": content}
            if body.get("tools") and schema:
                name = body["tools"][0]["function"]["name"]
                message = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {"id": "call_1", "type": "function", "function": {"name": name, "arguments": content}}
                    ],
                }
            return JSONResponse(
                {
                    "id": "chatcmpl-mock",
                    "object": "chat.completion",
                    "created": 0,
                    "model": body.get("model") or "mock",
                    "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                }
            )

        async def embeddings(request: Request):
            body = await request.json()
            texts = body.get("input")
            texts = [texts] if isinstance(texts, str) else texts
            with self._lock:
                self.requests.append({"path": request.url.path, "kind": "embeddings", "model": body.get("model")})
            return JSONResponse(
                {
                    "object": "list",
                    "data": [
                        {"object": "embedding", "index": i, "embedding": _embedding(t)} for i, t in enumerate(texts)
                    ],
                    "model": body.get("model") or "mock",
                    "usage": {"prompt_tokens": 1, "total_tokens": 1},
                }
            )

        self.app = Starlette(
            routes=[
                Route("/v1/chat/completions", chat, methods=["POST"]),  # Ollama / OpenAI
                Route("/v1/embeddings", embeddings, methods=["POST"]),
                Route("/openai/deployments/{deployment}/chat/completions", chat, methods=["POST"]),  # Azure
                Route("/openai/deployments/{deployment}/embeddings", embeddings, methods=["POST"]),
            ]
        )

    def count(self, kind: str) -> int:
        return sum(1 for r in self.requests if r["kind"] == kind)
