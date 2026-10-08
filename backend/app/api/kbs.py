"""Knowledge bases: create, list, review/submit, add data, runs, access, chat, jobs.

Every route that touches a KB calls kb.require_access() with the roles allowed for it:
owner-only for review/submit/access management; owner or user for add-data and chat.
"""

import json
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from app import chat, conversations, extraction, jobs, kb, nist_pii, pipelines, rag
from app import graph_schema as gs
from app.auth import CurrentUser, current_user
from app.config import get_settings
from app.db import get_conn
from app.graphstore import GraphStore
from app.observability import trace_context
from app.tabular import TabularError

router = APIRouter(prefix="/api", tags=["knowledge bases"])

GRAPH_EXT = (".csv", ".xlsx", ".xlsm")
MAX_UPLOAD_MB = 50


def _save_upload(kb_name: str, upload: UploadFile, allowed: tuple) -> tuple[str, str]:
    name = Path(upload.filename or "upload").name
    if not name.lower().endswith(allowed):
        raise HTTPException(415, f"{name}: unsupported file type (allowed: {', '.join(allowed)})")
    folder = Path(get_settings().upload_dir) / kb_name
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"{uuid.uuid4().hex[:8]}_{name}"
    with dest.open("wb") as f:
        shutil.copyfileobj(upload.file, f)
    if dest.stat().st_size > MAX_UPLOAD_MB * 1024 * 1024:
        dest.unlink()
        raise HTTPException(413, f"{name} is larger than {MAX_UPLOAD_MB} MB")
    if dest.stat().st_size == 0:
        dest.unlink()
        raise HTTPException(422, f"{name} is empty")
    return str(dest), name


def _job_view(j: dict) -> dict:
    return {
        k: j[k]
        for k in (
            "id",
            "kb_name",
            "job_type",
            "status",
            "source_file",
            "steps",
            "progress",
            "error",
            "started_by",
            "created_at",
            "started_at",
            "finished_at",
        )
    }


# ------------------------------------------------------------------ list / create
@router.get("/kbs")
def list_kbs(user: CurrentUser = Depends(current_user)):
    return kb.list_for_user(user.user_id)


@router.post("/kbs", status_code=201)
def create_kb(
    kb_name: str = Form(...),
    kb_type: str = Form(...),
    domain: str = Form(...),
    sub_domain: str = Form(...),
    session_id: str | None = Form(None),
    files: list[UploadFile] = File(...),
    user: CurrentUser = Depends(current_user),
):
    kb_name, domain, sub_domain = kb_name.strip(), domain.strip(), sub_domain.strip()
    if kb_type not in ("graph", "rag"):
        raise HTTPException(422, "kb_type must be graph or rag")
    if not domain or not sub_domain:
        raise HTTPException(422, "Domain and sub-domain are required")
    if kb_type == "graph" and len(files) != 1:
        raise HTTPException(422, "A knowledge graph is created from exactly one CSV or XLSX file")
    for f in files:  # check types before creating anything
        allowed = GRAPH_EXT if kb_type == "graph" else rag.SUPPORTED
        if not (f.filename or "").lower().endswith(allowed):
            raise HTTPException(415, f"{f.filename}: unsupported file type (allowed: {', '.join(allowed)})")
    storage = GraphStore(kb_name).storage_ref if kb_type == "graph" else f"turboquant:{kb_name}"
    kb.create(user, kb_name, kb_type, domain, sub_domain, storage)
    saved = [_save_upload(kb_name, f, GRAPH_EXT if kb_type == "graph" else rag.SUPPORTED) for f in files]
    fail = lambda msg: kb.set_status(kb_name, "failed", msg[:500])  # noqa: E731
    if kb_type == "graph":
        path, name = saved[0]
        job_id = jobs.create(kb_name, "graph_extract", pipelines.EXTRACT_STEPS, user.user_id, name)
        jobs.submit(
            job_id, pipelines.graph_extract, kb_name, path, name, on_error=fail,
            user_id=user.user_id, session_id=session_id or user.session_id, operation="kg_extraction",
        )
    else:
        job_id = jobs.create(kb_name, "rag_ingest", pipelines.RAG_STEPS, user.user_id, ", ".join(n for _, n in saved))
        jobs.submit(
            job_id, pipelines.rag_ingest, kb_name, saved, user.user_id, "rag_ingest",
            on_error=fail, user_id=user.user_id, session_id=session_id or user.session_id, operation="rag_ingest",
        )
    return {"kb_name": kb_name, "job_id": job_id}


