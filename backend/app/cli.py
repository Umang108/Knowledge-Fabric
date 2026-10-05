"""Admin commands (there is no sign-up screen).

python -m app.cli migrate
python -m app.cli create-user meera.s --name "Meera S" [--email ...] [--password ...] [--keycloak]
python -m app.cli set-password meera.s [--password ...]
python -m app.cli deactivate-user meera.s
python -m app.cli seed-demo-users          # local testing only; password test1234
python -m app.cli seed-demo-data [--llm-pii]   # the mockup knowledge bases, built from /data/samples
"""

import argparse
import getpass
import sys

from app.auth import hash_password, revoke_user_sessions
from app.db import get_conn, run_migrations

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
    sd = sub.add_parser("seed-demo-data")
    sd.add_argument("--samples", default="/data/samples")
    sd.add_argument("--llm-pii", action="store_true", help="use the LLM for PII detection (slow on CPU)")
    a = p.parse_args(argv)

    run_migrations()
    if a.cmd == "migrate":
        print("migrations up to date")
    elif a.cmd == "create-user":
        create_user(a.user_id, a.name, a.email, None if a.keycloak else _password(a.password), a.keycloak)
        print(f"created {a.user_id}")
    elif a.cmd == "set-password":
        with get_conn() as conn:
            n = conn.execute(
                "UPDATE users SET password_hash = %s, auth_source = 'local', modified_by = 'cli' " "WHERE user_id = %s",
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
