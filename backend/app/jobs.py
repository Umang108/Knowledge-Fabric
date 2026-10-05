"""Background jobs with step-level progress (screen 3, upload chips, pipeline runs).

A small thread pool runs jobs in-process; state lives in the jobs table so any request can read
it. Jobs interrupted by a restart are marked failed on startup.
"""

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

from app.db import get_conn

log = logging.getLogger(__name__)
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="job")
_local = threading.local()


class JobCancelled(Exception):
    pass


def create(kb_name: str, job_type: str, steps: list[str], started_by: str, source_file: str | None = None) -> int:
    with get_conn() as conn:
        return conn.execute(
            """INSERT INTO jobs (kb_name, job_type, source_file, steps, started_by, modified_by)
               VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
            (
                kb_name,
                job_type,
                source_file,
                json.dumps([{"name": s, "status": "waiting", "detail": ""} for s in steps]),
                started_by,
                started_by,
            ),
        ).fetchone()["id"]


def get(job_id: int) -> dict | None:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM jobs WHERE id = %s", (job_id,)).fetchone()


@contextmanager
def steps_after(job_id: int, offset: int):
    """Run a pipeline whose step numbers start at 0 after `offset` steps of its own (e.g. a connector's
    "Fetch from ServiceNow" step before the usual extraction steps). Jobs run on one thread each."""
    offsets = getattr(_local, "offsets", {})
    _local.offsets = {**offsets, job_id: offset}
    try:
        yield
    finally:
        _local.offsets = offsets


def step(job_id: int, index: int, status: str, detail: str = "", progress: float | None = None) -> None:
    """Mark step `index` as waiting|running|done|failed; progress defaults to completed steps."""
    index += getattr(_local, "offsets", {}).get(job_id, 0)
    with get_conn() as conn:
        job = conn.execute("SELECT steps, status FROM jobs WHERE id = %s FOR UPDATE", (job_id,)).fetchone()
        steps = job["steps"]
        steps[index].update({"status": status, "detail": detail})
        if progress is None:
            done = sum(s["status"] == "done" for s in steps)
            progress = 100 * (done + (0.5 if status == "running" else 0)) / len(steps)
        conn.execute(
            "UPDATE jobs SET steps = %s, progress = %s, modified_by = 'system' WHERE id = %s",
            (json.dumps(steps), round(min(progress, 100), 2), job_id),
        )
        if job["status"] == "cancelled":
            raise JobCancelled()


def is_cancelled(job_id: int) -> bool:
    j = get(job_id)
    return bool(j and j["status"] == "cancelled")


def cancel(job_id: int, actor: str) -> bool:
    with get_conn() as conn:
        return (
            conn.execute(
                """UPDATE jobs SET status = 'cancelled', finished_at = now(), modified_by = %s
                               WHERE id = %s AND status IN ('queued', 'running')""",
                (actor, job_id),
            ).rowcount
            > 0
        )


def submit(job_id: int, fn, *args, on_error=None, **kwargs) -> None:
    """Run fn(job_id, *args) in the background, recording start/finish/failure."""

    def run():
        with get_conn() as conn:
            started = conn.execute(
                """UPDATE jobs SET status = 'running', started_at = now(), modified_by = 'system'
                                      WHERE id = %s AND status = 'queued'""",
                (job_id,),
            ).rowcount
        if not started:
            return
        try:
            fn(job_id, *args, **kwargs)
            with get_conn() as conn:
                conn.execute(
                    """UPDATE jobs SET status = 'succeeded', progress = 100, finished_at = now(),
                                modified_by = 'system' WHERE id = %s AND status = 'running'""",
                    (job_id,),
                )
        except JobCancelled:
            log.info("job %s cancelled", job_id)
            if on_error:
                on_error("Cancelled")
        except Exception as exc:
            log.exception("job %s failed", job_id)
            message = str(exc) if isinstance(exc, ValueError) else f"{type(exc).__name__}: {exc}"
            with get_conn() as conn:
                conn.execute(
                    """UPDATE jobs SET status = 'failed', error = %s, finished_at = now(),
                                modified_by = 'system' WHERE id = %s AND status = 'running'""",
                    (message[:2000], job_id),
                )
            if on_error:
                on_error(message)

    _executor.submit(run)


def recover_interrupted() -> None:
    with get_conn() as conn:
        conn.execute("""UPDATE jobs SET status = 'failed', error = 'Interrupted by a server restart',
                        finished_at = now(), modified_by = 'system' WHERE status IN ('queued', 'running')""")
        conn.execute("""UPDATE kb_catalog SET status = 'failed', status_detail = 'Interrupted by a server restart',
                        modified_by = 'system' WHERE status IN ('extracting', 'building', 'ingesting')""")
        conn.execute("""UPDATE pipeline_runs SET status = 'failed', summary = 'Interrupted by a server restart',
                        finished_at = now(), modified_by = 'system' WHERE status = 'running'""")


def wait(job_id: int, timeout: float = 600) -> dict:
    """For tests and scripts: block until the job finishes."""
    import time

    end = time.time() + timeout
    while time.time() < end:
        j = get(job_id)
        if j["status"] in ("succeeded", "failed", "cancelled"):
            return j
        time.sleep(0.2)
    raise TimeoutError(f"job {job_id} still running")
