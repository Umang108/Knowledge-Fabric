"""`python -m app.cli doctor`: checks every service the app needs, the way the app itself uses them, and says
what to fix. Run it from the backend folder with the same .env / environment as uvicorn."""

import os
import tempfile
import urllib.request
import uuid
from pathlib import Path


def _proxy_note(host: str) -> str:
    """Python HTTP clients send requests through HTTP(S)_PROXY unless the host is listed in NO_PROXY."""
    proxy = os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")
    if not proxy:
        return ""
    if urllib.request.proxy_bypass(host):
        return ""
    return f" Note: HTTP_PROXY is set and {host} is not in NO_PROXY, so requests to it go through the proxy."


def checks():
    from app import llm, rag, vectorstore
    from app.config import get_settings
    from app.db import get_conn

    s = get_settings()

    def postgres():
        with get_conn() as conn:
            conn.execute("SELECT 1")
        return f"{s.postgres_host}:{s.postgres_port}/{s.postgres_db}"

    def uploads():
        folder = Path(s.upload_dir)
        folder.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=folder):
            pass
        return f"{folder.resolve()} is writable"

    def vector_store():
        status = rag._store("checking the index folder", vectorstore.status)
        name = f"kf_doctor_{uuid.uuid4().hex[:8]}"
        try:
            chunk = [{"text": "doctor check", "page": None, "chunk": 0, "run_id": None}]
            rag._store(
                "saving a test vector", lambda: vectorstore.replace_source(name, "doctor.txt", chunk, [[0.1, 0.2, 0.3]])
            )
            hits = rag._store("searching", lambda: vectorstore.search(name, [0.1, 0.2, 0.3], 1))
            if not hits or hits[0]["text"] != "doctor check":
                raise RuntimeError(f"the test vector was saved but not found again: {hits}")
        finally:
            rag.drop_index(name)
        return f"{status}; save, search, delete OK"

    def embeddings():
        vec = rag._embed(lambda: llm.get_embeddings().embed_query("doctor check"))
        model = s.azure_openai_embed_deployment if s.llm_provider == "azure" else s.ollama_embed_model
        return f"{s.llm_provider} / {model}: {len(vec)} dimensions"

    def chat_model():
        reply = llm.ask_text("Reply with the single word OK.", "Say OK")
        model = s.azure_openai_chat_deployment if s.llm_provider == "azure" else s.ollama_chat_model
        return f"{s.llm_provider} / {model}: replied {reply.strip()[:40]!r}"

    def neo4j():
        from app.graphstore import get_driver

        get_driver().verify_connectivity()
        return f"{s.neo4j_uri} ({s.neo4j_mode} mode)"

    return [
        ("Postgres", postgres, s.postgres_host),
        ("Upload folder", uploads, None),
        ("Vector store (TurboQuant)", vector_store, None),
        ("Embedding model", embeddings, None),
        ("Chat model", chat_model, None),
        ("Neo4j", neo4j, None),
    ]


def run() -> int:
    failed = 0
    for name, fn, host in checks():
        try:
            detail = fn()
            print(f"  OK    {name}: {detail}")
        except Exception as exc:  # noqa: BLE001 - every check reports, none stops the others
            failed += 1
            print(f"  FAIL  {name}: {type(exc).__name__}: {exc}{_proxy_note(host) if host else ''}")
    print("All checks passed." if not failed else f"{failed} check(s) failed.")
    return 1 if failed else 0
