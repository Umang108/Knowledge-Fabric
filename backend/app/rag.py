"""RAG documents: parse PDF/DOCX/TXT, chunk, embed into a per-KB Chroma collection, PII scan."""

import contextlib
import hashlib
import logging
import re
from collections import Counter
from pathlib import Path

import chromadb
import chromadb.config
from docx import Document as DocxDocument
from langchain_text_splitters import RecursiveCharacterTextSplitter
from pypdf import PdfReader

from app import rules
from app.config import get_settings
from app.llm import ask_json, get_embeddings

log = logging.getLogger(__name__)

SUPPORTED = (".pdf", ".docx", ".txt", ".md")
CHUNK_SIZE, CHUNK_OVERLAP = 900, 150


class DocumentError(ValueError):
    pass


# ------------------------------------------------------------------ parsing
def _strip_repeated_lines(pages: list[str]) -> list[str]:
    """Drop headers/footers: lines (ignoring digits) that repeat on most pages."""
    if len(pages) < 2:
        return pages

    def norm(line: str) -> str:
        return re.sub(r"\d+", "#", line.strip())

    counts = Counter(n for p in pages for n in {norm(line) for line in p.splitlines() if line.strip()})
    repeated = {line for line, c in counts.items() if c >= max(2, 0.6 * len(pages))}
    return ["\n".join(line for line in p.splitlines() if norm(line) not in repeated) for p in pages]


def extract_text(path: Path, filename: str) -> list[tuple[int | None, str]]:
    """[(page number or None, text)]"""
    name = filename.lower()
    try:
        if name.endswith(".pdf"):
            reader = PdfReader(str(path))
            pages = _strip_repeated_lines([p.extract_text() or "" for p in reader.pages])
            out = [(i, t) for i, t in enumerate(pages, 1) if t.strip()]
            if not out:
                raise DocumentError("The PDF has no extractable text (it may be a scanned image).")
            return out
        if name.endswith(".docx"):
            doc = DocxDocument(str(path))
            parts = []
            body = doc.element.body
            for child in body.iterchildren():
                tag = child.tag.rsplit("}", 1)[-1]
                if tag == "p":
                    text = "".join(t.text or "" for t in child.iter() if t.tag.endswith("}t"))
                    if text.strip():
                        parts.append(text)
                elif tag == "tbl":
                    for tr in child.iter():
                        if tr.tag.endswith("}tr"):
                            cells = []
                            for tc in tr.iterchildren():
                                if tc.tag.endswith("}tc"):
                                    cells.append(
                                        "".join(t.text or "" for t in tc.iter() if t.tag.endswith("}t")).strip()
                                    )
                            parts.append(" | ".join(cells))
            text = "\n".join(parts)
            if not text.strip():
                raise DocumentError("The document is empty.")
            return [(None, text)]
        if name.endswith((".txt", ".md")):
            data = path.read_bytes()
            for enc in ("utf-8-sig", "cp1252"):
                try:
                    text = data.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
            text = text.replace("\r\n", "\n").replace("\r", "\n")
            if not text.strip():
                raise DocumentError("The file is empty.")
            return [(None, text)]
    except DocumentError:
        raise
    except Exception as exc:
        raise DocumentError(f"Could not read {filename}: {type(exc).__name__}") from exc
    raise DocumentError("Only PDF, DOCX and TXT files are supported for RAG.")


def chunk(pages: list[tuple[int | None, str]]) -> list[dict]:
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP, separators=["\n\n", "\n", ". ", " ", ""]
    )
    out = []
    for page, text in pages:
        for piece in splitter.split_text(text):
            if piece.strip():
                out.append({"text": piece.strip(), "page": page})
    return out


# ------------------------------------------------------------------ chroma
def chroma_client():
    s = get_settings()
    return chromadb.HttpClient(
        host=s.chroma_host, port=s.chroma_port, settings=chromadb.config.Settings(anonymized_telemetry=False)
    )


def collection(kb_name: str):
    return chroma_client().get_or_create_collection(kb_name, metadata={"hnsw:space": "cosine"})


def drop_collection(kb_name: str) -> None:
    with contextlib.suppress(Exception):  # the collection may not exist
        chroma_client().delete_collection(kb_name)


