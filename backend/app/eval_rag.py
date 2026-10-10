"""RAGAS evaluation of RAG and graph knowledge bases.

    uv sync                                                     # from the backend folder
    python -m app.cli eval-ragas --kb retail_policies_rag --testset eval/testsets/retail_policies_rag.csv
    python -m app.cli eval-ragas --kb retail_supply_chain_kg --testset my_graph_questions.csv

For every question in the test set the app answers exactly as it does in the chat (TurboQuant retrieval for RAG
or the read-only, KB-scoped graph flow), then RAGAS (https://docs.ragas.io) scores the answer, using the app's own
model as the judge unless
RAGAS_JUDGE_MODEL names another one:

  faithfulness         every claim in the answer is supported by the retrieved passages (no hallucination)
  answer_relevancy     the answer addresses the question
  context_precision    the useful passages are ranked first (judged against the reference answer when there is one)
  context_recall       the passages contain everything the reference answer needs          (needs a reference)
  factual_correctness  the answer agrees with the reference answer                         (needs a reference)

Every score is 0-1, higher is better. Results are saved three ways:
  - a folder per run in RAGAS_REPORT_DIR (default backend/reports/ragas): results.csv (opens in Excel),
    results.jsonl (everything, including the passages) and summary.json (averages, settings, change since last run)
  - Postgres: rag_eval_runs (one row per run) and rag_eval_results (one row per question)
  - Langfuse, when tracing is on: every answer is a "RAG Evaluation" trace carrying its metric scores
"""

from __future__ import annotations

import asyncio
import contextlib
import csv
import datetime as dt
import json
import logging
import math
import os
import time
from pathlib import Path

os.environ.setdefault("RAGAS_DO_NOT_TRACK", "true")  # RAGAS sends usage analytics to its authors unless told not to

from app import chat, rag  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import get_conn  # noqa: E402
from app.observability import _client as langfuse_client  # noqa: E402
from app.observability import llm_context, observe_workflow, record_output  # noqa: E402

log = logging.getLogger(__name__)

# name -> (what it measures, needs a reference answer)
METRICS: dict[str, tuple[str, bool]] = {
    "faithfulness": ("claims in the answer are supported by the retrieved passages", False),
    "answer_relevancy": ("the answer addresses the question", False),
    "context_precision": ("useful passages are ranked first", False),
    "context_recall": ("passages contain what the reference answer needs", True),
    "factual_correctness": ("the answer agrees with the reference answer", True),
}
QUESTION_KEYS = ("question", "user_input", "q", "query")
REFERENCE_KEYS = ("reference", "ground_truth", "expected_answer", "reference_answer", "answer")
METRIC_TIMEOUT = get_settings().ragas_metric_timeout_seconds


class EvalError(RuntimeError):
    """A problem the user can fix (missing package, bad test set, unknown knowledge base)."""


# ------------------------------------------------------------------ test set
def load_testset(path: str | Path) -> list[dict]:
    """CSV (columns question, reference[, id]), JSONL or JSON (a list, or {"questions": [...]}).
    The reference answer is optional; without it only faithfulness, answer_relevancy and context_precision run."""
    path = Path(path)
    if not path.is_file():
        raise EvalError(f"Test set not found: {path}")
    try:
        if path.suffix.lower() == ".csv":
            with path.open(encoding="utf-8-sig", newline="") as f:
                rows = list(csv.DictReader(f))
        elif path.suffix.lower() == ".jsonl":
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        elif path.suffix.lower() == ".json":
            data = json.loads(path.read_text(encoding="utf-8"))
            rows = data.get("questions", []) if isinstance(data, dict) else data
        else:
            raise EvalError(f"{path.name}: use a .csv, .jsonl or .json test set")
    except (json.JSONDecodeError, UnicodeDecodeError, csv.Error) as exc:
        raise EvalError(f"{path.name} could not be read: {exc}") from exc

    def first(row: dict, keys) -> str:
        for k in keys:
            v = row.get(k)
            if isinstance(v, list):
                v = "; ".join(str(x) for x in v)
            if v is not None and str(v).strip():
                return str(v).strip()
        return ""

    items, seen = [], set()
    for n, raw in enumerate(rows, 1):
        if not isinstance(raw, dict):
            raise EvalError(f"{path.name} item {n} is not an object")
        row = {str(k).strip().lower(): v for k, v in raw.items() if k is not None}
        question = first(row, QUESTION_KEYS)
        if not question:
            if not any(str(v or "").strip() for v in row.values()):
                continue  # blank line
            raise EvalError(f"{path.name} row {n}: no question (expected columns: question, reference)")
        qid = first(row, ("id",)) or str(n)
        if qid in seen:
            raise EvalError(f"{path.name}: id {qid} is used twice")
        seen.add(qid)
        items.append({"id": qid, "question": question, "reference": first(row, REFERENCE_KEYS) or None})
    if not items:
        raise EvalError(f"{path.name} has no questions")
    return items


