"""The TurboQuant vector store (app/vectorstore.py): saving, replacing and deleting documents, search quality against
exact cosine search, writes from several threads and processes, clear errors, and health/doctor checks. Real
Postgres and turbovec; bag-of-words embeddings instead of a model."""

import concurrent.futures as cf
import os
import subprocess
import sys
import uuid
from pathlib import Path

import numpy as np
import pytest

from app import doctor, rag, vectorstore

from .fixtures import SAMPLES
from .test_mcp_server import HashEmbeddings

DOCS = ["returns_policy.pdf", "vendor_handbook.docx", "warehouse_sop.txt"]
QUESTIONS = [
    "What is the standard return window?",
    "What is the restocking fee for electronics?",
    "What are the vendor payment terms?",
    "What is the late delivery penalty?",
    "What temperature must the Chennai cold room be kept at?",
    "What are the inbound dock hours?",
]


@pytest.fixture
def name():
    kb_name = f"t_vs_{uuid.uuid4().hex[:8]}"
    yield kb_name
    rag.drop_index(kb_name)


@pytest.fixture
def hashed(monkeypatch):
    monkeypatch.setattr(rag, "get_embeddings", lambda: HashEmbeddings())


def _chunks(texts, page=None):
    return [{"text": t, "page": page, "chunk": i, "run_id": None} for i, t in enumerate(texts)]


def _vec(text):
    return HashEmbeddings().embed_query(text)


# ------------------------------------------------------------------ basics
def test_store_search_replace_and_delete(name, hashed):
    texts = ["Returns are accepted within 30 days.", "Dock hours are 06:00 to 14:00.", "Vendors are paid net 45."]
    assert rag.store_chunks(name, "policy.txt", [{"text": t, "page": None} for t in texts], run_id=None) == 3
    assert rag.store_chunks(name, "guide.pdf", [{"text": "Cold room between 2 and 8 degrees.", "page": 4}]) == 1
    assert rag.documents(name) == {"guide.pdf": 1, "policy.txt": 3}
    assert vectorstore.index_path(name).exists()

    top = rag.retrieve(name, "When are the dock hours?", k=2)
    assert top[0]["text"] == "Dock hours are 06:00 to 14:00." and top[0]["source"] == "policy.txt"
    assert top[0]["page"] is None and 0 < top[0]["score"] <= 1 and top[0]["score"] >= top[1]["score"]
    assert rag.retrieve(name, "cold room temperature", k=1)[0]["page"] == 4

    # uploading the same document again replaces it: no duplicates, old text gone
    rag.store_chunks(name, "policy.txt", [{"text": "Returns are accepted within 45 days.", "page": None}])
    assert rag.documents(name) == {"guide.pdf": 1, "policy.txt": 1}
    assert vectorstore.count(name) == 2
    assert all("30 days" not in h["text"] for h in rag.retrieve(name, "return window days", k=5))

    rag.drop_index(name)
    assert rag.documents(name) == {} and rag.retrieve(name, "anything") == []
    assert not vectorstore.index_path(name).exists()


def test_scores_are_cosine_similarity(name):
    rng = np.random.default_rng(1)
    vectors = rng.standard_normal((50, 384)).astype(np.float32) * 7  # not normalised: the store normalises
    vectorstore.replace_source(name, "r.txt", _chunks([f"row {i}" for i in range(50)]), vectors)
    query = vectors[7] + 0.1 * rng.standard_normal(384).astype(np.float32)
    hit = vectorstore.search(name, query, 1)[0]
    exact = float(query @ vectors[7] / np.linalg.norm(query) / np.linalg.norm(vectors[7]))
    assert hit["text"] == "row 7" and abs(hit["score"] - exact) < 0.02


def test_dimensions_that_are_not_a_multiple_of_8_are_padded(name):
    vectorstore.replace_source(name, "d.txt", _chunks(["a", "b"]), [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])
    assert vectorstore.search(name, [0.9, 0.1, 0.0], 1)[0]["text"] == "a"


