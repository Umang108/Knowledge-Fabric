"""The work each background job does. Every function takes job_id first (see jobs.submit)."""

import json
from pathlib import Path

from app import extraction, jobs, kb, loader, rag
from app.db import get_conn
from app.graphstore import GraphStore
from app.tabular import read_table_file

EXTRACT_STEPS = extraction.STEPS
BUILD_STEPS = ["Validate rows against the approved schema", "Create nodes and relationships", "Verify graph"]
ADD_GRAPH_STEPS = ["Match file to the existing schema", "Validate rows", "Write to the graph"]
RAG_STEPS = ["Read documents", "Chunk text", "Embed and store in ChromaDB", "LLM scans for PII"]


# ------------------------------------------------------------------ PII rows
def sync_graph_pii(kb_name: str, schema: dict, actor: str = "system") -> None:
    """Make kb_pii_fields match the schema's PII list (graph KBs). Dismissed entries stay, with status
    'dismissed', so a reviewer's decision that a column is not PII is recorded."""
    targets = extraction.pii_targets(schema)
    with get_conn() as conn:
        keep = [(t["node_label"], t["property_name"]) for t in targets]
        existing = conn.execute(
            """SELECT id, node_label, property_name FROM kb_pii_fields
                                   WHERE kb_name = %s AND source_document IS NULL""",
            (kb_name,),
        ).fetchall()
        for row in existing:
            if (row["node_label"], row["property_name"]) not in keep:
                conn.execute("DELETE FROM kb_pii_fields WHERE id = %s", (row["id"],))
        for t in targets:
            conn.execute(
                """INSERT INTO kb_pii_fields (kb_name, node_label, property_name, pii_category, sensitivity,
                                              confidence, reason, detected_by, status, modified_by)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (kb_name, node_label, property_name) WHERE source_document IS NULL
                   DO UPDATE SET pii_category = EXCLUDED.pii_category, sensitivity = EXCLUDED.sensitivity,
                                 confidence = EXCLUDED.confidence, reason = EXCLUDED.reason,
                                 detected_by = EXCLUDED.detected_by, status = EXCLUDED.status,
                                 modified_by = EXCLUDED.modified_by""",
                (
                    kb_name,
                    t["node_label"],
                    t["property_name"],
                    t["category"],
                    t["sensitivity"],
                    t["confidence"],
                    f"column '{t['column']}' in sheet '{t['sheet']}': {t.get('reason', '')}"[:500],
                    t["detected_by"],
                    t.get("status", "detected"),
                    actor,
                ),
            )


def save_doc_pii(kb_name: str, filename: str, items: list[dict]) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM kb_pii_fields WHERE kb_name = %s AND source_document = %s", (kb_name, filename))
        for i in items:
            conn.execute(
                """INSERT INTO kb_pii_fields (kb_name, source_document, pii_category, sensitivity, confidence,
                                              occurrences, reason, detected_by, modified_by)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'system')""",
                (
                    kb_name,
                    filename,
                    i["pii_category"],
                    i["sensitivity"],
                    i["confidence"],
                    i["occurrences"],
                    i["reason"],
                    i["detected_by"],
                ),
            )


# ------------------------------------------------------------------ pipeline runs
def start_run(kb_name: str, run_type: str, source_file: str, started_by: str, job_id: int | None) -> int:
    with get_conn() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (kb_name,))
        run_no = conn.execute(
            "SELECT coalesce(max(run_no), 0) + 1 AS n FROM pipeline_runs WHERE kb_name = %s", (kb_name,)
        ).fetchone()["n"]
        return conn.execute(
            """INSERT INTO pipeline_runs (kb_name, run_no, job_id, run_type, source_file, started_by, modified_by)
               VALUES (%s, %s, %s, %s, %s, %s, 'system') RETURNING id""",
            (kb_name, run_no, job_id, run_type, source_file, started_by),
        ).fetchone()["id"]


def finish_run(run_id: int, status: str, **fields) -> None:
    if "rejected_report" in fields:
        fields["rejected_report"] = json.dumps(fields["rejected_report"], default=str)
    sets = ", ".join(f"{k} = %({k})s" for k in fields)
    with get_conn() as conn:
        conn.execute(
            f"""UPDATE pipeline_runs SET status = %(status)s, finished_at = now(), modified_by = 'system'
                         {', ' + sets if sets else ''} WHERE id = %(id)s""",
            {"status": status, "id": run_id, **fields},
        )