# ------------------------------------------------------------------ judge model
def _ragas():
    try:
        import ragas
        from ragas.embeddings import embedding_factory
        from ragas.llms import llm_factory
        from ragas.metrics import collections
    except ImportError as exc:
        raise EvalError(
            f"RAGAS is not installed ({exc}). From the backend folder run: uv sync"
        ) from exc
    return ragas, llm_factory, embedding_factory, collections


def judge_names(model: str | None = None, embed_model: str | None = None) -> tuple[str, str]:
    s = get_settings()
    if s.llm_provider == "azure":
        default_chat, default_embed = s.azure_openai_chat_deployment, s.azure_openai_embed_deployment
    else:
        default_chat, default_embed = s.ollama_chat_model, s.ollama_embed_model
    return (
        model or s.ragas_judge_model or default_chat,
        embed_model or s.ragas_judge_embed_model or default_embed,
    )


def _judge_client():
    """An OpenAI-compatible async client for the configured provider (Azure OpenAI, or Ollama's /v1 API)."""
    s = get_settings()
    if s.llm_provider == "azure":
        from openai import AsyncAzureOpenAI

        return AsyncAzureOpenAI(
            azure_endpoint=s.azure_openai_endpoint,
            api_key=s.azure_openai_api_key,
            api_version=s.azure_openai_api_version,
            timeout=s.llm_timeout_seconds,
        )
    from openai import AsyncOpenAI

    return AsyncOpenAI(base_url=s.ollama_base_url.rstrip("/") + "/v1", api_key="ollama", timeout=s.llm_timeout_seconds)


def build_metrics(names: list[str], model: str, embed_model: str) -> dict:
    """{metric name: {"plain": metric, "with_reference": metric or None}}"""
    _, llm_factory, embedding_factory, C = _ragas()
    client = _judge_client()
    llm = llm_factory(
        model,
        provider="openai",
        client=client,
        max_tokens=get_settings().ragas_judge_max_tokens,
    )
    out = {}
    for name in names:
        if name == "faithfulness":
            out[name] = {"plain": C.Faithfulness(llm=llm)}
        elif name == "answer_relevancy":
            embeddings = embedding_factory("openai", model=embed_model, client=client)
            out[name] = {"plain": C.AnswerRelevancy(llm=llm, embeddings=embeddings)}
        elif name == "context_precision":
            out[name] = {
                "plain": C.ContextPrecisionWithoutReference(llm=llm),
                "with_reference": C.ContextPrecisionWithReference(llm=llm),
            }
        elif name == "context_recall":
            out[name] = {"with_reference": C.ContextRecall(llm=llm)}
        elif name == "factual_correctness":
            out[name] = {"with_reference": C.FactualCorrectness(llm=llm)}
        else:
            raise EvalError(f"Unknown metric {name}; choose from {', '.join(METRICS)}")
    return out


# ------------------------------------------------------------------ 1. answer every question like the chat does
def _check_kb(kb_name: str) -> dict:
    with get_conn() as conn:
        row = conn.execute(
            """SELECT kb_name, kb_type, status, approved_schema, approved_at
               FROM kb_catalog WHERE lower(kb_name) = lower(%s)""",
            (kb_name,),
        ).fetchone()
    if not row:
        with get_conn() as conn:
            available = conn.execute("SELECT kb_name FROM kb_catalog ORDER BY kb_name").fetchall()
        names = ", ".join(r["kb_name"] for r in available) or "none"
        raise EvalError(f"No knowledge base named {kb_name}. Available knowledge bases: {names}")
    if row["status"] != "ready":
        raise EvalError(f"{row['kb_name']} is {row['status']}, not ready for evaluation")
    if row["kb_type"] == "rag" and not rag.documents(row["kb_name"]):
        raise EvalError(
            f"{row['kb_name']} has no documents in the TurboQuant vector store; ingest the documents again"
        )
    if row["kb_type"] == "graph" and not row["approved_schema"]:
        raise EvalError(f"{row['kb_name']} has no approved graph schema")
    return dict(row)


def _graph_context(result: dict) -> list[str]:
    """Pair graph rows with the query that explains what each returned value represents."""
    rows = result.get("rows") or []
    if not rows:
        return []
    query = result.get("cypher") or ""
    evidence = (
        f"Graph query:\n{query}\n\n"
        f"Returned rows:\n{json.dumps(rows, default=str, sort_keys=True)}"
    )
    return [evidence]


