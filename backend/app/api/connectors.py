"""Connections to SAP / ServiceNow, and knowledge bases built (or refreshed) from them.

A connection belongs to the person who saved it (their credentials); they use it to create a KB they own,
or to add data to any KB they may add data to.
"""

import re

import anyio
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app import jobs, kb, pipelines
from app.auth import CurrentUser, current_user
from app.connectors import KINDS, ConnectorError, Dataset, store
from app.connectors import pipeline as cp
from app.db import get_conn
from app.graphstore import GraphStore

router = APIRouter(prefix="/api", tags=["connectors"])

SINCE = re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?$")


class ConnectionBody(BaseModel):
    name: str | None = None
    kind: str | None = None
    base_url: str | None = None
    auth_type: str | None = None
    username: str | None = None
    secret: str | None = Field(None, description="password or client secret; leave empty to keep the saved one")
    options: dict = {}


class DatasetsBody(BaseModel):
    connection_id: int
    datasets: list[dict] = Field(min_length=1, max_length=25)


class FromConnectionBody(DatasetsBody):
    kb_name: str
    kb_type: str = "graph"
    domain: str
    sub_domain: str


class AddFromConnectionBody(DatasetsBody):
    since: str | None = None
    merge_existing: bool = True
    skip_invalid: bool = True


class PreviewBody(BaseModel):
    dataset: dict


def _datasets(row: dict, raw: list[dict]) -> list[dict]:
    """Validate the requested datasets against the connector's rules; returns them as plain dicts."""
    out = []
    try:
        with store.open_connector(row) as conn:
            for d in raw:
                ds = Dataset.from_dict(d)
                if not ds.source:
                    raise ConnectorError("Every dataset needs a source table / entity set")
                conn.validate_dataset(ds)
                out.append(ds.to_dict())
    except ConnectorError as exc:
        raise HTTPException(422, str(exc)) from None
    return out


async def _blocking(fn, *args):
    return await anyio.to_thread.run_sync(lambda: fn(*args))


# ------------------------------------------------------------------ catalogue
@router.get("/connectors")
def connector_kinds(user: CurrentUser = Depends(current_user)):
    return {
        "kinds": [
            {"kind": k, "label": cls.label, "auth_types": ["basic", "oauth"], "presets": cls.PRESETS}
            for k, cls in KINDS.items()
        ]
    }


# ------------------------------------------------------------------ saved connections (owner only)
@router.get("/connections")
def list_connections(user: CurrentUser = Depends(current_user)):
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM connections WHERE owner_id = %s ORDER BY name", (user.user_id,)).fetchall()
    return [store.view(r) for r in rows]


@router.post("/connections", status_code=201)
def create_connection(body: ConnectionBody, user: CurrentUser = Depends(current_user)):
    return store.create(user, body.model_dump())


@router.put("/connections/{connection_id}")
def update_connection(connection_id: int, body: ConnectionBody, user: CurrentUser = Depends(current_user)):
    return store.update(user, connection_id, body.model_dump(exclude_unset=True))


@router.delete("/connections/{connection_id}")
def delete_connection(connection_id: int, user: CurrentUser = Depends(current_user)):
    store.delete(user, connection_id)
    return {"deleted": connection_id}


@router.post("/connections/{connection_id}/test")
async def test_connection(connection_id: int, user: CurrentUser = Depends(current_user)):
    row = store.get_row(user, connection_id)

    def run():
        try:
            with store.open_connector(row) as conn:
                result = conn.test()
        except ConnectorError as exc:
            result = {"ok": False, "detail": str(exc)}
        store.record_test(connection_id, result["ok"], result["detail"])
        return result

    return await _blocking(run)


