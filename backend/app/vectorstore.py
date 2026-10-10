"""Vector store for RAG knowledge bases: Google's TurboQuant vector quantization (the `turbovec` library).

Layout
  {VECTOR_DIR}/{kb_name}.tvim   one TurboQuant index per knowledge base: every chunk's embedding, compressed to
                                TURBOQUANT_BITS (2, 3 or 4) bits per coordinate, keyed by rag_chunks.id
  Postgres table rag_chunks     the text and metadata (source document, page, chunk number, run) of every chunk

TurboQuant needs no training and no server: vectors are randomly rotated and each coordinate is quantized with a
fixed codebook. A 4-bit index is about 4x smaller than the float32 vectors and is searched in memory in the backend.
Vectors are L2-normalised, so a search score is the cosine similarity (1 = same direction), as before with Chroma.

Concurrency
  Writes to one knowledge base are serialised with a Postgres advisory lock, so the backend and the MCP server
  (separate processes) can both write. The index file is replaced atomically (write a temp file, then rename), and
  the rows are committed after it, so a reader sees either the old or the new version.
  Each process keeps loaded indexes in memory and reloads one when its file changes.
  Every process that uses the store (backend, MCP server) must see the same VECTOR_DIR.
"""

from __future__ import annotations

import logging
import os
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from app.config import get_settings
from app.db import get_conn

log = logging.getLogger(__name__)

class VectorStoreError(RuntimeError):
    """Raised for problems the caller should report as they are (wrong embedding model, damaged file)."""


def _turbovec():
    import turbovec  # imported lazily so the rest of the app starts even when it is missing

    return turbovec


def library_version() -> str:
    return _turbovec().__version__


def folder() -> Path:
    return Path(get_settings().vector_dir).expanduser()


def index_path(kb_name: str) -> Path:
    return folder() / f"{kb_name}{get_settings().vector_index_suffix}"


def _bits() -> int:
    bits = get_settings().turboquant_bits
    if bits not in (2, 3, 4):
        raise VectorStoreError(f"TURBOQUANT_BITS must be 2, 3 or 4 (now {bits})")
    return bits


# ------------------------------------------------------------------ vectors
def _prepare(vectors) -> np.ndarray:
    """float32, L2-normalised (score = cosine), zero-padded to a multiple of 8 dimensions (TurboQuant's block size;
    padding with zeros changes no inner product)."""
    arr = np.asarray(vectors, dtype=np.float32)
    if arr.ndim != 2 or arr.shape[1] == 0:
        raise VectorStoreError(f"expected a list of vectors, got shape {arr.shape}")
    if not np.isfinite(arr).all():
        raise VectorStoreError("the embedding model returned NaN or infinite values")
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    arr = arr / np.where(norms == 0, 1.0, norms)
    pad = (-arr.shape[1]) % 8
    if pad:
        arr = np.pad(arr, ((0, 0), (0, pad)))
    return np.ascontiguousarray(arr, dtype=np.float32)


def _check_dim(index, vectors: np.ndarray, kb_name: str) -> None:
    if index.dim is not None and len(index) and index.dim != vectors.shape[1]:
        raise VectorStoreError(
            f"'{kb_name}' was indexed with an embedding model that gives {index.dim} dimensions, but the current "
            f"model gives {vectors.shape[1]}. Switch back to the embedding model used when the documents were "
            "uploaded, or upload them again into a new knowledge base."
        )


# ------------------------------------------------------------------ files
def _new_index():
    return _turbovec().IdMapIndex(bit_width=_bits())


def _load(path: Path):
    try:
        return _turbovec().IdMapIndex.load(str(path))
    except (ValueError, OSError) as exc:
        if isinstance(exc, (FileNotFoundError, PermissionError)):
            raise
        raise VectorStoreError(
            f"the index file {path} could not be read ({exc}). It may be damaged or incomplete; restore it from a "
            "backup, or delete the knowledge base and upload the documents again."
        ) from exc