def answer(kb: dict, item: dict, user: str, session: str) -> dict:
    """The app's answer to one question, with retrieved TurboQuant passages or graph rows."""
    kb_name = kb["kb_name"]
    started = time.perf_counter()
    trace_id, error = None, None
    result: dict = {}
    try:
        with (
            llm_context(user, session, kb_name),
            observe_workflow(
                "RAGAS Evaluation",
                {"question": item["question"], "knowledge_base": kb_name, "kb_type": kb["kb_type"]},
            ) as obs,
        ):
            if kb["kb_type"] == "rag":
                result = chat.rag_answer(kb_name, item["question"], [], include_contexts=True)
            else:
                from app.graphstore import GraphStore

                result = chat.graph_answer(
                    GraphStore(kb_name),
                    kb["approved_schema"],
                    kb["approved_at"],
                    item["question"],
                    [],
                )
            if obs is not None:
                trace_id = getattr(obs, "trace_id", None)
                record_output(obs, result.get("answer"))
    except Exception as exc:  # noqa: BLE001 - one failing question must not stop the run
        error = f"{type(exc).__name__}: {exc}"[:500]
        log.warning("question %s failed: %s", item["id"], error)
    return {
        **item,
        "answer": result.get("answer", ""),
        "contexts": result.get("contexts", []) if kb["kb_type"] == "rag" else _graph_context(result),
        "sources": (
            [{"source": s["source"], "page": s["page"], "score": s["score"]} for s in result.get("sources", [])]
            if kb["kb_type"] == "rag"
            else [{"cypher": result.get("cypher", ""), "path": result.get("path", [])}]
        ),
        "kb_type": kb["kb_type"],
        "cypher": result.get("cypher", ""),
        "rows": result.get("rows", []),
        "blocked": bool(result.get("blocked")),
        "guardrails": result.get("guardrails", []),
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "trace_id": trace_id,
        "answer_error": error,
    }


# ------------------------------------------------------------------ 2. score with RAGAS
def _inputs(name: str, row: dict) -> tuple[str, dict] | tuple[None, str]:
    """(variant, keyword arguments) for one metric, or (None, why it is skipped)."""
    q, a, ctx, ref = row["question"], row["answer"], row["contexts"], row["reference"]
    if row["answer_error"]:
        return None, "the app failed to answer"
    if row["blocked"]:
        return None, "the question was blocked by a guardrail"
    if name in ("faithfulness", "context_precision", "context_recall") and not ctx:
        return None, "no passages were retrieved"
    if METRICS[name][1] and not ref:
        return None, "the test set has no reference answer for this question"
    if name == "faithfulness":
        return "plain", {"user_input": q, "response": a, "retrieved_contexts": ctx}
    if name == "answer_relevancy":
        return "plain", {"user_input": q, "response": a}
    if name == "context_precision":
        if ref:
            return "with_reference", {"user_input": q, "reference": ref, "retrieved_contexts": ctx}
        return "plain", {"user_input": q, "response": a, "retrieved_contexts": ctx}
    if name == "context_recall":
        return "with_reference", {"user_input": q, "retrieved_contexts": ctx, "reference": ref}
    return "with_reference", {"response": a, "reference": ref}  # factual_correctness


def _number(value) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else round(f, 4)


async def _score_all(rows: list[dict], metrics: dict, concurrency: int, progress) -> None:
    sem = asyncio.Semaphore(max(1, concurrency))
    done = 0
    total = len(rows) * len(metrics)

    async def one(row, name, variants):
        nonlocal done
        variant, kwargs = _inputs(name, row)
        if variant is None:
            row["scores"][name] = None
            row["skipped"][name] = kwargs
        else:
            async with sem:
                try:
                    result = await asyncio.wait_for(
                        variants[variant].ascore(**kwargs),
                        METRIC_TIMEOUT,
                    )
                    row["scores"][name] = _number(getattr(result, "value", result))
                    if row["scores"][name] is None:
                        row["errors"][name] = f"no score returned ({getattr(result, 'reason', '') or 'NaN'})"
                except Exception as exc:  # noqa: BLE001 - recorded per question and metric
                    row["scores"][name] = None
                    row["errors"][name] = f"{type(exc).__name__}: {exc}"[:500]
        done += 1
        progress(done, total)

    for row in rows:
        row.setdefault("scores", {})
        row.setdefault("errors", {})
        row.setdefault("skipped", {})
    await asyncio.gather(*(one(row, name, v) for row in rows for name, v in metrics.items()))


