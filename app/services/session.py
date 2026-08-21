"""
Session / turn management for the equipment chat.

What changed from the original, and why:

  1. `append_turn()` replaces the assumption that every turn is exactly one
     user string + one assistant string. Tool-calling turns (e.g. the
     assistant calling create_job_aid) are multiple messages: assistant
     tool_use -> (app executes tool) -> tool_result -> assistant text.
     `save_messages()` is kept as a thin backward-compatible wrapper so
     nothing calling it today breaks.

  2. Every message can carry `metadata` (JSONB) -- sources/links/images
     surfaced on that turn, which tool was called, model name, token
     counts, whatever the orchestrator wants. This is what lets a later
     turn ("send me that job aid link again") answer from history instead
     of re-deriving it.

  3. `content` is still stored as TEXT (no column type migration needed),
     but can hold either a plain string (old rows, simple turns) or a
     JSON-encoded list of content blocks (Anthropic-style, for tool
     turns). `get_history()` transparently deserializes whichever it
     finds.

  4. Long conversations degrade instead of blowing up the prompt: a
     rolling summary lives on chat_sessions (`summary`,
     `summary_message_count`). `get_history()` returns the summary plus
     only the messages after it. `maybe_summarize()` is how the
     orchestrator triggers a re-summarize once the tail gets long --
     session.py deliberately does NOT call an LLM itself (keeps this
     module dependency-free of app.services.llm); the caller passes a
     `summarizer_fn(existing_summary, messages) -> str`.
     AS OF THE "no conversation history in answers" change in
     chat_service.py, neither `get_history()` nor `maybe_summarize()` is
     called from the chat turn anymore -- every answer is generated from
     only the current query + freshly retrieved Equipment Knowledge, on
     purpose (a model that once fabricates something must not be able to
     treat its own earlier turns as established fact and build further
     invented detail on top of them). Both functions are left here,
     working and unit-testable, in case some other feature wants a
     conversation summary later; they're just not wired into answering.

  5. A session is just a convenience for keeping history around (for
     display/audit -- e.g. a UI listing a session's past messages), not
     an access-control boundary the chat can fail on, AND (since the
     change described in point 4) not something generation reads from at
     all anymore either. There used to be a get_active_session() helper
     (unused anywhere -- removed) and an update_session_equipment() that
     re-pointed a session's equipment_id WITHOUT touching its existing
     messages/summary. That combination is exactly how one equipment's
     real, correctly-grounded answer (e.g. a pump's oil spec) leaked into
     another equipment's answer, back when get_history() fed the
     conversation into every call_claude() turn: no per-equipment filter,
     so a re-pointed session's old messages/summary just kept riding
     along into the new equipment's context. `reset_session_for_equipment()`
     replaces it: when chat_service.handle_chat_turn() sees a session_id
     attached to different equipment than the current request, it clears
     that session's messages/summary and re-homes it in the same
     transaction -- history and equipment_id can never drift apart. Now
     that generation doesn't read history at all, this no longer prevents
     cross-equipment leakage into an *answer* (there's nothing for it to
     leak into) -- it's kept purely so the *stored* chat_messages for a
     session_id stay coherent for one piece of equipment.

Requires two additive migrations (see migration.sql):
    ALTER TABLE chat_sessions ADD COLUMN summary TEXT;
    ALTER TABLE chat_sessions ADD COLUMN summary_message_count INT NOT NULL DEFAULT 0;
    ALTER TABLE chat_messages ADD COLUMN metadata JSONB NOT NULL DEFAULT '{}'::jsonb;
Nothing here requires changing the existing `content` column type.
"""
import json
import uuid

import psycopg2.extras
from app.utils.db import get_db_connection

DEFAULT_HISTORY_LIMIT = 10
# Once more than this many messages have accumulated after the last
# summary, it's worth rolling them up.
SUMMARIZE_TRIGGER_COUNT = 20
# ...but always keep this many of the most recent messages verbatim,
# unsummarized, so recent turn-taking stays crisp.
SUMMARIZE_KEEP_RECENT = 6