# ------------------------------------------------------------------ graph: extract -> review
def graph_extract(job_id: int, kb_name: str, file_path: str, file_name: str) -> None:
    jobs.step(job_id, 0, "running")
    sheets = read_table_file(file_path, file_name)
    jobs.step(job_id, 0, "done", f"{len(sheets)} sheets, {sum(len(s.rows) for s in sheets):,} rows")
    schema = extraction.extract(
        sheets,
        file_name,
        step=lambda i, st, d: jobs.step(job_id, i, st, d),
        cancelled=lambda: jobs.is_cancelled(job_id),
    )
    if not schema:
        raise jobs.JobCancelled()
    schema["source_path"] = file_path
    kb.set_status(kb_name, "awaiting_review", None, draft_schema=json.dumps(schema, default=str))
    sync_graph_pii(kb_name, schema)


# ------------------------------------------------------------------ graph: build after Submit
def graph_build(job_id: int, kb_name: str, user_id: str) -> None:
    cat = kb.get_catalog(kb_name)
    schema = cat["approved_schema"]
    store = GraphStore(kb_name)
    run_id = start_run(kb_name, "initial_build", schema["source_file"], user_id, job_id)
    try:
        jobs.step(job_id, 0, "running")
        sheets = read_table_file(schema["source_path"], schema["source_file"])
        plan = loader.plan_load(schema, sheets, loader.existing_keys_for(store, schema))
        jobs.step(job_id, 0, "done", f"{plan.rows_loaded:,} rows accepted, {len(plan.rejected)} rejected")
        jobs.step(job_id, 1, "running")
        totals = loader.execute_plan(
            store,
            schema,
            plan,
            progress=lambda f, m: jobs.step(job_id, 1, "running", m, progress=33 + 60 * f),
            cancelled=lambda: jobs.is_cancelled(job_id),
        )
        jobs.step(
            job_id,
            1,
            "done",
            f"{totals.get('nodes_created', 0):,} nodes, " f"{totals.get('relationships_created', 0):,} relationships",
        )
        jobs.step(job_id, 2, "running")
        counts = store.counts()
        jobs.step(job_id, 2, "done", f"{sum(counts['nodes'].values()):,} nodes in graph")
        finish_run(
            run_id,
            "completed",
            rows_total=plan.rows_total,
            rows_loaded=plan.rows_loaded,
            rows_rejected=len(plan.rejected),
            nodes_created=totals.get("nodes_created", 0),
            relationships_created=totals.get("relationships_created", 0),
            rejected_report=plan.rejected,
            summary=_summary(plan, totals, initial=True),
        )
        kb.set_status(kb_name, "ready", None)
    except Exception as exc:
        finish_run(run_id, "failed", summary=str(exc)[:500])
        raise


def _summary(plan, totals, initial=False) -> str:
    text = (
        f"{'Created from the reviewed schema. ' if initial else ''}Added {totals.get('nodes_created', 0):,} nodes "
        f"and {totals.get('relationships_created', 0):,} relationships from {plan.rows_loaded:,} rows."
    )
    if plan.rejected_by_reason:
        text += (
            f" {sum(plan.rejected_by_reason.values())} rows rejected ("
            + ", ".join(f"{n} {r}" for r, n in plan.rejected_by_reason.most_common())
            + ")."
        )
    if plan.links_skipped:
        text += (
            f" {sum(plan.links_skipped.values()):,} links left out because the record they point to isn't in the "
            "data (" + ", ".join(f"{n} {t}" for t, n in plan.links_skipped.most_common()) + ")."
        )
    if plan.unmatched_sheets:
        text += f" Skipped sheets that don't match the schema: {', '.join(plan.unmatched_sheets)}."
    return text


# ------------------------------------------------------------------ graph: add data
def check_graph_file(schema: dict, file_path: str, file_name: str) -> dict:
    sheets = read_table_file(file_path, file_name)
    matches, unmatched = loader.match_sheets(schema, sheets)
    return {
        "matched": bool(matches),
        "sheets": [
            {
                "file_sheet": m.file_sheet.name,
                "schema_sheet": m.schema_sheet,
                "rows": len(m.file_sheet.rows),
                "columns_matched": len(m.columns),
                "columns_unused": [c for c in m.file_sheet.columns if c not in m.columns.values()],
            }
            for m in matches
        ],
        "unmatched_sheets": unmatched,
        "total_rows": sum(len(s.rows) for s in sheets),
    }