# ------------------------------------------------------------------ 3. save
def summarise(rows: list[dict], names: list[str]) -> dict:
    out = {}
    for name in names:
        values = [r["scores"][name] for r in rows if r["scores"].get(name) is not None]
        out[name] = {
            "mean": round(sum(values) / len(values), 4) if values else None,
            "scored": len(values),
            "errors": sum(1 for r in rows if name in r["errors"]),
            "skipped": sum(1 for r in rows if name in r["skipped"]),
        }
    return out


def _previous(kb_name: str, testset: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            """SELECT id, metrics, started_at FROM rag_eval_runs WHERE kb_name = %s AND testset = %s
               ORDER BY started_at DESC LIMIT 1""",
            (kb_name, testset),
        ).fetchone()
    return dict(row) if row else None


def _save_files(folder: Path, rows: list[dict], summary: dict, names: list[str]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    with (folder / "results.jsonl").open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, default=str, ensure_ascii=False) + "\n")
    with (folder / "results.csv").open("w", encoding="utf-8-sig", newline="") as f:  # BOM: Excel reads UTF-8
        w = csv.writer(f)
        w.writerow(
            [
                "id",
                "question",
                "reference",
                "answer",
                *names,
                "sources",
                "passages",
                "latency_ms",
                "notes",
                "trace_id",
            ]
        )
        for r in rows:
            if r["kb_type"] == "rag":
                sources = "; ".join(
                    f"{s['source']}" + (f" p.{s['page']}" if s["page"] else "") for s in r["sources"]
                )
            else:
                sources = r.get("cypher", "")
            notes = [f"{k}: {v}" for k, v in {**r["skipped"], **r["errors"]}.items()]
            if r["answer_error"]:
                notes.insert(0, f"answer failed: {r['answer_error']}")
            w.writerow(
                [r["id"], r["question"], r["reference"] or "", r["answer"]]
                + ["" if r["scores"].get(n) is None else r["scores"][n] for n in names]
                + [sources, len(r["contexts"]), r["latency_ms"], " | ".join(notes), r["trace_id"] or ""]
            )


