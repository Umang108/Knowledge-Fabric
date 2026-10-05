"""Hand connector data to the normal pipelines.

Graph: the tables are written to an .xlsx (one sheet per dataset) in the KB's upload folder, exactly like an
uploaded workbook, so extraction, Review, re-extraction and add-data all work unchanged.
RAG:   ServiceNow knowledge articles are written as .md documents and indexed like uploaded documents.
Each job starts with its own "Fetch from <system>" step; the existing pipeline's steps follow it.
"""

import datetime as dt
import json
import uuid
from pathlib import Path

from openpyxl import Workbook

from app import jobs, kb, pipelines
from app.config import get_settings
from app.connectors import ConnectorError, Dataset, store
from app.connectors.base import safe_sheet_name
from app.db import get_conn

EXCEL_CELL_MAX = 32_000


def _cell(v):
    if isinstance(v, (int, float, bool)) or v is None:
        return v
    s = str(v)
    return s[:EXCEL_CELL_MAX] if len(s) > EXCEL_CELL_MAX else s


def write_workbook(path: Path, tables) -> dict[str, int]:
    wb = Workbook()
    wb.remove(wb.active)
    taken, counts = set(), {}
    for t in tables:
        ws = wb.create_sheet(safe_sheet_name(t.name, taken))
        ws.append(t.columns)
        for row in t.rows:
            ws.append([_cell(row.get(c)) for c in t.columns])
        counts[ws.title] = len(t.rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return counts


def _folder(kb_name: str) -> Path:
    return Path(get_settings().upload_dir) / kb_name


def export_tables(connection_row: dict, datasets: list[Dataset], kb_name: str, since: str | None, say) -> tuple:
    """Pull every dataset and write the workbook; returns (path, file name, summary)."""
    with store.open_connector(connection_row) as conn:
        tables = []
        for ds in datasets:
            say(f"Reading {ds.source}")
            tables.append(conn.fetch_table(ds, since=since, progress=say))
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M")
    name = f"{connection_row['kind']}_{stamp}.xlsx"
    path = _folder(kb_name) / f"{uuid.uuid4().hex[:8]}_{name}"
    counts = write_workbook(path, tables)
    if not any(counts.values()):
        path.unlink(missing_ok=True)
        raise ConnectorError("The source returned no rows" + (" changed since then" if since else ""))
    return path, name, ", ".join(f"{s}: {n:,} rows" for s, n in counts.items())


def export_documents(connection_row: dict, datasets: list[Dataset], kb_name: str, since: str | None, say):
    """Pull documents (ServiceNow knowledge) and write them as .md files; returns [(path, name)]."""
    saved = []
    with store.open_connector(connection_row) as conn:
        for ds in datasets:
            say(f"Reading {ds.source}")
            for name, text in conn.fetch_documents(ds, since=since, progress=say):
                path = _folder(kb_name) / f"{uuid.uuid4().hex[:8]}_{name}"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
                saved.append((str(path), name))
    if not saved:
        raise ConnectorError("The source returned no documents" + (" changed since then" if since else ""))
    return saved


def fetch_step(label: str) -> str:
    return f"Fetch from {label}"


# ------------------------------------------------------------------ jobs (job_id first, see jobs.submit)
def graph_from_connection(job_id: int, kb_name: str, connection_row: dict, datasets: list[dict]) -> None:
    say = lambda msg: jobs.step(job_id, 0, "running", msg)  # noqa: E731
    say("Connecting")
    path, name, summary = export_tables(connection_row, [Dataset.from_dict(d) for d in datasets], kb_name, None, say)
    jobs.step(job_id, 0, "done", summary)
    with jobs.steps_after(job_id, 1):
        pipelines.graph_extract(job_id, kb_name, str(path), name)
    relax_links(kb_name)


def relax_links(kb_name: str) -> None:
    """Systems of record leave references blank (unassigned incident, top manager) or point at records
    outside the pulled tables (an inactive user). Neither should cost the row: make every relationship of a
    connector-built graph optional, and skip a link whose target isn't in the data instead of rejecting the row.
    (Uploaded spreadsheets keep the strict contract.)"""
    schema = relax_schema(kb.get_catalog(kb_name)["draft_schema"])
    with get_conn() as conn:
        conn.execute(
            "UPDATE kb_catalog SET draft_schema = %s WHERE kb_name = %s", (json.dumps(schema, default=str), kb_name)
        )


def relax_schema(schema: dict) -> dict:
    for r in schema.get("relationships", []):
        r["required"], r["on_unknown"] = False, "skip"
    return schema


def rag_from_connection(
    job_id: int,
    kb_name: str,
    connection_row: dict,
    datasets: list[dict],
    user_id: str,
    run_type: str,
    since: str | None = None,
) -> None:
    say = lambda msg: jobs.step(job_id, 0, "running", msg)  # noqa: E731
    say("Connecting")
    files = export_documents(connection_row, [Dataset.from_dict(d) for d in datasets], kb_name, since, say)
    jobs.step(job_id, 0, "done", f"{len(files)} documents")
    with jobs.steps_after(job_id, 1):
        pipelines.rag_ingest(job_id, kb_name, files, user_id, run_type)


def graph_add_from_connection(
    job_id: int,
    kb_name: str,
    connection_row: dict,
    datasets: list[dict],
    user_id: str,
    since: str | None,
    merge_existing: bool,
    skip_invalid: bool,
) -> None:
    say = lambda msg: jobs.step(job_id, 0, "running", msg)  # noqa: E731
    say("Connecting")
    path, name, summary = export_tables(connection_row, [Dataset.from_dict(d) for d in datasets], kb_name, since, say)
    jobs.step(job_id, 0, "done", summary)
    with jobs.steps_after(job_id, 1):
        pipelines.graph_add_data(job_id, kb_name, str(path), name, user_id, merge_existing, skip_invalid)