def _save(index, path: Path) -> None:
    """Write next to the destination, flush to disk, then rename over it (atomic on Linux and Windows)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        index.write(str(tmp))
        with open(tmp, "rb+") as f:
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


# ------------------------------------------------------------------ in-memory cache of loaded indexes
@dataclass
class _Cached:
    signature: tuple
    index: object
    lock: threading.Lock


_cache: dict[str, _Cached] = {}
_cache_lock = threading.Lock()


def _signature(path: Path) -> tuple | None:
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    return (st.st_ino, st.st_mtime_ns, st.st_size)


def _cached(kb_name: str) -> _Cached | None:
    """The loaded index of a knowledge base, reloaded when another process (or thread) replaced the file."""
    path = index_path(kb_name)
    sig = _signature(path)
    with _cache_lock:
        entry = _cache.get(kb_name)
        if sig is None:
            _cache.pop(kb_name, None)
            return None
        if entry is None or entry.signature != sig:
            index = _load(path)
            index.prepare()  # warm the search tables now, not inside the first question
            entry = _Cached(sig, index, threading.Lock())
            _cache[kb_name] = entry
        return entry


def _forget(kb_name: str) -> None:
    with _cache_lock:
        _cache.pop(kb_name, None)


# ------------------------------------------------------------------ writes
def _lock(conn, kb_name: str) -> None:
    conn.execute(
        "SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
        (get_settings().vector_lock_namespace, kb_name),
    )


def replace_source(kb_name: str, source: str, chunks: list[dict], vectors) -> int:
    """Replace every chunk of one document (`source`) with `chunks` ([{text, page, chunk, run_id}]) and their
    embeddings. An empty list just removes the document."""
    if len(chunks) != len(vectors):
        raise VectorStoreError(f"{len(chunks)} chunks but {len(vectors)} vectors")
    prepared = _prepare(vectors) if len(chunks) else None
    path = index_path(kb_name)
    with get_conn() as conn, conn.transaction():
        _lock(conn, kb_name)
        index = _load(path) if path.exists() else _new_index()
        if prepared is not None:
            _check_dim(index, prepared, kb_name)
        old = conn.execute(
            "DELETE FROM rag_chunks WHERE kb_name = %s AND source = %s RETURNING id", (kb_name, source)
        ).fetchall()
        for row in old:
            index.remove(int(row["id"]))
        if prepared is not None:
            if len(index) == 0 and index.dim is not None and index.dim != prepared.shape[1]:
                index = _new_index()  # empty index of an earlier embedding model: start again
            ids = [
                r["id"]
                for r in conn.execute(
                    "SELECT nextval(pg_get_serial_sequence('rag_chunks', 'id')) AS id FROM generate_series(1, %s)",
                    (len(chunks),),
                ).fetchall()
            ]
            with conn.cursor() as cur:
                cur.executemany(
                    """INSERT INTO rag_chunks (id, kb_name, source, page, chunk, run_id, text)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                    [
                        (i, kb_name, source, c.get("page"), c.get("chunk", n), c.get("run_id"), c["text"])
                        for n, (i, c) in enumerate(zip(ids, chunks, strict=True))
                    ],
                )
            index.add_with_ids(prepared, np.asarray(ids, dtype=np.uint64))
        if len(index):
            _save(index, path)
        else:
            path.unlink(missing_ok=True)
        # The rows commit when this block ends, after the file is in place. If that commit fails, the document's
        # new vectors point at rows that do not exist (search skips them): upload the document again.
    _forget(kb_name)
    return len(chunks)


def drop(kb_name: str) -> None:
    """Delete a knowledge base's index file and chunks."""
    with get_conn() as conn, conn.transaction():
        _lock(conn, kb_name)
        conn.execute("DELETE FROM rag_chunks WHERE kb_name = %s", (kb_name,))
        index_path(kb_name).unlink(missing_ok=True)
    _forget(kb_name)


# ------------------------------------------------------------------ reads
def search(kb_name: str, vector, k: int) -> list[dict]:
    """Top-k chunks by cosine similarity: [{id, text, source, page, chunk, score}], best first."""
    entry = _cached(kb_name)
    if entry is None:
        return []
    query = _prepare([vector])
    with entry.lock:
        total = len(entry.index)
        if total == 0:
            return []
        _check_dim(entry.index, query, kb_name)
        scores, ids = entry.index.search(query, k=min(k, total))
    hits = [(int(i), float(s)) for i, s in zip(ids[0], scores[0], strict=True)]
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, source, page, chunk, text FROM rag_chunks WHERE kb_name = %s AND id = ANY(%s)",
            (kb_name, [i for i, _ in hits]),
        ).fetchall()
    by_id = {r["id"]: r for r in rows}
    out = []
    for i, score in hits:
        row = by_id.get(i)
        if row is None:  # a write that did not finish; its rows were rolled back
            continue
        out.append({**row, "score": round(min(max(score, -1.0), 1.0), 4)})
    return out


def documents(kb_name: str) -> dict[str, int]:
    """{document name: number of chunks}"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT source, count(*) AS n FROM rag_chunks WHERE kb_name = %s GROUP BY source ORDER BY source",
            (kb_name,),
        ).fetchall()
    return {r["source"]: r["n"] for r in rows}


def count(kb_name: str) -> int:
    entry = _cached(kb_name)
    return len(entry.index) if entry else 0


def status() -> str:
    """One line for health checks: library, bit width, folder, number of indexes. Raises if the folder is not
    usable."""
    path = folder()
    path.mkdir(parents=True, exist_ok=True)
    probe = path / f".probe.{os.getpid()}.{uuid.uuid4().hex[:6]}"
    probe.write_bytes(b"")
    probe.unlink()
    n = sum(1 for _ in path.glob(f"*{get_settings().vector_index_suffix}"))
    return f"TurboQuant (turbovec {library_version()}), {_bits()}-bit, {path.resolve()}: {n} index file(s)"
