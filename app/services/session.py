import uuid
import psycopg2.extras
from app.utils.db import get_db_connection


def create_session(company_id: str, user_id: str, equipment_path: str, equipment_id: str) -> str:
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            session_id = str(uuid.uuid4())
            cur.execute("""
                INSERT INTO chat_sessions (id, company_id, user_id, equipment_id, equipment_path)
                VALUES (%s::uuid, %s::uuid, %s::uuid, %s::uuid, %s)
            """, (session_id, company_id, user_id, equipment_id, equipment_path))
            conn.commit()
            return session_id
    finally:
        conn.close()


def get_session(session_id: str, company_id: str) -> dict:
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, company_id, user_id, equipment_id, equipment_path
                FROM chat_sessions
                WHERE id = %s::uuid
                AND company_id = %s::uuid
                AND deleted_at IS NULL
            """, (session_id, company_id))
            return cur.fetchone()
    finally:
        conn.close()


def get_history(session_id: str, company_id: str, limit: int = 10) -> list:
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT role, content
                FROM chat_messages
                WHERE session_id = %s::uuid
                AND company_id = %s::uuid
                ORDER BY created_at DESC
                LIMIT %s
            """, (session_id, company_id, limit))
            messages = cur.fetchall()
            return list(reversed(messages))
    finally:
        conn.close()


def save_messages(session_id: str, company_id: str, user_query: str, assistant_answer: str):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO chat_messages (session_id, company_id, role, content)
                VALUES (%s::uuid, %s::uuid, 'user', %s),
                       (%s::uuid, %s::uuid, 'assistant', %s)
            """, (session_id, company_id, user_query, session_id, company_id, assistant_answer))
            cur.execute("""
                UPDATE chat_sessions SET updated_at = NOW()
                WHERE id = %s::uuid
            """, (session_id,))
            conn.commit()
    finally:
        conn.close()