@router.get("/kbs/{kb_name}")
def get_kb(kb_name: str, user: CurrentUser = Depends(current_user)):
    cat = kb.require_access(user, kb_name)
    out = {
        k: cat[k]
        for k in (
            "kb_name",
            "kb_type",
            "domain",
            "sub_domain",
            "owner_id",
            "status",
            "status_detail",
            "role",
            "created_at",
            "updated_at",
            "approved_at",
            "approved_by",
        )
    }
    if cat["kb_type"] == "graph" and cat["status"] == "ready":
        counts = GraphStore(kb_name).counts()
        schema = cat["approved_schema"]
        out["stats"] = {
            "node_types": len(schema["nodes"]),
            "relationship_types": len(schema["relationships"]),
            "entities": sum(counts["nodes"].values()),
            "relationships": sum(counts["relationships"].values()),
            "by_label": counts["nodes"],
            "by_type": counts["relationships"],
        }
    elif cat["kb_type"] == "rag":
        docs = rag.documents(kb_name) if cat["status"] in ("ready", "ingesting") else {}
        out["stats"] = {"documents": len(docs), "chunks": sum(docs.values())}
    with get_conn() as conn:
        job = conn.execute("SELECT * FROM jobs WHERE kb_name = %s ORDER BY id DESC LIMIT 1", (kb_name,)).fetchone()
    out["last_job"] = _job_view(job) if job else None
    return out


@router.delete("/kbs/{kb_name}")
def delete_kb(kb_name: str, user: CurrentUser = Depends(current_user)):
    cat = kb.require_access(user, kb_name, roles=("owner",))
    if cat["status"] in ("extracting", "building", "ingesting"):
        raise HTTPException(409, "Wait for the current job to finish before deleting this knowledge base")
    if cat["kb_type"] == "graph":
        GraphStore(kb_name).drop()
    else:
        rag.drop_index(kb_name)
    removed = kb.delete(kb_name, user.user_id)
    shutil.rmtree(Path(get_settings().upload_dir) / kb_name, ignore_errors=True)
    return {"deleted": removed["kb_name"]}


@router.post("/kbs/{kb_name}/extract")
def rerun_extraction(
    kb_name: str,
    session_id: str | None = None,
    user: CurrentUser = Depends(current_user),
):
    cat = kb.require_access(user, kb_name, roles=("owner",))
    if cat["kb_type"] != "graph" or cat["status"] not in ("failed", "awaiting_review"):
        raise HTTPException(409, "Extraction can only be re-run for a graph that is failed or awaiting review")
    with get_conn() as conn:
        job = conn.execute(
            """SELECT source_file FROM jobs WHERE kb_name = %s AND job_type = 'graph_extract'
                              ORDER BY id DESC LIMIT 1""",
            (kb_name,),
        ).fetchone()
    folder = Path(get_settings().upload_dir) / kb_name
    matches = sorted(folder.glob(f"*_{job['source_file']}")) if job else []
    if not matches:
        raise HTTPException(409, "The original upload is no longer available; create the knowledge base again")
    kb.set_status(kb_name, "extracting", None, actor=user.user_id)
    job_id = jobs.create(kb_name, "graph_extract", pipelines.EXTRACT_STEPS, user.user_id, job["source_file"])
    jobs.submit(
        job_id,
        pipelines.graph_extract,
        kb_name,
        str(matches[-1]),
        job["source_file"],
        on_error=lambda m: kb.set_status(kb_name, "failed", m[:500]),
        user_id=user.user_id,
        session_id=session_id or user.session_id,
        operation="kg_extraction",
    )
    return {"job_id": job_id}


