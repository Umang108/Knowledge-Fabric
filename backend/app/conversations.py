"""Saved chat conversations: each user keeps their last KEEP conversations (older ones are deleted when a new one
starts). A conversation belongs to one knowledge base; it is only shown while the user can still access that
knowledge base. Follow-up questions use the saved turns as history (not history sent by the browser)."""

import json

from fastapi import HTTPException

from app import kb
from app.auth import CurrentUser
from app.db import get_conn

KEEP = 10
HISTORY_TURNS = 5


def _title(question: str) -> str:
    q = " ".join(question.split())
    return q if len(q) <= 80 else q[:77].rstrip() + "..."


def create(user: CurrentUser, kb_name: str, question: str) -> dict:
    with get_conn() as conn:
        conv = conn.execute(
            """INSERT INTO chat_conversations (user_id, kb_name, title, modified_by)
               VALUES (%s, %s, %s, %s) RETURNING *""",
            (user.user_id, kb_name, _title(question), user.user_id),
        ).fetchone()
        conn.execute(
            """DELETE FROM chat_conversations WHERE user_id = %s AND id NOT IN (
                   SELECT id FROM chat_conversations WHERE user_id = %s
                   ORDER BY last_message_at DESC, id DESC LIMIT %s)""",
            (user.user_id, user.user_id, KEEP),
        )
    return conv


def get(user: CurrentUser, conversation_id: int) -> dict:
    """The user's own conversation, on a knowledge base they can still access."""
    with get_conn() as conn:
        conv = conn.execute(
            "SELECT * FROM chat_conversations WHERE id = %s AND user_id = %s", (conversation_id, user.user_id)
        ).fetchone()
    if not conv:
        raise HTTPException(404, "Conversation not found")
    kb.require_access(user, conv["kb_name"])
    return conv


def history(conversation_id: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT question, answer, details FROM chat_messages WHERE conversation_id = %s
               ORDER BY id DESC LIMIT %s""",
            (conversation_id, HISTORY_TURNS),
        ).fetchall()
    return [
        {"question": r["question"], "answer": r["answer"], "cypher": (r["details"] or {}).get("cypher") or ""}
        for r in reversed(rows)
    ]


def add_message(user: CurrentUser, conversation_id: int, question: str, result: dict) -> None:
    details = {k: result.get(k) for k in ("kind", "cypher", "path", "rows", "row_count", "sources", "guardrails")}
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO chat_messages (conversation_id, question, answer, details, modified_by)
               VALUES (%s, %s, %s, %s, %s)""",
            (conversation_id, question, result.get("answer") or "", json.dumps(details, default=str), user.user_id),
        )
        conn.execute(  # bump to the top of the list
            "UPDATE chat_conversations SET last_message_at = now(), modified_by = %s WHERE id = %s",
            (user.user_id, conversation_id),
        )


def list_recent(user: CurrentUser) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT c.id, c.kb_name, c.title, c.created_at, c.last_message_at, k.kb_type,
                      (SELECT count(*) FROM chat_messages m WHERE m.conversation_id = c.id) AS messages
               FROM chat_conversations c
               JOIN knowledge_bases k ON k.kb_name = c.kb_name AND k.user_id = c.user_id
               WHERE c.user_id = %s ORDER BY c.last_message_at DESC, c.id DESC LIMIT %s""",
            (user.user_id, KEEP),
        ).fetchall()
    return [dict(r) for r in rows]


def messages(conversation_id: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT id, question, answer, details, created_at FROM chat_messages
               WHERE conversation_id = %s ORDER BY id""",
            (conversation_id,),
        ).fetchall()
    return [
        {
            "id": r["id"],
            "question": r["question"],
            "answer": r["answer"],
            "created_at": r["created_at"],
            **(r["details"] or {}),
        }
        for r in rows
    ]


def delete(user: CurrentUser, conversation_id: int) -> None:
    with get_conn() as conn:
        n = conn.execute(
            "DELETE FROM chat_conversations WHERE id = %s AND user_id = %s", (conversation_id, user.user_id)
        ).rowcount
    if not n:
        raise HTTPException(404, "Conversation not found")
