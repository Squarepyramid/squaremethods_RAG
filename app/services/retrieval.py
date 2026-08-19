"""
Retrieval layer for the equipment chat.

Changes from the original version:
  1. All getters accept an optional `conn` so `build_context()` can run the
     whole read on a single connection/snapshot instead of opening 4-6
     separate connections per turn.
  2. `build_context()` now returns a dict with a `context` string (for the
     LLM prompt) AND a structured `sources` payload (job aid links, images,
     failure modes, semantic hits) so the app layer can render clickable
     links / images without scraping them back out of prose.
  3. Added `get_job_aid_media()` and `build_job_aid_url()` to support links
     and images. The table/URL shape is an ASSUMPTION -- see the comment on
     each, and the migration.sql file -- confirm against your real schema
     and routing before shipping.
  4. Added `create_job_aid()` for the "assistant can author a job aid"
     tool (see tools.py). New job aids are created with status='draft' by
     default -- an AI-authored maintenance procedure should never become
     visible to technicians (status='published', which is what
     get_job_aids() filters on) without a human reviewing it first. This
     is a deliberate safety choice, not an oversight.
  5. failure_modes.resolutions is treated as already being the aggregate
     of all contributions (per your confirmation) -- get_failure_modes()
     is functionally unchanged, just documented.

Everything here still assumes get_db_connection() hands back a connection
whose cursors default to RealDictCursor (that's implied by the original
code doing `equipment['name']` on fetchone() results).
"""
import os
import uuid
from contextlib import contextmanager

import psycopg2.extras
from app.utils.db import get_db_connection
from app.services.embeddings import get_embedding

# Similarity floor for semantic search hits that are worth surfacing to the
# model / user. Was a magic number (0.7) inline before.
SEMANTIC_SIMILARITY_THRESHOLD = 0.7

# ASSUMPTION: base URL your frontend serves equipment/job aid pages from.
# Wire this to your actual settings/config object instead of an env var if
# you already have one (e.g. `from app.config import settings`).
APP_BASE_URL = os.environ.get("APP_BASE_URL", "https://app.example.com").rstrip("/")


@contextmanager
def _connection(conn=None):
    """Reuse a passed-in connection, or open+close a private one."""
    owns_conn = conn is None
    conn = conn or get_db_connection()
    try:
        yield conn
    finally:
        if owns_conn:
            conn.close()


