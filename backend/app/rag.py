"""RAG documents: parse PDF/DOCX/TXT, chunk, embed into a per-KB TurboQuant index (app/vectorstore.py), PII scan."""

import logging
import re
from collections import Counter
from pathlib import Path

from docx import Document as DocxDocument
from langchain_text_splitters import RecursiveCharacterTextSplitter
from pypdf import PdfReader

from app import nist_pii, rules, vectorstore
from app.config import get_settings
from app.llm import ask_json, get_embeddings
from app.observability import observe_span

log = logging.getLogger(__name__)

SUPPORTED = (".pdf", ".docx", ".txt", ".md")


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
    settings = get_settings()
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=settings.rag_chunk_size,
        chunk_overlap=settings.rag_chunk_overlap,
        separators=["\n\n", "\n", ". ", " ", ""],
    )
    out = []
    for page, text in pages:
        for piece in splitter.split_text(text):
            if piece.strip():
                out.append({"text": piece.strip(), "page": page})
    return out


# ------------------------------------------------------------------ vector store (TurboQuant, app/vectorstore.py)
class ServiceError(RuntimeError):
    """A dependency of RAG (vector store or embedding model) failed; the message says which and what to check."""


def _vector_store_error(what: str, exc: Exception) -> ServiceError:
    s = get_settings()
    msg = str(exc).strip() or type(exc).__name__
    where = f"TurboQuant index in {s.vector_dir}"
    if isinstance(exc, vectorstore.VectorStoreError):
        return ServiceError(f"Vector store ({where}) failed while {what}: {msg}")
    if isinstance(exc, ModuleNotFoundError) and "turbovec" in msg:
        hint = "the turbovec package is not installed; run pip install -r requirements.txt in the backend folder"
    elif "no space" in msg.lower():
        hint = "the disk holding VECTOR_DIR is full"
    elif isinstance(exc, OSError) and exc.errno is not None and not isinstance(exc, TimeoutError | ConnectionError):
        hint = (
            "the backend cannot write to VECTOR_DIR. Set VECTOR_DIR in .env to a folder the backend user can write, "
            "and use the same folder for the MCP server"
        )
    elif "rag_chunks" in msg:
        hint = "the rag_chunks table is missing; restart the backend (or run python -m app.cli migrate)"
    else:
        hint = "run python -m app.cli doctor for details"
    return ServiceError(f"Vector store ({where}) failed while {what}: {type(exc).__name__}: {msg}. Hint: {hint}.")


def _embedding_error(exc: Exception) -> ServiceError:
    s = get_settings()
    msg = str(exc).strip() or type(exc).__name__
    if s.llm_provider == "azure":
        hint = (
            f"check AZURE_OPENAI_ENDPOINT (now '{s.azure_openai_endpoint}'), the API key and "
            f"AZURE_OPENAI_EMBED_DEPLOYMENT (now '{s.azure_openai_embed_deployment}'), which must be the name of an "
            "embedding deployment in that Azure OpenAI resource"
        )
    else:
        hint = f"check OLLAMA_BASE_URL and run `ollama pull {s.ollama_embed_model}`"
    return ServiceError(f"Embedding model ({s.llm_provider}) failed: {type(exc).__name__}: {msg[:300]}. Hint: {hint}.")


def _store(what: str, fn):
    try:
        return fn()
    except ServiceError:
        raise
    except Exception as exc:  # noqa: BLE001 - reported with a hint
        raise _vector_store_error(what, exc) from exc


def _embed(fn):
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        raise _embedding_error(exc) from exc


def drop_index(kb_name: str) -> None:
    """Remove a knowledge base's vectors and chunks (nothing happens if it has none)."""
    _store("deleting the index", lambda: vectorstore.drop(kb_name))


def store_chunks(kb_name: str, filename: str, chunks: list[dict], run_id: int | None = None, progress=None) -> int:
    """Embed the chunks, then replace any earlier version of this document with them in one step."""
    emb = get_embeddings()
    batch = get_settings().rag_embedding_batch_size
    vectors: list[list[float]] = []
    for i in range(0, len(chunks), batch):
        part = chunks[i : i + batch]
        # the file name is embedded with the text so questions naming a document find it
        vectors += _embed(lambda part=part: emb.embed_documents([f"{filename}\n{c['text']}" for c in part]))
        if progress:
            progress(min(i + batch, len(chunks)) / len(chunks))
    rows = [{"text": c["text"], "page": c["page"], "chunk": n, "run_id": run_id} for n, c in enumerate(chunks)]
    return _store("saving chunks", lambda: vectorstore.replace_source(kb_name, filename, rows, vectors))


def retrieve(kb_name: str, question: str, k: int | None = None) -> list[dict]:
    if k is None:
        k = get_settings().rag_retrieval_top_k
    with observe_span("TurboQuant Retrieval", {"knowledge_base": kb_name, "top_k": k}):
        if _store("opening the index", lambda: vectorstore.count(kb_name)) == 0:
            return []
        vector = _embed(lambda: get_embeddings().embed_query(question))
        hits = _store("searching", lambda: vectorstore.search(kb_name, vector, k))
        return [
            {"text": h["text"], "source": h["source"], "page": h["page"] or None, "score": h["score"]} for h in hits
        ]


def documents(kb_name: str) -> dict[str, int]:
    return _store("listing documents", lambda: vectorstore.documents(kb_name))


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
    for cat, n in counts.items():
        if n <= 0:
            continue
        by_rules = cat != "person_name"
        level = nist_pii.document_level(cat)
        out.append(
            {
                "source_document": filename,
                "pii_category": cat,
                "occurrences": n,
                "sensitivity": nist_pii.SENSITIVITY[level],
                "nist_identifier": nist_pii.CATEGORIES[cat][1],
                "nist_impact": level,
                "nist_factors": [
                    f"data field sensitivity: {nist_pii.CATEGORIES[cat][2]} ({nist_pii.CATEGORIES[cat][3]})",
                    "context: mentioned in a document, linked to the people it names",
                    f"quantity: {n} occurrence(s)",
                ],
                "confidence": 0.95 if by_rules else 0.7,
                "detected_by": "rules" if by_rules else names_by,
                "reason": f"{n} {cat.replace('_', ' ')} occurrence(s) found",
            }
        )
    return out