# ------------------------------------------------------------------ jobs
@router.get("/jobs/{job_id}")
def get_job(job_id: int, user: CurrentUser = Depends(current_user)):
    j = jobs.get(job_id)
    if not j:
        raise HTTPException(404, "Job not found")
    kb.require_access(user, j["kb_name"])
    return _job_view(j)


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: int, user: CurrentUser = Depends(current_user)):
    j = jobs.get(job_id)
    if not j:
        raise HTTPException(404, "Job not found")
    cat = kb.require_access(user, j["kb_name"])
    if cat["role"] != "owner" and j["started_by"] != user.user_id:
        raise kb.AccessDenied("Only the owner or the person who started the job can cancel it")
    if not jobs.cancel(job_id, user.user_id):
        raise HTTPException(409, "The job has already finished")
    if j["job_type"] == "graph_extract":
        kb.set_status(j["kb_name"], "failed", "Extraction cancelled", actor=user.user_id)
    return {"cancelled": True}


# ------------------------------------------------------------------ review (owner only)
class SchemaBody(BaseModel):
    schema_: dict = Field(alias="schema")


def _review_payload(cat: dict, schema: dict) -> dict:
    return {
        "kb_name": cat["kb_name"],
        "status": cat["status"],
        "schema": schema,
        "cypher": gs.preview(schema),
        "summary": gs.summary(schema),
        "pii": nist_pii.assess(schema.get("pii", []), schema.get("sheets", {})),  # older drafts get NIST fields too
        "pii_categories": extraction.PII_CATEGORIES,
        "pii_catalogue": nist_pii.catalogue(),
    }


def _merge_edit(saved: dict, edited: dict) -> dict:
    """Users edit nodes/relationships/pii; sheet profiles and source path always come from the server copy."""
    merged = {
        **saved,
        "nodes": edited.get("nodes", saved["nodes"]),
        "relationships": edited.get("relationships", saved["relationships"]),
        "pii": edited.get("pii", saved.get("pii", [])),
    }
    try:
        cleaned = gs.clean(merged)
        cleaned["pii"] = extraction.clean_pii(merged["pii"], saved.get("sheets", {}))
        return cleaned
    except gs.SchemaError as exc:
        raise HTTPException(422, {"message": "The schema has problems", "errors": exc.errors}) from exc
    except (KeyError, TypeError) as exc:
        raise HTTPException(422, {"message": "Malformed schema", "errors": [f"missing field {exc}"]}) from exc


@router.get("/kbs/{kb_name}/review")
def get_review(kb_name: str, user: CurrentUser = Depends(current_user)):
    cat = kb.require_access(user, kb_name, roles=("owner",))
    if not cat["draft_schema"]:
        raise HTTPException(409, "Extraction hasn't finished yet")
    return _review_payload(cat, cat["draft_schema"])


@router.post("/kbs/{kb_name}/review/preview")
def preview_review(kb_name: str, body: SchemaBody, user: CurrentUser = Depends(current_user)):
    cat = kb.require_access(user, kb_name, roles=("owner",))
    if not cat["draft_schema"]:
        raise HTTPException(409, "Extraction hasn't finished yet")
    return _review_payload(cat, _merge_edit(cat["draft_schema"], body.schema_))


@router.put("/kbs/{kb_name}/review")
def save_review(kb_name: str, body: SchemaBody, user: CurrentUser = Depends(current_user)):
    cat = kb.require_access(user, kb_name, roles=("owner",))
    if cat["status"] != "awaiting_review":
        raise HTTPException(409, f"The knowledge base is {cat['status']}, not awaiting review")
    schema = _merge_edit(cat["draft_schema"], body.schema_)
    kb.set_status(kb_name, "awaiting_review", None, actor=user.user_id, draft_schema=json.dumps(schema, default=str))
    pipelines.sync_graph_pii(kb_name, schema, actor=user.user_id)
    return _review_payload(cat, schema)