def test_search_matches_exact_cosine_search_on_the_sample_documents(name, hashed):
    """4-bit TurboQuant against brute-force float32 cosine over the same chunks: the passage exact search ranks
    first is in TurboQuant's top 3 for every question."""
    emb = HashEmbeddings()
    for doc in DOCS:
        rag.store_chunks(name, doc, rag.chunk(rag.extract_text(SAMPLES / doc, doc)))
    with vectorstore.get_conn() as conn:
        rows = conn.execute("SELECT id, source, text FROM rag_chunks WHERE kb_name = %s", (name,)).fetchall()
    matrix = np.asarray(emb.embed_documents([f"{r['source']}\n{r['text']}" for r in rows]), dtype=np.float32)
    for q in QUESTIONS:
        qv = np.asarray(emb.embed_query(q), dtype=np.float32)
        best = rows[int(np.argmax(matrix @ qv))]["text"]
        assert best in [h["text"] for h in rag.retrieve(name, q, k=3)], q


# ------------------------------------------------------------------ concurrency
def test_parallel_uploads_to_one_knowledge_base_keep_every_document(name, hashed):
    def upload(i):
        rag.store_chunks(name, f"doc{i}.txt", [{"text": f"document {i} part {j}", "page": None} for j in range(5)])

    with cf.ThreadPoolExecutor(8) as pool:
        list(pool.map(upload, range(16)))
    docs = rag.documents(name)
    assert len(docs) == 16 and set(docs.values()) == {5}
    assert vectorstore.count(name) == 80  # index and rows agree


def test_a_write_from_another_process_is_seen_without_restart(name):
    """The backend and the MCP server are separate processes sharing VECTOR_DIR."""
    vectorstore.replace_source(name, "a.txt", _chunks(["alpha one"]), [_vec("alpha one")])
    assert vectorstore.search(name, _vec("alpha"), 5)[0]["text"] == "alpha one"  # now cached in this process

    code = (
        "import sys; from tests.test_mcp_server import HashEmbeddings; from app import vectorstore; "
        "t = 'beta two'; vectorstore.replace_source(sys.argv[1], 'b.txt', "
        "[{'text': t, 'page': None, 'chunk': 0, 'run_id': None}], [HashEmbeddings().embed_query(t)])"
    )
    backend = Path(__file__).resolve().parents[1]
    subprocess.run([sys.executable, "-c", code, name], cwd=backend, env=os.environ.copy(), check=True, timeout=120)
    assert vectorstore.search(name, _vec("beta"), 1)[0]["text"] == "beta two"
    assert vectorstore.count(name) == 2


# ------------------------------------------------------------------ errors
def test_a_different_embedding_model_is_reported_clearly(name, hashed, monkeypatch):
    rag.store_chunks(name, "a.txt", [{"text": "hello", "page": None}])  # 256 dimensions

    class Bigger:
        def embed_query(self, text):
            return [0.1] * 1024

        def embed_documents(self, texts):
            return [[0.1] * 1024 for _ in texts]

    monkeypatch.setattr(rag, "get_embeddings", lambda: Bigger())
    with pytest.raises(rag.ServiceError, match="gives 256 dimensions, but the current model gives 1024"):
        rag.retrieve(name, "hello")
    with pytest.raises(rag.ServiceError, match="Switch back to the embedding model"):
        rag.store_chunks(name, "b.txt", [{"text": "more", "page": None}])
    assert rag.documents(name) == {"a.txt": 1}  # the failed upload changed nothing


def test_a_damaged_index_file_is_reported_clearly(name, hashed):
    rag.store_chunks(name, "a.txt", [{"text": "hello", "page": None}])
    vectorstore.index_path(name).write_bytes(b"not an index")
    with pytest.raises(rag.ServiceError, match="could not be read .* damaged"):
        rag.retrieve(name, "hello")


# ------------------------------------------------------------------ checks
def test_health_and_doctor_report_the_store(client, capsys):
    services = client.get("/api/health").json()["services"]
    assert services["vector_store"]["ok"] and "TurboQuant (turbovec" in services["vector_store"]["detail"]
    check = dict((n, fn) for n, fn, _ in doctor.checks())["Vector store (TurboQuant)"]
    assert "save, search, delete OK" in check()
    assert not list(vectorstore.folder().glob("kf_doctor_*"))  # the test index is removed again