def graph_add_data(
    job_id: int, kb_name: str, file_path: str, file_name: str, user_id: str, merge_existing: bool, skip_invalid: bool
) -> None:
    schema = kb.get_catalog(kb_name)["approved_schema"]
    store = GraphStore(kb_name)
    run_id = start_run(kb_name, "add_data", file_name, user_id, job_id)
    try:
        jobs.step(job_id, 0, "running")
        sheets = read_table_file(file_path, file_name)
        existing = loader.existing_keys_for(store, schema)
        plan = loader.plan_load(schema, sheets, existing, merge_existing=merge_existing)
        jobs.step(job_id, 0, "done", ", ".join(f"{m.file_sheet.name} -> {m.schema_sheet}" for m in plan.matches))
        jobs.step(job_id, 1, "done", f"{plan.rows_loaded:,} rows accepted, {len(plan.rejected)} rejected")
        if plan.rejected and not skip_invalid:
            finish_run(
                run_id,
                "failed",
                rows_total=plan.rows_total,
                rows_loaded=0,
                rows_rejected=len(plan.rejected),
                rejected_report=plan.rejected,
                summary=f"Nothing written: {len(plan.rejected)} rows don't fit the schema and "
                "skipping bad rows was turned off.",
            )
            raise loader.LoadError(f"{len(plan.rejected)} rows don't fit the schema; nothing was written")
        jobs.step(job_id, 2, "running")
        totals = loader.execute_plan(
            store,
            schema,
            plan,
            progress=lambda f, m: jobs.step(job_id, 2, "running", m, progress=66 + 33 * f),
            cancelled=lambda: jobs.is_cancelled(job_id),
        )
        jobs.step(
            job_id,
            2,
            "done",
            f"{totals.get('nodes_created', 0):,} nodes, "
            f"{totals.get('relationships_created', 0):,} relationships added",
        )
        finish_run(
            run_id,
            "completed",
            rows_total=plan.rows_total,
            rows_loaded=plan.rows_loaded,
            rows_rejected=len(plan.rejected),
            nodes_created=totals.get("nodes_created", 0),
            relationships_created=totals.get("relationships_created", 0),
            rejected_report=plan.rejected,
            summary=_summary(plan, totals),
        )
    except loader.LoadError:
        raise
    except Exception as exc:
        finish_run(run_id, "failed", summary=str(exc)[:500])
        raise


# ------------------------------------------------------------------ RAG ingest (create and add data)
def rag_ingest(
    job_id: int, kb_name: str, files: list[tuple[str, str]], user_id: str, run_type: str, pii_llm: bool = True
) -> None:
    """files: [(path, original name)]. Each file gets its own pipeline run (upload chip status)."""
    runs = {name: start_run(kb_name, run_type, name, user_id, job_id) for _, name in files}
    parsed, failures = {}, []
    jobs.step(job_id, 0, "running")
    for path, name in files:
        try:
            parsed[name] = rag.extract_text(Path(path), name)
        except rag.DocumentError as exc:
            failures.append(f"{name}: {exc}")
            finish_run(runs[name], "failed", summary=str(exc))
    jobs.step(job_id, 0, "done", f"{len(parsed)} of {len(files)} documents readable")

    jobs.step(job_id, 1, "running")
    chunks = {name: rag.chunk(pages) for name, pages in parsed.items()}
    jobs.step(job_id, 1, "done", f"{sum(len(c) for c in chunks.values())} chunks")

    jobs.step(job_id, 2, "running")
    done_files = 0
    for name, cs in chunks.items():
        try:
            rag.store_chunks(
                kb_name,
                name,
                cs,
                runs[name],
                progress=lambda f, name=name, done=done_files: jobs.step(
                    job_id, 2, "running", f"{name}: {f:.0%}", progress=50 + 25 * (done + f) / max(len(chunks), 1)
                ),
            )
            finish_run(
                runs[name],
                "completed",
                chunks_added=len(cs),
                rows_total=len(cs),
                rows_loaded=len(cs),
                rows_rejected=0,
                summary=f"Indexed {len(cs)} chunks.",
            )
        except Exception as exc:
            failures.append(f"{name}: {exc}")
            finish_run(runs[name], "failed", summary=str(exc)[:500])
            chunks[name] = []
        done_files += 1
        if jobs.is_cancelled(job_id):
            raise jobs.JobCancelled()
    jobs.step(job_id, 2, "done", f"{done_files} documents embedded")

    jobs.step(job_id, 3, "running")
    for name, cs in chunks.items():
        if cs:
            save_doc_pii(kb_name, name, rag.scan_pii(name, cs, use_llm=pii_llm))
    jobs.step(job_id, 3, "done", "")
    if run_type == "rag_ingest" and kb.get_catalog(kb_name)["status"] != "ready":
        if not any(chunks.values()):
            kb.set_status(kb_name, "failed", "; ".join(failures) or "No readable documents")
            raise rag.DocumentError("; ".join(failures) or "No readable documents")
        kb.set_status(kb_name, "ready", "; ".join(failures) or None)
    if failures and not any(chunks.values()):
        raise rag.DocumentError("; ".join(failures))