def store_chunks(kb_name: str, filename: str, chunks: list[dict], run_id: int | None = None, progress=None) -> int:
    """Replace any earlier version of this document, then add its chunks."""
    col = collection(kb_name)
    col.delete(where={"source": filename})
    emb = get_embeddings()
    doc_id = hashlib.sha1(filename.encode()).hexdigest()[:12]
    batch = 32
    for i in range(0, len(chunks), batch):
        part = chunks[i : i + batch]
        # the file name is embedded with the text so questions naming a document find it
        vectors = emb.embed_documents([f"{filename}\n{c['text']}" for c in part])
        col.add(
            ids=[f"{doc_id}-{i + j}" for j in range(len(part))],
            documents=[c["text"] for c in part],
            embeddings=vectors,
            metadatas=[
                {"source": filename, "page": c["page"] or 0, "chunk": i + j, "run_id": run_id or 0}
                for j, c in enumerate(part)
            ],
        )
        if progress:
            progress(min(i + batch, len(chunks)) / len(chunks))
    return len(chunks)


def retrieve(kb_name: str, question: str, k: int = 6) -> list[dict]:
    col = collection(kb_name)
    if col.count() == 0:
        return []
    res = col.query(
        query_embeddings=[get_embeddings().embed_query(question)],
        n_results=min(k, col.count()),
        include=["documents", "metadatas", "distances"],
    )
    return [
        {"text": d, "source": m["source"], "page": m.get("page") or None, "score": round(1 - dist, 4)}
        for d, m, dist in zip(res["documents"][0], res["metadatas"][0], res["distances"][0], strict=False)
    ]


def documents(kb_name: str) -> dict[str, int]:
    col = collection(kb_name)
    got = col.get(include=["metadatas"])
    return dict(Counter(m["source"] for m in got["metadatas"]))


# ------------------------------------------------------------------ PII scan
PII_NAMES_PROMPT = """Here are capitalised phrases found in the document "{doc}":
{candidates}

Which of them are names of individual people (a first name and a surname)? Not companies, products, places,
departments, document titles or job titles.
JSON: {{"people": [<the phrases that are people, copied exactly>]}}"""

_CANDIDATE = re.compile(r"\b([A-Z][a-z]+(?:[ \t]+[A-Z][a-z]+){1,2})\b")
_PAN_IN_TEXT = re.compile(rf"\b{rules.PAN.pattern}\b")
_AADHAAR_IN_TEXT = re.compile(r"\b\d{4}\s\d{4}\s\d{4}\b")  # spaced form only, to avoid other 12-digit numbers
_DOB = re.compile(r"\b(?:date of birth|dob|born on)\b", re.I)


def scan_pii(filename: str, chunks: list[dict], use_llm: bool = True) -> list[dict]:
    """Per-category occurrence counts for one document; values are never kept.
    Emails, phones and ID numbers are found by pattern. Person names: capitalised phrases are the
    candidates and the LLM picks the ones that are people (small models classify far better than they
    extract); its picks must be among the candidates."""
    full = "\n".join(c["text"] for c in chunks)
    counts = Counter(
        {
            "email": len(set(rules.EMAIL.findall(full))),
            "phone": len({re.sub(r"\D", "", p) for p in rules.PHONE.findall(full)}),
            "government_id": len(set(_PAN_IN_TEXT.findall(full)) | set(_AADHAAR_IN_TEXT.findall(full))),
            "date_of_birth": len(_DOB.findall(full)),
        }
    )
    names_by = "llm"
    candidates = sorted({m for m in _CANDIDATE.findall(full) if not rules.COMPANY.search(m)})
    if use_llm and candidates:
        try:
            data = ask_json(
                "You are a data-privacy auditor. Reply with one JSON object only.",
                PII_NAMES_PROMPT.format(doc=filename, candidates="\n".join(f"- {c}" for c in candidates[:150])),
            )
            people = {p for p in (data.get("people") or []) if isinstance(p, str) and p in candidates}
            counts["person_name"] = len(people)
        except Exception as exc:
            log.warning("PII name scan failed for %s: %s", filename, exc)
            names_by = "unavailable"
    out = []
    sens = {"government_id": "high", "date_of_birth": "high", "person_name": "low"}
    for cat, n in counts.items():
        if n <= 0:
            continue
        by_rules = cat != "person_name"
        out.append(
            {
                "source_document": filename,
                "pii_category": cat,
                "occurrences": n,
                "sensitivity": sens.get(cat, "medium"),
                "confidence": 0.95 if by_rules else 0.7,
                "detected_by": "rules" if by_rules else names_by,
                "reason": f"{n} {cat.replace('_', ' ')} occurrence(s) found",
            }
        )
    return out
