"""Knowledge-base catalog and access control. Every API route goes through require_access()."""

from fastapi import HTTPException

from app.auth import CurrentUser
from app.db import get_conn


class AccessDenied(HTTPException):
    def __init__(self, detail="You do not have access to this knowledge base"):
        super().__init__(403, detail)


def get_catalog(kb_name: str) -> dict | None:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM kb_catalog WHERE kb_name = %s", (kb_name,)).fetchone()


def role_of(user_id: str, kb_name: str) -> str | None:
    """Active role from knowledge_bases, cross-checked against the kb_access audit trail."""
    with get_conn() as conn:
        row = conn.execute(
            """SELECT kb.access FROM knowledge_bases kb
               JOIN kb_access a ON a.kb_name = kb.kb_name AND a.user_id = kb.user_id AND a.revoked_at IS NULL
               JOIN users u ON u.user_id = kb.user_id AND u.is_active
               WHERE kb.user_id = %s AND kb.kb_name = %s""",
            (user_id, kb_name),
        ).fetchone()
    return row["access"] if row else None


def require_access(user: CurrentUser, kb_name: str, roles=("owner", "user")) -> dict:
    cat = get_catalog(kb_name)
    if cat is None:
        raise HTTPException(404, "Knowledge base not found")
    role = role_of(user.user_id, kb_name)
    if role is None:
        raise AccessDenied()
    if role not in roles:
        raise AccessDenied("Only the owner of this knowledge base can do that")
    return {**cat, "role": role}


def list_for_user(user_id: str) -> list[dict]:
    with get_conn() as conn:
        return conn.execute(
            """SELECT kb.kb_name, kb.kb_type, kb.domain, kb.sub_domain, kb.access AS role,
                      c.status, c.status_detail, c.owner_id, c.created_at, c.updated_at,
                      (SELECT j.id FROM jobs j WHERE j.kb_name = c.kb_name ORDER BY j.id DESC LIMIT 1) AS last_job_id
               FROM knowledge_bases kb JOIN kb_catalog c ON c.kb_name = kb.kb_name
               JOIN kb_access a ON a.kb_name = kb.kb_name AND a.user_id = kb.user_id AND a.revoked_at IS NULL
               WHERE kb.user_id = %s ORDER BY c.created_at DESC""",
            (user_id,),
        ).fetchall()