def _serialize_content(content):
    """Plain strings pass through untouched (backward compatible with
    existing rows); anything else (list of content blocks, dict) is
    JSON-encoded."""
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
    """
    Re-homes a session to a different piece of equipment AND wipes its
    message history + rolling summary, atomically, in one transaction.

    A session is just a convenience for keeping a chat's history around
    (so a later turn can say "which job aid was that?" and still know)
    -- it is NOT an access-control boundary the chat is allowed to fail
    on. chat_service.handle_chat_turn() calls this instead of erroring
    when a session_id shows up attached to different equipment than the
    current request: drop the history that no longer applies and keep
    the conversation going, rather than blocking the user's question on
    a session-bookkeeping mismatch.

    Clearing messages/summary is not optional here -- it's always paired
    with the equipment_id update, in the same query. Repointing a
    session's equipment_id WITHOUT wiping its history is what the old
    update_session_equipment() did, and it's exactly how one equipment's
    real, correctly-grounded answer (a pump's oil spec) leaked into an
    answer about unrelated equipment: get_history() has no per-equipment
    filter, so the old messages just kept riding along to the new
    equipment's context. There is deliberately no version of this
    function that repoints equipment_id alone.
    """
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                DELETE FROM chat_messages
                WHERE session_id = %s::uuid AND company_id = %s::uuid
            """, (session_id, company_id))
            cur.execute("""
                UPDATE chat_sessions
                SET equipment_id = %s::uuid, equipment_path = %s,
                    summary = NULL, summary_message_count = 0, updated_at = NOW()
                WHERE id = %s::uuid AND company_id = %s::uuid
            """, (equipment_id, equipment_path, session_id, company_id))
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def append_turn(session_id: str, company_id: str, role: str, content,
                 metadata: dict = None, conn=None) -> None:
    """
    Insert a single message. `content` may be a plain string or a list of
    Anthropic-style content blocks (text / tool_use / tool_result dicts).
    `metadata` is free-form JSON (sources, tool name, model, token counts).
    """
    if role not in ("user", "assistant", "tool"):
        raise ValueError(f"Unexpected role: {role!r}")

    owns_conn = conn is None
    conn = conn or get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO chat_messages (session_id, company_id, role, content, metadata)
                VALUES (%s::uuid, %s::uuid, %s, %s, %s::jsonb)
            """, (
                session_id, company_id, role,
                _serialize_content(content),
                psycopg2.extras.Json(metadata or {}),
            ))
            cur.execute("""
                UPDATE chat_sessions SET updated_at = NOW()
                WHERE id = %s::uuid
            """, (session_id,))
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
    """
    Backward-compatible wrapper for the common case (no tool use): one
    user turn, one assistant turn, saved atomically. Prefer
    `append_turn()` directly for tool-calling turns.
    """
    conn = get_db_connection()
    try:
        append_turn(session_id, company_id, "user", user_query, conn=conn)
        assistant_metadata = {}
        if sources:
            assistant_metadata["sources"] = sources
        if tool_calls:
            assistant_metadata["tool_calls"] = tool_calls
        append_turn(session_id, company_id, "assistant", assistant_answer,
                    metadata=assistant_metadata, conn=conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_history(session_id: str, company_id: str, limit: int = DEFAULT_HISTORY_LIMIT) -> dict:
    """
    Returns {"summary": str|None, "messages": [{"role","content","metadata","created_at"}, ...]}.

    `summary` covers everything older than the returned messages (see
    maybe_summarize below); feed both to the model -- summary as a system/
    context note, messages as the real turn history.
    """
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT summary, summary_message_count
                FROM chat_sessions
                WHERE id = %s::uuid AND company_id = %s::uuid
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

            for m in messages:
                m["content"] = _deserialize_content(m["content"])

            return {
                "summary": session_row.get("summary"),
                "messages": messages,
            }
    finally:
        conn.close()


def maybe_summarize(session_id: str, company_id: str, summarizer_fn,
                     trigger_after: int = SUMMARIZE_TRIGGER_COUNT,
                     keep_recent: int = SUMMARIZE_KEEP_RECENT) -> bool:
    """
    If more than `trigger_after` messages have piled up since the last
    summary, roll everything except the most recent `keep_recent` into an
    updated summary via `summarizer_fn(existing_summary, messages) -> str`.

    Returns True if a summary update happened. Call this after
    append_turn/save_messages, e.g. fire-and-forget after responding to
    the user so it doesn't add latency to the turn itself.
    """
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT summary, summary_message_count
                FROM chat_sessions
                WHERE id = %s::uuid AND company_id = %s::uuid
                FOR UPDATE
            """, (session_id, company_id))
            session_row = cur.fetchone()
            if not session_row:
                return False

            cur.execute("""
                SELECT id, role, content, created_at
                FROM chat_messages
                WHERE session_id = %s::uuid AND company_id = %s::uuid
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

            for m in to_summarize:
                m["content"] = _deserialize_content(m["content"])

            new_summary = summarizer_fn(session_row["summary"], to_summarize)

            cur.execute("""
                UPDATE chat_sessions
                SET summary = %s, summary_message_count = %s
                WHERE id = %s::uuid AND company_id = %s::uuid
            """, (new_summary, already_summarized + len(to_summarize), session_id, company_id))
            conn.commit()
            return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()