def _save_db(kb_name: str, testset: str, rows: list[dict], summary: dict, folder: Path, user: str) -> int:
    with get_conn() as conn, conn.transaction():
        run_id = conn.execute(
            """INSERT INTO rag_eval_runs
                   (kb_name, kb_type, testset, questions, metrics, config, report_dir, started_at, created_by)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (
                kb_name,
                summary["config"]["evaluation_type"],
                testset,
                len(rows),
                json.dumps({k: v["mean"] for k, v in summary["metrics"].items()}),
                json.dumps(summary["config"], default=str),
                str(folder),
                summary["started_at"],
                user,
            ),
        ).fetchone()["id"]
        with conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO rag_eval_results (run_id, question_id, question, reference, answer, contexts, sources,
                                                 scores, errors, latency_ms, trace_id)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                [
                    (
                        run_id,
                        r["id"],
                        r["question"],
                        r["reference"],
                        r["answer"],
                        json.dumps(r["contexts"]),
                        json.dumps(r["sources"]),
                        json.dumps(r["scores"]),
                        json.dumps({**r["errors"], **({"answer": r["answer_error"]} if r["answer_error"] else {})}),
                        r["latency_ms"],
                        r["trace_id"],
                    )
                    for r in rows
                ],
            )
    return run_id


def _send_langfuse_scores(rows: list[dict], run_id: int | None) -> int:
    client = langfuse_client()
    if client is None:
        return 0
    sent = 0
    for r in rows:
        if not r["trace_id"]:
            continue
        for name, value in r["scores"].items():
            if value is None:
                continue
            try:
                client.create_score(
                    trace_id=r["trace_id"],
                    name=f"ragas_{name}",
                    value=value,
                    data_type="NUMERIC",
                    comment=f"RAGAS run {run_id}",
                )
                sent += 1
            except Exception:  # noqa: BLE001 - tracing must never fail the evaluation
                log.warning("Langfuse: could not send a score", exc_info=True)
                return sent
    with contextlib.suppress(Exception):
        client.flush()
    return sent


# ------------------------------------------------------------------ run
def run(
    kb_name: str,
    testset_path: str | Path,
    metrics: list[str] | None = None,
    out_dir: str | Path | None = None,
    judge_model: str | None = None,
    judge_embed_model: str | None = None,
    limit: int | None = None,
    concurrency: int | None = None,
    user: str = "ragas-eval",
    echo=print,
) -> dict:
    s = get_settings()
    names = metrics or list(METRICS)
    unknown = [n for n in names if n not in METRICS]
    if unknown:
        raise EvalError(f"Unknown metric(s) {', '.join(unknown)}; choose from {', '.join(METRICS)}")
    ragas = _ragas()[0]
    kb = _check_kb(kb_name)
    kb_name = kb["kb_name"]
    concurrency = concurrency or get_settings().ragas_default_concurrency
    items = load_testset(testset_path)[: limit or None]
    testset = Path(testset_path).name
    model, embed_model = judge_names(judge_model, judge_embed_model)
    built = build_metrics(names, model, embed_model)  # fails fast on a bad judge setup, before any answering
    started = dt.datetime.now(dt.UTC)
    stamp = started.strftime("%Y%m%d-%H%M%S")
    session = f"ragas-{kb_name}-{stamp}"

    echo(f"Evaluating {kb_name} with {len(items)} question(s) from {testset}; judge: {s.llm_provider} / {model}")
    rows = []
    for n, item in enumerate(items, 1):
        rows.append(answer(kb, item, user, session))
        echo(f"  answered {n}/{len(items)}: {item['question'][:70]}")

    last = [0.0]

    def progress(done, total):
        if done == total or time.monotonic() - last[0] > 5:
            last[0] = time.monotonic()
            echo(f"  scoring: {done}/{total} question-metric pairs done")

    asyncio.run(_score_all(rows, built, concurrency, progress))

    finished = dt.datetime.now(dt.UTC)
    folder = Path(out_dir or s.ragas_report_dir) / f"{kb_name}_{stamp}"
    previous = _previous(kb_name, testset)
    summary = {
        "knowledge_base": kb_name,
        "testset": testset,
        "questions": len(rows),
        "answered": sum(1 for r in rows if not r["answer_error"]),
        "blocked": sum(1 for r in rows if r["blocked"]),
        "with_reference": sum(1 for r in rows if r["reference"]),
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "average_latency_ms": round(sum(r["latency_ms"] for r in rows) / len(rows)),
        "metrics": summarise(rows, names),
        "config": {
            "ragas_version": ragas.__version__,
            "llm_provider": s.llm_provider,
            "chat_model": s.azure_openai_chat_deployment if s.llm_provider == "azure" else s.ollama_chat_model,
            "embedding_model": s.azure_openai_embed_deployment if s.llm_provider == "azure" else s.ollama_embed_model,
            "judge_model": model,
            "judge_embedding_model": embed_model,
            "top_k": s.rag_retrieval_top_k,
            "retrieval_backend": (
                f"TurboQuant {s.turboquant_bits}-bit" if kb["kb_type"] == "rag" else "Neo4j read-only graph"
            ),
            "evaluation_type": kb["kb_type"],
            "guardrail_mask_pii": s.guardrail_mask_pii,
            "guardrail_min_relevance": s.guardrail_min_relevance,
        },
    }
    if previous:
        before = previous["metrics"] or {}
        summary["previous_run"] = {
            "id": previous["id"],
            "started_at": previous["started_at"],
            "change": {
                k: round(v["mean"] - before[k], 4)
                for k, v in summary["metrics"].items()
                if v["mean"] is not None and before.get(k) is not None
            },
        }
    summary["report_dir"] = str(folder.resolve())
    _save_files(folder, rows, summary, names)  # first, so a database problem cannot lose the results
    try:
        summary["run_id"] = _save_db(kb_name, testset, rows, summary, folder, user)
    except Exception as exc:  # noqa: BLE001 - the files are already saved
        summary["run_id"] = None
        summary["database_error"] = f"{type(exc).__name__}: {exc}"[:300]
        log.warning("RAGAS results not saved to Postgres: %s", summary["database_error"])
    summary["langfuse_scores"] = _send_langfuse_scores(rows, summary["run_id"])
    (folder / "summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    _print_summary(summary, echo)
    return summary


def _print_summary(summary: dict, echo) -> None:
    echo("")
    echo(f"RAGAS results for {summary['knowledge_base']}")
    echo(f"  {'metric':<22}{'score':>7}")
    for name, m in summary["metrics"].items():
        score = "-" if m["mean"] is None else f"{m['mean']:.3f}"
        echo(f"  {name:<22}{score:>7}")
    if summary["run_id"]:
        echo(f"Saved to {summary['report_dir']} and Postgres (rag_eval_runs id {summary['run_id']}).")
    else:
        echo(f"Saved to {summary['report_dir']}. Not saved to Postgres: {summary.get('database_error')}")
    if summary.get("langfuse_scores"):
        echo(f"Sent {summary['langfuse_scores']} score(s) to Langfuse.")
