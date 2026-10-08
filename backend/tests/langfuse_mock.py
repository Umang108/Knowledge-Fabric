"""A stand-in for Langfuse's OpenTelemetry ingestion endpoint (POST /api/public/otel/v1/traces).

It decodes the OTLP protobuf the Langfuse SDK sends and keeps every span, so tests can check what would show up
in Langfuse: which traces exist, their name / user / session, and that every LLM call sits inside its trace.
It can also behave like a real server under load: reject bodies above a size limit (HTTP 413) or answer slowly.
"""

import base64
import json
import threading
import time

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route


def _value(v):
    kind = v.WhichOneof("value")
    if kind == "array_value":
        return [_value(x) for x in v.array_value.values]
    return getattr(v, kind) if kind else None


class MockLangfuse:
    def __init__(self, public_key: str = "pk-lf-test", secret_key: str = "sk-lf-test"):
        self.public_key, self.secret_key = public_key, secret_key
        self.spans: list[dict] = []
        self.requests: list[dict] = []
        self.max_body = None  # bytes; larger exports get 413 like a real ingestion endpoint
        self.delay = 0.0  # seconds before answering
        self.lock = threading.Lock()
        self.errors: list[str] = []

        # Plain Starlette: a FastAPI app would trace its own requests once a global OTel provider exists,
        # and in-process those spans would be exported back to this endpoint in an endless loop.
        async def ingest(request: Request):
            body = await request.body()
            expected = "Basic " + base64.b64encode(f"{self.public_key}:{self.secret_key}".encode()).decode()
            ok_auth = request.headers.get("authorization") == expected
            with self.lock:
                self.requests.append({"bytes": len(body), "auth": ok_auth})
            if self.delay:
                time.sleep(self.delay)
            if not ok_auth:
                return Response(status_code=401)
            if self.max_body and len(body) > self.max_body:
                return Response(status_code=413)
            req = ExportTraceServiceRequest()
            try:
                req.ParseFromString(body)
                self._store(req)
            except Exception as exc:  # a bug here must fail the test, not look like a slow server
                self.errors.append(repr(exc))
                return Response(status_code=400)
            return Response(status_code=200)

        self.app = Starlette(routes=[Route("/api/public/otel/v1/traces", ingest, methods=["POST"])])

    def _store(self, req) -> None:
        spans = [
            {
                "trace_id": s.trace_id.hex(),
                "span_id": s.span_id.hex(),
                "parent_id": s.parent_span_id.hex() or None,
                "name": s.name,
                "attrs": {a.key: _value(a.value) for a in s.attributes},
            }
            for rs in req.resource_spans
            for ss in rs.scope_spans
            for s in ss.spans
        ]
        with self.lock:
            self.spans.extend(spans)

    # ------------------------------------------------------------------ what Langfuse would show
    def traces(self) -> dict[str, dict]:
        """trace id -> {root, spans, name, user, session}. Trace-level fields come from the root span, the way
        this app sets them (children never touch them)."""
        out: dict[str, dict] = {}
        with self.lock:
            spans = list(self.spans)
        for s in spans:
            t = out.setdefault(s["trace_id"], {"spans": [], "root": None})
            t["spans"].append(s)
            if s["parent_id"] is None:
                t["root"] = s
        for t in out.values():
            attrs = (t["root"] or {}).get("attrs", {})
            t["name"] = attrs.get("langfuse.trace.name")
            t["user"] = attrs.get("user.id")
            t["session"] = attrs.get("session.id")
            t["names_set_by_children"] = [
                s["name"] for s in t["spans"] if s is not t["root"] and "langfuse.trace.name" in s["attrs"]
            ]
        return out

    def observation_type(self, span: dict) -> str | None:
        return span["attrs"].get("langfuse.observation.type")

    @staticmethod
    def payload(span: dict, key: str = "langfuse.observation.input"):
        raw = span["attrs"].get(key)
        try:
            return json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            return raw