def _validate_uuid(value: str, field_name: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        raise ValueError(f"Invalid UUID for {field_name}: {value!r}")


def build_job_aid_url(company_id: str, equipment_id: str, job_aid_id: str) -> str:
    """
    ASSUMPTION: adjust this path to match your real frontend routing.
    Kept as one function so there's a single place to fix if it's wrong.
    """
    return f"{APP_BASE_URL}/equipment/{equipment_id}/job-aids/{job_aid_id}"


def get_equipment(equipment_id: str, company_id: str, conn=None) -> dict:
    with _connection(conn) as c:
        with c.cursor() as cur:
            cur.execute("""
                SELECT e.id, e.name, e.reference_code, e.notes, e.status,
                       et.name as equipment_type
                FROM equipment e
                LEFT JOIN equipment_types et ON e.equipment_type_id = et.id
                WHERE e.id = %s::uuid
                AND e.company_id = %s::uuid
                AND e.deleted_at IS NULL
            """, (equipment_id, company_id))
            return cur.fetchone()


def get_job_aids(equipment_id: str, company_id: str, conn=None) -> list:
    with _connection(conn) as c:
        with c.cursor() as cur:
            cur.execute("""
                SELECT ja.id, ja.title, ja.instruction, ja.category,
                       ja.estimated_duration, ja.status
                FROM job_aids ja
                JOIN job_aid_equipment jae ON ja.id = jae.job_aid_id
                WHERE jae.equipment_id = %s::uuid
                AND ja.company_id = %s::uuid
                AND ja.deleted_at IS NULL
                AND ja.status = 'published'
            """, (equipment_id, company_id))
            return cur.fetchall()


def get_procedures(job_aid_ids: list, company_id: str, conn=None) -> list:
    if not job_aid_ids:
        return []
    with _connection(conn) as c:
        with c.cursor() as cur:
            cur.execute("""
                SELECT p.job_aid_id, p.title, p.step, p.instruction,
                       p.precautions, p.type
                FROM procedures p
                WHERE p.job_aid_id = ANY(%s::uuid[])
                AND p.company_id = %s::uuid
                AND p.deleted_at IS NULL
                ORDER BY p.job_aid_id, p.step
            """, (job_aid_ids, company_id))
            return cur.fetchall()


def get_job_aid_media(job_aid_ids: list, company_id: str, conn=None) -> list:
    """
    ASSUMPTION: a `job_aid_media` table doesn't exist yet in the code you
    shared. If you have images/diagrams attached to job aids somewhere
    else (a generic `attachments` table, S3 keys on the job_aid row,
    etc.), point this query at that instead -- the shape everything else
    expects back is: job_aid_id, url, caption, media_type.

    Suggested DDL is in migration.sql.
    """
    if not job_aid_ids:
        return []
    with _connection(conn) as c:
        with c.cursor() as cur:
            try:
                cur.execute("""
                    SELECT job_aid_id, url, caption, media_type
                    FROM job_aid_media
                    WHERE job_aid_id = ANY(%s::uuid[])
                    AND company_id = %s::uuid
                    AND deleted_at IS NULL
                """, (job_aid_ids, company_id))
                return cur.fetchall()
            except Exception as e:
                # Table may not exist yet in your environment -- degrade
                # gracefully instead of breaking the whole chat turn.
                print(f"Job aid media fetch error (is job_aid_media set up?): {str(e)}")
                c.rollback()
                return []


def get_failure_modes(equipment_id: str, company_id: str, conn=None) -> list:
    """
    `resolutions` is already the aggregate of every contribution made
    against this failure mode (per product confirmation) -- no join
    needed here, this is unchanged from the original besides accepting a
    shared connection.
    """
    with _connection(conn) as c:
        with c.cursor() as cur:
            cur.execute("""
                SELECT id, title, status, resolutions
                FROM failure_modes
                WHERE equipment_id = %s::uuid
                AND company_id = %s::uuid
                AND deleted_at IS NULL
            """, (equipment_id, company_id))
            return cur.fetchall()


def semantic_search(query: str, company_id: str, equipment_id: str = None,
                     limit: int = 5, conn=None) -> list:
    try:
        embedding = get_embedding(query)
        embedding_str = "[" + ",".join(map(str, embedding)) + "]"
        with _connection(conn) as c:
            with c.cursor() as cur:
                if equipment_id:
                    cur.execute("""
                        SELECT source_type, source_id, content,
                               1 - (embedding <=> %s::vector) AS similarity
                        FROM knowledge_embeddings
                        WHERE company_id = %s::uuid
                        AND source_id = %s::uuid
                        ORDER BY embedding <=> %s::vector
                        LIMIT %s
                    """, (embedding_str, company_id, equipment_id, embedding_str, limit))
                else:
                    cur.execute("""
                        SELECT source_type, source_id, content,
                               1 - (embedding <=> %s::vector) AS similarity
                        FROM knowledge_embeddings
                        WHERE company_id = %s::uuid
                        ORDER BY embedding <=> %s::vector
                        LIMIT %s
                    """, (embedding_str, company_id, embedding_str, limit))
                return cur.fetchall()
    except Exception as e:
        print(f"Semantic search error: {str(e)}")
        return []


def create_job_aid(company_id: str, equipment_id: str, created_by_user_id: str,
                    title: str, instruction: str = None, category: str = None,
                    estimated_duration=None, steps: list = None, conn=None) -> dict:
    """
    Creates a job aid (+ its equipment link + its procedure steps) as a
    single transaction, in status='draft'.

    `steps` is a list of dicts: {"title": str|None, "instruction": str,
    "precautions": list[str]|None, "type": str|None}. Step numbers are
    assigned by list order (1-indexed) -- pass them already in the order
    they should be performed.

    ASSUMPTION: job_aids has a `created_by` column and procedures rows
    don't need their own id passed in (DB-generated). Adjust the INSERTs
    if your schema differs -- these are the columns visible in the
    original SELECTs plus the obvious ones needed to write a row.
    """
    company_id = _validate_uuid(company_id, "company_id")
    equipment_id = _validate_uuid(equipment_id, "equipment_id")
    if not title or not title.strip():
        raise ValueError("title is required to create a job aid")
    steps = steps or []

    job_aid_id = str(uuid.uuid4())

    with _connection(conn) as c:
        try:
            with c.cursor() as cur:
                cur.execute("""
                    INSERT INTO job_aids
                        (id, company_id, title, instruction, category,
                         estimated_duration, status, created_by)
                    VALUES (%s::uuid, %s::uuid, %s, %s, %s, %s, 'draft', %s::uuid)
                """, (job_aid_id, company_id, title.strip(), instruction, category,
                      estimated_duration, created_by_user_id))

                cur.execute("""
                    INSERT INTO job_aid_equipment (job_aid_id, equipment_id)
                    VALUES (%s::uuid, %s::uuid)
                """, (job_aid_id, equipment_id))

                for i, step in enumerate(steps, start=1):
                    cur.execute("""
                        INSERT INTO procedures
                            (job_aid_id, company_id, title, step, instruction,
                             precautions, type)
                        VALUES (%s::uuid, %s::uuid, %s, %s, %s, %s, %s)
                    """, (
                        job_aid_id, company_id, step.get("title"), i,
                        step.get("instruction"), step.get("precautions"),
                        step.get("type", "step"),
                    ))
            c.commit()
        except Exception:
            c.rollback()
            raise

    return {
        "id": job_aid_id,
        "title": title.strip(),
        "status": "draft",
        "step_count": len(steps),
        "url": build_job_aid_url(company_id, equipment_id, job_aid_id),
    }


def build_context(equipment_path: str, company_id: str, query: str) -> dict:
    """
    Returns:
        {
          "context": "<text blob for the LLM prompt>",
          "sources": {
              "equipment": {...} | None,
              "job_aids": [{"id","title","category","url"}, ...],
              "images": [{"job_aid_id","url","caption"}, ...],
              "failure_modes": [{"id","title","status"}, ...],
              "semantic": [{"source_type","source_id","similarity"}, ...],
          }
        }

    `sources` is what the app layer uses to render clickable job aid
    links / inline images alongside the model's answer, and is also what
    gets persisted on the assistant's chat_messages row (see session.py)
    so a later turn can say "which job aid was that?" and still know.
    """
    equipment_id = equipment_path.strip("/").split("/")[-1]

    context_parts = []
    sources = {
        "equipment": None,
        "job_aids": [],
        "images": [],
        "failure_modes": [],
        "semantic": [],
    }

    with _connection() as conn:
        # 1. Equipment details
        try:
            equipment = get_equipment(equipment_id, company_id, conn=conn)
            if equipment:
                context_parts.append(
                    f"Equipment: {equipment['name']} "
                    f"(Type: {equipment['equipment_type']}, "
                    f"Code: {equipment['reference_code']}, "
                    f"Status: {equipment['status']})"
                )
                if equipment['notes']:
                    context_parts.append(f"Equipment Notes: {equipment['notes']}")
                sources["equipment"] = {
                    "id": str(equipment["id"]),
                    "name": equipment["name"],
                    "status": equipment["status"],
                }
        except Exception as e:
            print(f"Equipment fetch error: {str(e)}")

        # 2. Job aids, procedures, and media
        try:
            job_aids = get_job_aids(equipment_id, company_id, conn=conn)
            if job_aids:
                job_aid_ids = [str(ja['id']) for ja in job_aids]
                procedures = get_procedures(job_aid_ids, company_id, conn=conn)
                media = get_job_aid_media(job_aid_ids, company_id, conn=conn)

                for ja in job_aids:
                    ja_text = f"\nJob Aid: {ja['title']}"
                    if ja['category']:
                        ja_text += f" [{ja['category']}]"
                    if ja['instruction']:
                        ja_text += f"\nInstructions: {ja['instruction']}"

                    ja_procedures = [
                        p for p in procedures
                        if str(p['job_aid_id']) == str(ja['id'])
                    ]
                    if ja_procedures:
                        ja_text += "\nSteps:"
                        for p in ja_procedures:
                            ja_text += f"\n  {p['step']}. {p['instruction']}"
                            if p['precautions']:
                                ja_text += f" (Precautions: {', '.join(p['precautions'])})"

                    job_aid_url = build_job_aid_url(company_id, equipment_id, str(ja['id']))
                    ja_text += f"\nLink: {job_aid_url}"
                    context_parts.append(ja_text)

                    sources["job_aids"].append({
                        "id": str(ja['id']),
                        "title": ja['title'],
                        "category": ja['category'],
                        "url": job_aid_url,
                    })

                for m in media:
                    sources["images"].append({
                        "job_aid_id": str(m['job_aid_id']),
                        "url": m['url'],
                        "caption": m.get('caption') if isinstance(m, dict) else None,
                    })
        except Exception as e:
            print(f"Job aids fetch error: {str(e)}")

        # 3. Failure modes (resolutions already reflect all contributions)
        try:
            failure_modes = get_failure_modes(equipment_id, company_id, conn=conn)
            if failure_modes:
                fm_text = "\nKnown Failure Modes:"
                for fm in failure_modes:
                    fm_text += f"\n- {fm['title']} (Status: {fm['status']})"
                    if fm['resolutions']:
                        fm_text += f"\n  Resolutions: {', '.join(fm['resolutions'])}"
                    sources["failure_modes"].append({
                        "id": str(fm['id']),
                        "title": fm['title'],
                        "status": fm['status'],
                    })
                context_parts.append(fm_text)
        except Exception as e:
            print(f"Failure modes fetch error: {str(e)}")

        # 4. Semantic search
        try:
            semantic_results = semantic_search(query, company_id, equipment_id, conn=conn)
            if semantic_results:
                sem_text = "\nAdditional relevant knowledge:"
                added = False
                for r in semantic_results:
                    if r['similarity'] > SEMANTIC_SIMILARITY_THRESHOLD:
                        sem_text += f"\n- {r['content']}"
                        sources["semantic"].append({
                            "source_type": r["source_type"],
                            "source_id": str(r["source_id"]),
                            "similarity": float(r["similarity"]),
                        })
                        added = True
                if added:
                    context_parts.append(sem_text)
        except Exception as e:
            print(f"Semantic search error: {str(e)}")

    context = "\n".join(context_parts) if context_parts else "No specific equipment knowledge found."
    return {"context": context, "sources": sources}