def create(user: CurrentUser, kb_name: str, kb_type: str, domain: str, sub_domain: str, storage_ref: str) -> dict:
    """The creator becomes the owner: one row in kb_catalog, knowledge_bases and kb_access."""
    import psycopg

    status = "extracting" if kb_type == "graph" else "ingesting"
    try:
        with get_conn() as conn:
            conn.execute(
                """INSERT INTO kb_catalog
                       (kb_name, kb_type, domain, sub_domain, owner_id, status, storage_ref, modified_by)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (kb_name, kb_type, domain, sub_domain, user.user_id, status, storage_ref, user.user_id),
            )
            conn.execute(
                """INSERT INTO knowledge_bases (user_id, kb_name, kb_type, domain, sub_domain, access, modified_by)
                   VALUES (%s, %s, %s, %s, %s, 'owner', %s)""",
                (user.user_id, kb_name, kb_type, domain, sub_domain, user.user_id),
            )
            conn.execute(
                """INSERT INTO kb_access (kb_name, user_id, role, granted_by, modified_by)
                   VALUES (%s, %s, 'owner', 'system', %s)""",
                (kb_name, user.user_id, user.user_id),
            )
    except psycopg.errors.UniqueViolation:
        raise HTTPException(409, f"A knowledge base named '{kb_name}' already exists") from None
    except psycopg.errors.CheckViolation:
        raise HTTPException(
            422, "Name must be 3-63 characters: lowercase letters, digits and underscores, " "starting with a letter"
        ) from None
    return get_catalog(kb_name)


def set_status(kb_name: str, status: str, detail: str | None = None, actor: str = "system", **fields) -> None:
    sets = ["status = %(status)s", "status_detail = %(detail)s", "modified_by = %(actor)s"]
    params = {"status": status, "detail": detail, "actor": actor, "kb": kb_name}
    for k, v in fields.items():
        sets.append(f"{k} = %({k})s")
        params[k] = v
    with get_conn() as conn:
        conn.execute(f"UPDATE kb_catalog SET {', '.join(sets)} WHERE kb_name = %(kb)s", params)


def delete(kb_name: str, actor: str) -> dict:
    """Delete the catalog and all access/job rows for a knowledge base."""
    with get_conn() as conn:
        row = conn.execute(
            "DELETE FROM kb_catalog WHERE kb_name = %s RETURNING kb_name, kb_type", (kb_name,)
        ).fetchone()
    if not row:
        raise HTTPException(404, "Knowledge base not found")
    return row


# ------------------------------------------------------------------ grant / revoke (owner only)
def grant(owner: CurrentUser, kb_name: str, user_id: str) -> dict:
    cat = require_access(owner, kb_name, roles=("owner",))
    user_id = user_id.strip()
    with get_conn() as conn:
        target = conn.execute(
            "SELECT user_id, display_name, is_active FROM users WHERE user_id = %s", (user_id,)
        ).fetchone()
        if not target or not target["is_active"]:
            raise HTTPException(
                404,
                f"No active user '{user_id}'. In Keycloak mode the person must sign in once "
                "(or be added with the create-user command) before access can be granted.",
            )
        active = conn.execute(
            "SELECT role FROM kb_access WHERE kb_name = %s AND user_id = %s AND revoked_at IS NULL", (kb_name, user_id)
        ).fetchone()
        if active:
            raise HTTPException(409, f"{user_id} already has {active['role']} access")
        conn.execute(
            """INSERT INTO kb_access (kb_name, user_id, role, granted_by, modified_by)
                        VALUES (%s, %s, 'user', %s, %s)""",
            (kb_name, user_id, owner.user_id, owner.user_id),
        )
        conn.execute(
            """INSERT INTO knowledge_bases (user_id, kb_name, kb_type, domain, sub_domain, access, modified_by)
                        VALUES (%s, %s, %s, %s, %s, 'user', %s)
                        ON CONFLICT (user_id, kb_name)
                        DO UPDATE SET access = 'user', modified_by = EXCLUDED.modified_by""",
            (user_id, kb_name, cat["kb_type"], cat["domain"], cat["sub_domain"], owner.user_id),
        )
    return {"user_id": user_id, "display_name": target["display_name"], "role": "user"}


def revoke(owner: CurrentUser, kb_name: str, user_id: str) -> None:
    require_access(owner, kb_name, roles=("owner",))
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, role FROM kb_access WHERE kb_name = %s AND user_id = %s AND revoked_at IS NULL",
            (kb_name, user_id),
        ).fetchone()
        if not row:
            raise HTTPException(404, f"{user_id} has no active access")
        if row["role"] == "owner":
            raise HTTPException(400, "The owner's access can't be revoked")
        conn.execute(
            "UPDATE kb_access SET revoked_at = now(), modified_by = %s WHERE id = %s", (owner.user_id, row["id"])
        )
        conn.execute("DELETE FROM knowledge_bases WHERE user_id = %s AND kb_name = %s", (user_id, kb_name))


def people(kb_name: str) -> list[dict]:
    with get_conn() as conn:
        return conn.execute(
            """SELECT a.user_id, u.display_name, a.role, a.granted_by, a.granted_at
               FROM kb_access a JOIN users u ON u.user_id = a.user_id
               WHERE a.kb_name = %s AND a.revoked_at IS NULL
               ORDER BY (a.role = 'owner') DESC, a.granted_at""",
            (kb_name,),
        ).fetchall()


def access_log(kb_name: str) -> list[dict]:
    """Every create / grant / revoke, newest first, from the kb_access audit trail."""
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM kb_access WHERE kb_name = %s", (kb_name,)).fetchall()
    events = []
    for r in rows:
        if r["role"] == "owner":
            events.append({"action": "created", "actor": r["user_id"], "user_id": r["user_id"], "at": r["granted_at"]})
        else:
            events.append(
                {"action": "granted", "actor": r["granted_by"], "user_id": r["user_id"], "at": r["granted_at"]}
            )
        if r["revoked_at"]:
            events.append(
                {"action": "revoked", "actor": r["modified_by"], "user_id": r["user_id"], "at": r["revoked_at"]}
            )
    return sorted(events, key=lambda e: e["at"], reverse=True)
