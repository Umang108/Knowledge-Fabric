"""Saved connections (table `connections`). Secrets are encrypted at rest and never leave the backend."""

import json

import psycopg
from fastapi import HTTPException

from app import auth
from app.auth import CurrentUser
from app.connectors import KINDS, Connector, ConnectorError
from app.connectors.base import check_url
from app.db import get_conn

# options a connection may carry (anything else is dropped)
OPTION_KEYS = {
    "servicenow": {"client_id", "token_url", "max_rows", "verify_tls"},
    "sap": {
        "client_id",
        "token_url",
        "sap_client",
        "service_root",
        "odata_version",
        "max_rows",
        "verify_tls",
        "test_dataset",
    },
}


def view(row: dict) -> dict:
    """A connection as the API shows it: never the secret."""
    return {
        "id": row["id"],
        "name": row["name"],
        "kind": row["kind"],
        "label": KINDS[row["kind"]].label,
        "base_url": row["base_url"],
        "auth_type": row["auth_type"],
        "username": row["username"],
        "has_secret": bool(row["secret"]),
        "options": row["options"] or {},
        "last_tested_at": row["last_tested_at"],
        "last_test_ok": row["last_test_ok"],
        "last_test_detail": row["last_test_detail"],
        "updated_at": row["updated_at"],
    }


def _clean(body: dict, existing: dict | None = None) -> dict:
    kind = body.get("kind") or (existing or {}).get("kind")
    if kind not in KINDS:
        raise HTTPException(422, f"kind must be one of {', '.join(KINDS)}")
    auth_type = body.get("auth_type") or (existing or {}).get("auth_type") or "basic"
    if auth_type not in ("basic", "oauth"):
        raise HTTPException(422, "auth_type must be basic or oauth")
    name = str(body.get("name") or (existing or {}).get("name") or "").strip()
    if not name or len(name) > 100:
        raise HTTPException(422, "Give the connection a name (up to 100 characters)")
    try:
        base_url = check_url(body.get("base_url") or (existing or {}).get("base_url") or "")
    except ConnectorError as exc:
        raise HTTPException(422, str(exc)) from None
    options = {k: v for k, v in (body.get("options") or {}).items() if k in OPTION_KEYS[kind]}
    if kind == "sap" and str(options.get("odata_version", "2")) not in ("2", "4"):
        raise HTTPException(422, "odata_version must be 2 or 4")
    if "max_rows" in options:
        try:
            options["max_rows"] = max(1, min(int(options["max_rows"]), 1_000_000))
        except (TypeError, ValueError):
            raise HTTPException(422, "max_rows must be a number") from None
    username = (body.get("username") if "username" in body else (existing or {}).get("username")) or None
    if auth_type == "basic" and not username:
        raise HTTPException(422, "Basic authentication needs a username")
    if auth_type == "oauth" and not options.get("client_id"):
        raise HTTPException(422, "OAuth needs a client id")
    if kind == "sap" and auth_type == "oauth" and not options.get("token_url"):
        raise HTTPException(422, "SAP OAuth needs the token URL (e.g. from the BTP service key)")
    return {
        "name": name,
        "kind": kind,
        "base_url": base_url,
        "auth_type": auth_type,
        "username": username,
        "options": options,
    }


def create(user: CurrentUser, body: dict) -> dict:
    c = _clean(body)
    if not body.get("secret"):
        raise HTTPException(422, "Enter the password or client secret")
    try:
        with get_conn() as conn:
            row = conn.execute(
                """INSERT INTO connections (name, kind, base_url, auth_type, username, secret, options, owner_id,
                                            modified_by)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
                (
                    c["name"],
                    c["kind"],
                    c["base_url"],
                    c["auth_type"],
                    c["username"],
                    auth.encrypt(body["secret"]),
                    json.dumps(c["options"]),
                    user.user_id,
                    user.user_id,
                ),
            ).fetchone()
    except psycopg.errors.UniqueViolation:
        raise HTTPException(409, f"You already have a connection named '{c['name']}'") from None
    return view(row)


def get_row(user: CurrentUser, connection_id: int) -> dict:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM connections WHERE id = %s AND owner_id = %s", (connection_id, user.user_id)
        ).fetchone()
    if not row:
        raise HTTPException(404, "Connection not found")
    return row


def update(user: CurrentUser, connection_id: int, body: dict) -> dict:
    existing = get_row(user, connection_id)
    c = _clean({**body, "kind": existing["kind"]}, existing)
    secret = auth.encrypt(body["secret"]) if body.get("secret") else existing["secret"]
    try:
        with get_conn() as conn:
            row = conn.execute(
                """UPDATE connections SET name = %s, base_url = %s, auth_type = %s, username = %s, secret = %s,
                          options = %s, modified_by = %s WHERE id = %s RETURNING *""",
                (
                    c["name"],
                    c["base_url"],
                    c["auth_type"],
                    c["username"],
                    secret,
                    json.dumps(c["options"]),
                    user.user_id,
                    connection_id,
                ),
            ).fetchone()
    except psycopg.errors.UniqueViolation:
        raise HTTPException(409, f"You already have a connection named '{c['name']}'") from None
    return view(row)


def delete(user: CurrentUser, connection_id: int) -> None:
    get_row(user, connection_id)
    with get_conn() as conn:
        conn.execute("DELETE FROM connections WHERE id = %s", (connection_id,))


def open_connector(row: dict) -> Connector:
    secret = auth.decrypt(row["secret"])
    if row["secret"] and secret is None:
        raise ConnectorError("The saved secret can't be read (SECRET_KEY changed); enter it again")
    return KINDS[row["kind"]](row["base_url"], row["auth_type"], row["username"], secret, row["options"] or {})


def record_test(connection_id: int, ok: bool, detail: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """UPDATE connections SET last_tested_at = now(), last_test_ok = %s, last_test_detail = %s
               WHERE id = %s""",
            (ok, detail[:500], connection_id),
        )