@router.post("/kbs/{kb_name}/submit")
def submit_review(kb_name: str, body: SchemaBody, user: CurrentUser = Depends(current_user)):
    cat = kb.require_access(user, kb_name, roles=("owner",))
    if cat["status"] != "awaiting_review":
        raise HTTPException(409, f"The knowledge base is {cat['status']}, not awaiting review")
    schema = _merge_edit(cat["draft_schema"], body.schema_)
    cypher = "\n".join(gs.preview(schema))
    with get_conn() as conn:
        conn.execute(
            """UPDATE kb_catalog SET status = 'building', status_detail = NULL, draft_schema = %s,
                        approved_schema = %s, approved_cypher = %s, approved_by = %s, approved_at = now(),
                        modified_by = %s WHERE kb_name = %s""",
            (
                json.dumps(schema, default=str),
                json.dumps(schema, default=str),
                cypher,
                user.user_id,
                user.user_id,
                kb_name,
            ),
        )
    pipelines.sync_graph_pii(kb_name, schema, actor=user.user_id)
    job_id = jobs.create(kb_name, "graph_build", pipelines.BUILD_STEPS, user.user_id, schema["source_file"])
    jobs.submit(
        job_id,
        pipelines.graph_build,
        kb_name,
        user.user_id,
        on_error=lambda m: kb.set_status(kb_name, "failed", f"Build failed: {m}"[:500]),
        user_id=user.user_id,
        session_id=user.session_id,
        operation="kg_build",
    )
    return {"job_id": job_id}


@router.get("/kbs/{kb_name}/pii")
def get_pii(kb_name: str, user: CurrentUser = Depends(current_user)):
    kb.require_access(user, kb_name)
    with get_conn() as conn:
        return conn.execute(
            """SELECT id, node_label, property_name, source_document, pii_category, sensitivity,
                                      confidence, occurrences, reason, detected_by, status, created_at, updated_at,
                                      modified_by
                               FROM kb_pii_fields WHERE kb_name = %s ORDER BY id""",
            (kb_name,),
        ).fetchall()


# ------------------------------------------------------------------ access (owner only)
class GrantBody(BaseModel):
    user_id: str = Field(min_length=1, max_length=128)
    role: str = "user"


@router.get("/kbs/{kb_name}/access")
def get_access(kb_name: str, user: CurrentUser = Depends(current_user)):
    kb.require_access(user, kb_name, roles=("owner",))
    return {"people": kb.people(kb_name), "log": kb.access_log(kb_name)}


@router.post("/kbs/{kb_name}/access", status_code=201)
def grant_access(kb_name: str, body: GrantBody, user: CurrentUser = Depends(current_user)):
    if body.role != "user":
        raise HTTPException(422, "Only the 'user' role can be granted; each knowledge base has one owner")
    return kb.grant(user, kb_name, body.user_id)


@router.delete("/kbs/{kb_name}/access/{user_id}")
def revoke_access(kb_name: str, user_id: str, user: CurrentUser = Depends(current_user)):
    kb.revoke(user, kb_name, user_id)
    return {"revoked": user_id}


@router.get("/users/{user_id}")
def lookup_user(user_id: str, user: CurrentUser = Depends(current_user)):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT user_id, display_name FROM users WHERE user_id = %s AND is_active", (user_id,)
        ).fetchone()
    if not row:
        raise HTTPException(404, "No such user")
    return row


# ------------------------------------------------------------------ add data (owner or user)
def _require_ready(cat: dict):
    if cat["status"] != "ready":
        raise HTTPException(409, f"The knowledge base is {cat['status']}; data can be added once it is ready")


@router.post("/kbs/{kb_name}/add-data/check")
def check_add_data(kb_name: str, file: UploadFile = File(...), user: CurrentUser = Depends(current_user)):
    cat = kb.require_access(user, kb_name)
    _require_ready(cat)
    if cat["kb_type"] == "rag":
        name = Path(file.filename or "").name
        return {"matched": name.lower().endswith(rag.SUPPORTED), "kind": "rag", "file": name}
    path, name = _save_upload(kb_name, file, GRAPH_EXT)
    try:
        return {"kind": "graph", "file": name, **pipelines.check_graph_file(cat["approved_schema"], path, name)}
    except TabularError as exc:
        raise HTTPException(422, str(exc)) from exc
    finally:
        Path(path).unlink(missing_ok=True)


