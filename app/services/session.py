"""Session and turn persistence for the SquareMethods equipment chat.

Conversation history is stored for UI/audit purposes, but it is deliberately
NOT treated as equipment knowledge by chat_service.py. Maintenance answers
must be grounded in the current equipment's manual, published job aids and
failure-mode records retrieved for that turn.
"""
import json
import uuid

import psycopg2.extras
from app.utils.db import get_db_connection

DEFAULT_HISTORY_LIMIT = 10
SUMMARIZE_TRIGGER_COUNT = 20
SUMMARIZE_KEEP_RECENT = 6


def _serialize_content(content):
    if isinstance(content, str):
        return content
    return json.dumps(content)


def _deserialize_content(raw):
    if raw is None:
        return raw
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, (list, dict)):
            return parsed
    except (json.JSONDecodeError, TypeError):
        pass
    return raw


def create_session(company_id: str, user_id: str, equipment_path: str,
                   equipment_id: str, title: str = None) -> str:
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            session_id = str(uuid.uuid4())
            cur.execute("""
                INSERT INTO chat_sessions
                    (id, company_id, user_id, equipment_id, equipment_path, title)
                VALUES (%s::uuid, %s::uuid, %s::uuid, %s::uuid, %s, %s)
            """, (session_id, company_id, user_id, equipment_id, equipment_path, title))
            conn.commit()
            return session_id
    finally:
        conn.close()


def get_session(session_id: str, company_id: str) -> dict:
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, company_id, user_id, equipment_id, equipment_path,
                       summary, summary_message_count
                FROM chat_sessions
                WHERE id = %s::uuid
                  AND company_id = %s::uuid
                  AND deleted_at IS NULL
            """, (session_id, company_id))
            return cur.fetchone()
    finally:
        conn.close()


def reset_session_for_equipment(session_id: str, company_id: str,
                                equipment_id: str, equipment_path: str) -> None:
    """Atomically clear old conversation state and re-home the session."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                DELETE FROM chat_messages
                WHERE session_id = %s::uuid
                  AND company_id = %s::uuid
            """, (session_id, company_id))
            cur.execute("""
                UPDATE chat_sessions
                SET equipment_id = %s::uuid,
                    equipment_path = %s,
                    summary = NULL,
                    summary_message_count = 0,
                    updated_at = NOW()
                WHERE id = %s::uuid
                  AND company_id = %s::uuid
                  AND deleted_at IS NULL
            """, (equipment_id, equipment_path, session_id, company_id))
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def append_turn(session_id: str, company_id: str, role: str, content,
                metadata: dict = None, conn=None) -> None:
    """Insert one chat message with optional structured metadata."""
    if role not in ("user", "assistant", "tool"):
        raise ValueError(f"Unexpected role: {role!r}")

    owns_conn = conn is None
    conn = conn or get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO chat_messages
                    (session_id, company_id, role, content, metadata)
                VALUES (%s::uuid, %s::uuid, %s, %s, %s::jsonb)
            """, (
                session_id,
                company_id,
                role,
                _serialize_content(content),
                psycopg2.extras.Json(metadata or {}),
            ))
            cur.execute("""
                UPDATE chat_sessions
                SET updated_at = NOW()
                WHERE id = %s::uuid
                  AND company_id = %s::uuid
            """, (session_id, company_id))
        if owns_conn:
            conn.commit()
    except Exception:
        if owns_conn:
            conn.rollback()
        raise
    finally:
        if owns_conn:
            conn.close()


def save_messages(session_id: str, company_id: str, user_query: str,
                  assistant_answer: str, sources: dict = None,
                  tool_calls: list = None) -> None:
    """Backward-compatible atomic save for a normal user/assistant turn."""
    conn = get_db_connection()
    try:
        append_turn(
            session_id,
            company_id,
            "user",
            user_query,
            conn=conn,
        )
        assistant_metadata = {}
        if sources:
            assistant_metadata["sources"] = sources
        if tool_calls:
            assistant_metadata["tool_calls"] = tool_calls
        append_turn(
            session_id,
            company_id,
            "assistant",
            assistant_answer,
            metadata=assistant_metadata,
            conn=conn,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_history(session_id: str, company_id: str,
                limit: int = DEFAULT_HISTORY_LIMIT) -> dict:
    """Return stored history for UI/audit features, not as maintenance evidence."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT summary, summary_message_count
                FROM chat_sessions
                WHERE id = %s::uuid
                  AND company_id = %s::uuid
                  AND deleted_at IS NULL
            """, (session_id, company_id))
            session_row = cur.fetchone() or {}

            cur.execute("""
                SELECT role, content, metadata, created_at
                FROM chat_messages
                WHERE session_id = %s::uuid
                  AND company_id = %s::uuid
                ORDER BY created_at DESC
                LIMIT %s
            """, (session_id, company_id, limit))
            messages = list(reversed(cur.fetchall()))
            for message in messages:
                message["content"] = _deserialize_content(message["content"])

            return {
                "summary": session_row.get("summary"),
                "messages": messages,
            }
    finally:
        conn.close()


def maybe_summarize(session_id: str, company_id: str, summarizer_fn,
                    trigger_after: int = SUMMARIZE_TRIGGER_COUNT,
                    keep_recent: int = SUMMARIZE_KEEP_RECENT) -> bool:
    """Maintain an optional UI/audit summary; never used as maintenance evidence."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT summary, summary_message_count
                FROM chat_sessions
                WHERE id = %s::uuid
                  AND company_id = %s::uuid
                  AND deleted_at IS NULL
                FOR UPDATE
            """, (session_id, company_id))
            session_row = cur.fetchone()
            if not session_row:
                return False

            cur.execute("""
                SELECT id, role, content, created_at
                FROM chat_messages
                WHERE session_id = %s::uuid
                  AND company_id = %s::uuid
                ORDER BY created_at ASC
            """, (session_id, company_id))
            all_messages = cur.fetchall()

            already_summarized = session_row["summary_message_count"] or 0
            unsummarized = all_messages[already_summarized:]
            if len(unsummarized) <= trigger_after:
                conn.rollback()
                return False

            to_summarize = unsummarized[:-keep_recent] if keep_recent else unsummarized
            if not to_summarize:
                conn.rollback()
                return False

            for message in to_summarize:
                message["content"] = _deserialize_content(message["content"])

            new_summary = summarizer_fn(session_row["summary"], to_summarize)
            cur.execute("""
                UPDATE chat_sessions
                SET summary = %s,
                    summary_message_count = %s,
                    updated_at = NOW()
                WHERE id = %s::uuid
                  AND company_id = %s::uuid
            """, (
                new_summary,
                already_summarized + len(to_summarize),
                session_id,
                company_id,
            ))
            conn.commit()
            return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()