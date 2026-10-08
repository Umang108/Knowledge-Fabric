"""Admin commands (there is no sign-up screen).

python -m app.cli migrate
python -m app.cli create-user meera.s --name "Meera S" [--email ...] [--password ...] [--keycloak]
python -m app.cli set-password meera.s [--password ...]
python -m app.cli deactivate-user meera.s
python -m app.cli seed-demo-users          # local testing only; password test1234
python -m app.cli seed-demo-data [--llm-pii]   # the mockup knowledge bases, built from /data/samples
python -m app.cli doctor                   # check Postgres, uploads, vector store, embeddings, chat model, Neo4j
python -m app.cli eval-ragas --kb NAME --testset FILE [--metrics ...] [--min-score 0.7]   # RAGAS for RAG or graph
"""

import argparse
import getpass
import sys

from app.auth import hash_password, revoke_user_sessions
from app.db import get_conn, run_migrations

EVAL_METRICS = ("faithfulness", "answer_relevancy", "context_precision", "context_recall", "factual_correctness")

DEMO_USERS = [
    ("priya.nair", "Priya Nair"),
    ("arjun.mehta", "Arjun Mehta"),
    ("sneha.iyer", "Sneha Iyer"),
    ("karthik.r", "Karthik R"),
    ("meera.s", "Meera S"),
    ("rohan.d", "Rohan D"),
]
DEMO_PASSWORD = "test1234"


def _password(given: str | None) -> str:
    if given:
        return given
    pw = getpass.getpass("Password: ")
    if pw != getpass.getpass("Repeat password: "):
        sys.exit("Passwords do not match")
    return pw


def create_user(user_id, name, email=None, password=None, keycloak=False, actor="cli"):
    """Keycloak users get no password; creating them up front lets owners grant access before first login."""
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO users (user_id, display_name, email, password_hash, auth_source, modified_by)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (
                user_id,
                name,
                email,
                None if keycloak else hash_password(password),
                "keycloak" if keycloak else "local",
                actor,
            ),
        )


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m app.cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate")
    c = sub.add_parser("create-user")
    c.add_argument("user_id")
    c.add_argument("--name", required=True)
    c.add_argument("--email")
    c.add_argument("--password")
    c.add_argument("--keycloak", action="store_true", help="pre-provision a Keycloak user (no password)")
    sp = sub.add_parser("set-password")
    sp.add_argument("user_id")
    sp.add_argument("--password")
    d = sub.add_parser("deactivate-user")
    d.add_argument("user_id")
    sub.add_parser("seed-demo-users")
    sub.add_parser("doctor")
    ev = sub.add_parser(
        "eval-ragas",
        aliases=["eval-rag", "eval-graph", "eval-kb"],
        help="RAGAS evaluation of a TurboQuant RAG or graph knowledge base (app/eval_rag.py)",
    )
    ev.add_argument("--kb", required=True, help="RAG or graph knowledge base to evaluate")
    ev.add_argument("--testset", required=True, help="CSV / JSONL / JSON with question and reference columns")
    ev.add_argument("--metrics", help="comma-separated; default: all (" + ", ".join(EVAL_METRICS) + ")")
    ev.add_argument("--out", help="report folder (default RAGAS_REPORT_DIR, backend/reports/ragas)")
    ev.add_argument(
        "--judge-model", help="judge chat model / Azure deployment (default RAGAS_JUDGE_MODEL or the app's)"
    )
    ev.add_argument("--judge-embed-model", help="judge embedding model (default the app's)")
    ev.add_argument("--limit", type=int, help="only the first N questions")
    ev.add_argument("--concurrency", type=int, default=4, help="metric calls in parallel (default 4)")
    ev.add_argument("--user", default="ragas-eval", help="name recorded as the run's author and Langfuse user")
    ev.add_argument("--min-score", type=float, help="exit with code 1 if any metric's average is below this")
    sd = sub.add_parser("seed-demo-data")
    sd.add_argument("--samples", default="/data/samples")
    sd.add_argument("--llm-pii", action="store_true", help="use the LLM for PII detection (slow on CPU)")
    a = p.parse_args(argv)

    if a.cmd == "doctor":  # before migrations: Postgres may be the thing that's broken
        from app import doctor

        sys.exit(doctor.run())
    run_migrations()
    if a.cmd == "migrate":
        print("migrations up to date")
    elif a.cmd == "create-user":
        create_user(a.user_id, a.name, a.email, None if a.keycloak else _password(a.password), a.keycloak)
        print(f"created {a.user_id}")
    elif a.cmd == "set-password":
        with get_conn() as conn:
            n = conn.execute(
                "UPDATE users SET password_hash = %s, auth_source = 'local', modified_by = 'cli' WHERE user_id = %s",
                (hash_password(_password(a.password)), a.user_id),
            ).rowcount
        print("password updated" if n else f"no such user {a.user_id}")
    elif a.cmd == "deactivate-user":
        with get_conn() as conn:
            n = conn.execute(
                "UPDATE users SET is_active = false, modified_by = 'cli' WHERE user_id = %s", (a.user_id,)
            ).rowcount
        ended = revoke_user_sessions(a.user_id, "user deactivated", "cli") if n else 0
        print(f"deactivated, {ended} session(s) ended" if n else f"no such user {a.user_id}")
    elif a.cmd in ("eval-ragas", "eval-rag", "eval-graph", "eval-kb"):
        from app import eval_rag

        try:
            summary = eval_rag.run(
                a.kb,
                a.testset,
                metrics=[m.strip() for m in a.metrics.split(",") if m.strip()] if a.metrics else None,
                out_dir=a.out,
                judge_model=a.judge_model,
                judge_embed_model=a.judge_embed_model,
                limit=a.limit,
                concurrency=a.concurrency,
                user=a.user,
            )
        except eval_rag.EvalError as exc:
            sys.exit(str(exc))
        if a.min_score is not None:
            low = {
                k: v["mean"] for k, v in summary["metrics"].items() if v["mean"] is not None and v["mean"] < a.min_score
            }
            if low:
                sys.exit(f"Below {a.min_score}: " + ", ".join(f"{k} {v:.3f}" for k, v in low.items()))
    elif a.cmd == "seed-demo-users":
        with get_conn() as conn:
            for uid, name in DEMO_USERS:
                conn.execute(
                    """INSERT INTO users (user_id, display_name, email, password_hash, modified_by)
                       VALUES (%s, %s, %s, %s, 'seed') ON CONFLICT (user_id) DO NOTHING""",
                    (uid, name, f"{uid}@graphbase-retail.example", hash_password(DEMO_PASSWORD)),
                )
        print(f"seeded {len(DEMO_USERS)} demo users (password {DEMO_PASSWORD})")
    elif a.cmd == "seed-demo-data":
        from pathlib import Path

        from app import demo
        from app.db import close_pool

        main(["seed-demo-users"])
        demo.seed(Path(a.samples), use_llm_pii=a.llm_pii)
        close_pool()
        print("demo data ready")


if __name__ == "__main__":
    main()