@router.post("/connections/{connection_id}/preview")
async def preview_dataset(connection_id: int, body: PreviewBody, user: CurrentUser = Depends(current_user)):
    row = store.get_row(user, connection_id)
    (ds,) = _datasets(row, [body.dataset])

    def run():
        try:
            with store.open_connector(row) as conn:
                t = conn.fetch_table(Dataset.from_dict({**ds, "limit": 5}))
        except ConnectorError as exc:
            raise HTTPException(422, str(exc)) from None
        return {"name": t.name, "columns": t.columns, "rows": t.rows}

    return await _blocking(run)


# ------------------------------------------------------------------ knowledge bases from a connection
@router.post("/kbs/from-connection", status_code=201)
def create_kb_from_connection(body: FromConnectionBody, user: CurrentUser = Depends(current_user)):
    row = store.get_row(user, body.connection_id)
    kb_name, domain, sub_domain = body.kb_name.strip(), body.domain.strip(), body.sub_domain.strip()
    if body.kb_type not in ("graph", "rag"):
        raise HTTPException(422, "kb_type must be graph or rag")
    if body.kb_type == "rag" and row["kind"] != "servicenow":
        raise HTTPException(422, f"{KINDS[row['kind']].label} data builds a knowledge graph, not a RAG store")
    if not domain or not sub_domain:
        raise HTTPException(422, "Domain and sub-domain are required")
    datasets = _datasets(row, body.datasets)
    label = KINDS[row["kind"]].label
    storage = GraphStore(kb_name).storage_ref if body.kb_type == "graph" else f"chroma:{kb_name}"
    kb.create(user, kb_name, body.kb_type, domain, sub_domain, storage)
    fail = lambda msg: kb.set_status(kb_name, "failed", msg[:500])  # noqa: E731
    source = f"{label}: {', '.join(d['source'] for d in datasets)}"[:300]
    if body.kb_type == "graph":
        job_id = jobs.create(
            kb_name, "graph_extract", [cp.fetch_step(label), *pipelines.EXTRACT_STEPS], user.user_id, source
        )
        jobs.submit(job_id, cp.graph_from_connection, kb_name, row, datasets, on_error=fail)
    else:
        job_id = jobs.create(kb_name, "rag_ingest", [cp.fetch_step(label), *pipelines.RAG_STEPS], user.user_id, source)
        jobs.submit(job_id, cp.rag_from_connection, kb_name, row, datasets, user.user_id, "rag_ingest", on_error=fail)
    return {"kb_name": kb_name, "job_id": job_id}


@router.post("/kbs/{kb_name}/add-data/connection", status_code=202)
def add_data_from_connection(kb_name: str, body: AddFromConnectionBody, user: CurrentUser = Depends(current_user)):
    cat = kb.require_access(user, kb_name)
    if cat["status"] != "ready":
        raise HTTPException(409, f"The knowledge base is {cat['status']}; data can be added once it is ready")
    row = store.get_row(user, body.connection_id)
    if body.since and not SINCE.match(body.since.strip()):
        raise HTTPException(422, "since must be a date like 2026-10-01 (optionally with a time)")
    since = body.since.strip() if body.since else None
    datasets = _datasets(row, body.datasets)
    label = KINDS[row["kind"]].label
    source = f"{label}: {', '.join(d['source'] for d in datasets)}"[:300]
    if cat["kb_type"] == "rag":
        if row["kind"] != "servicenow":
            raise HTTPException(422, f"{label} data can't be added to a RAG store")
        job_id = jobs.create(kb_name, "add_data", [cp.fetch_step(label), *pipelines.RAG_STEPS], user.user_id, source)
        jobs.submit(job_id, cp.rag_from_connection, kb_name, row, datasets, user.user_id, "add_data", since)
        return {"job_id": job_id}
    job_id = jobs.create(kb_name, "add_data", [cp.fetch_step(label), *pipelines.ADD_GRAPH_STEPS], user.user_id, source)
    jobs.submit(
        job_id,
        cp.graph_add_from_connection,
        kb_name,
        row,
        datasets,
        user.user_id,
        since,
        body.merge_existing,
        body.skip_invalid,
    )
    return {"job_id": job_id}