@router.post("/kbs/{kb_name}/add-data", status_code=202)
def add_data(
    kb_name: str,
    files: list[UploadFile] = File(...),
    merge_existing: bool = Form(True),
    skip_invalid: bool = Form(True),
    session_id: str | None = Form(None),
    user: CurrentUser = Depends(current_user),
):
    cat = kb.require_access(user, kb_name)
    _require_ready(cat)
    if cat["kb_type"] == "rag":
        saved = [_save_upload(kb_name, f, rag.SUPPORTED) for f in files]
        job_id = jobs.create(kb_name, "add_data", pipelines.RAG_STEPS, user.user_id, ", ".join(n for _, n in saved))
        jobs.submit(
            job_id, pipelines.rag_ingest, kb_name, saved, user.user_id, "add_data",
            user_id=user.user_id, session_id=session_id or user.session_id, operation="add_data",
        )
        return {"job_id": job_id}
    if len(files) != 1:
        raise HTTPException(422, "Add one CSV or XLSX file at a time")
    path, name = _save_upload(kb_name, files[0], GRAPH_EXT)
    try:
        check = pipelines.check_graph_file(cat["approved_schema"], path, name)
    except TabularError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not check["matched"]:
        raise HTTPException(422, "No sheet in this file matches the knowledge graph's schema")
    job_id = jobs.create(kb_name, "add_data", pipelines.ADD_GRAPH_STEPS, user.user_id, name)
    jobs.submit(
        job_id, pipelines.graph_add_data, kb_name, path, name, user.user_id, merge_existing, skip_invalid,
        user_id=user.user_id, session_id=session_id or user.session_id, operation="add_data",
    )
    return {"job_id": job_id}


@router.get("/kbs/{kb_name}/runs")
def list_runs(kb_name: str, user: CurrentUser = Depends(current_user)):
    kb.require_access(user, kb_name)
    with get_conn() as conn:
        return conn.execute(
            """SELECT r.id, r.run_no, r.run_type, r.source_file, r.status, r.rows_total, r.rows_loaded,
                      r.rows_rejected, r.nodes_created, r.relationships_created, r.chunks_added, r.summary,
                      r.started_by, r.started_at, r.finished_at, j.progress, j.id AS job_id
               FROM pipeline_runs r LEFT JOIN jobs j ON j.id = r.job_id
               WHERE r.kb_name = %s ORDER BY r.run_no DESC""",
            (kb_name,),
        ).fetchall()


@router.get("/kbs/{kb_name}/runs/{run_id}/report")
def run_report(kb_name: str, run_id: int, user: CurrentUser = Depends(current_user)):
    kb.require_access(user, kb_name)
    with get_conn() as conn:
        row = conn.execute(
            """SELECT run_no, source_file, summary, rejected_report FROM pipeline_runs
                              WHERE id = %s AND kb_name = %s""",
            (run_id, kb_name),
        ).fetchone()
    if not row:
        raise HTTPException(404, "Run not found")
    return {**row, "rejected_report": row["rejected_report"] or []}


# ------------------------------------------------------------------ chat (owner or user)
class ChatBody(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    conversation_id: int | None = None  # continue a saved conversation; omitted = start a new one
    history: list[dict] = []  # only used by API clients that don't use saved conversations
    session_id: str | None = None


@router.post("/kbs/{kb_name}/chat")
def chat_with_kb(kb_name: str, body: ChatBody, user: CurrentUser = Depends(current_user)):
    cat = kb.require_access(user, kb_name)
    if cat["status"] != "ready":
        raise HTTPException(409, f"The knowledge base is {cat['status']}, not ready for chat")
    question = body.question.strip()
    if body.conversation_id:
        conv = conversations.get(user, body.conversation_id)
        if conv["kb_name"] != kb_name:
            raise HTTPException(422, "That conversation belongs to another knowledge base")
        history = conversations.history(conv["id"])
    else:
        conv = conversations.create(user, kb_name, question)
        history = [{k: str(h.get(k, ""))[:2000] for k in ("question", "answer", "cypher")} for h in body.history[-5:]]
    operation = "chat" if cat["kb_type"] == "graph" else "rag_chat"
    # the conversation is the Langfuse session, so all turns of one chat are grouped
    with trace_context(user.user_id, body.session_id or f"chat-{conv['id']}", operation, kb_name):
        if cat["kb_type"] == "graph":
            result = {
                "kind": "graph",
                **chat.graph_answer(GraphStore(kb_name), cat["approved_schema"], cat["approved_at"], question, history),
            }
        else:
            try:
                result = {"kind": "rag", **chat.rag_answer(kb_name, question, history)}
            except rag.ServiceError as exc:
                raise HTTPException(503, str(exc)) from None
    conversations.add_message(user, conv["id"], question, result)
    return {"conversation_id": conv["id"], "title": conv["title"], **result}
