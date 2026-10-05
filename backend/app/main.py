import logging
from contextlib import asynccontextmanager

import httpx
import psycopg
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from neo4j import GraphDatabase

from app import jobs, rag
from app.api import auth as auth_api
from app.api import connectors as connectors_api
from app.api import kbs as kbs_api
from app.config import get_settings
from app.db import close_pool, open_pool, run_migrations
from app.graphstore import close_driver

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("chromadb.telemetry").setLevel(logging.CRITICAL)  # telemetry is switched off; hide its noise


@asynccontextmanager
async def lifespan(app: FastAPI):
    run_migrations()
    open_pool()
    jobs.recover_interrupted()
    yield
    close_driver()
    close_pool()


app = FastAPI(title="Graphbase API", lifespan=lifespan)

CSRF_HEADER = "X-Requested-With"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

# The UI reaches the API through the Vite proxy (same origin), so no CORS is needed. Only list origins in
# CORS_ORIGINS if a page on another origin must call the API: "*" with credentials would let any site send
# the CSRF header with the user's cookie, so it is never used.
_cors_origins = [o.strip() for o in get_settings().cors_origins.split(",") if o.strip() and o.strip() != "*"]
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Content-Type", CSRF_HEADER],
    )


@app.middleware("http")
async def csrf_protection(request: Request, call_next):
    """Sessions are cookies, so state-changing API calls must also carry a custom header. Browsers only
    let other sites send custom headers after a CORS preflight, which this API never grants."""
    if (
        request.method not in SAFE_METHODS
        and request.url.path.startswith("/api/")
        and request.headers.get(CSRF_HEADER) != "graphbase"
    ):
        return JSONResponse({"detail": "Missing CSRF header"}, status_code=403)
    return await call_next(request)


app.include_router(auth_api.router)
app.include_router(connectors_api.router)
app.include_router(kbs_api.router)


def _check_postgres(s) -> str:
    with psycopg.connect(s.postgres_dsn, connect_timeout=3) as conn:
        return conn.execute("SELECT version()").fetchone()[0].split(",")[0]


def _check_neo4j(s) -> str:
    with GraphDatabase.driver(s.neo4j_uri, auth=(s.neo4j_user, s.neo4j_password)) as driver:
        info = driver.execute_query(
            "CALL dbms.components() YIELD name, versions, edition RETURN versions[0] AS v, edition"
        ).records[0]
        return f"{info['v']} {info['edition']} (mode={s.neo4j_mode})"


def _check_chroma(s) -> str:
    client = rag.chroma_client()
    client.heartbeat()
    return f"ok ({client.get_version()})"


def _check_ollama(s) -> str:
    tags = httpx.get(f"{s.ollama_base_url}/api/tags", timeout=3).json()
    models = {m["name"] for m in tags.get("models", [])}
    missing = [
        m for m in (s.ollama_chat_model, s.ollama_embed_model) if m not in models and f"{m}:latest" not in models
    ]
    if missing:
        raise RuntimeError(f"reachable, missing models: {', '.join(missing)}")
    return "ok"


@app.get("/api/health")
def health():
    s = get_settings()
    checks = {"postgres": _check_postgres, "neo4j": _check_neo4j, "chroma": _check_chroma}
    if s.llm_provider == "ollama":
        checks["ollama"] = _check_ollama
    result = {}
    for name, fn in checks.items():
        try:
            result[name] = {"ok": True, "detail": fn(s)}
        except Exception as exc:  # report every dependency, don't stop at the first failure
            result[name] = {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
    return {"ok": all(v["ok"] for v in result.values()), "services": result}
