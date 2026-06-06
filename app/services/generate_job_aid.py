"""
SquareMethods - Job Aid Generation Service
==========================================
Place this file at: app/services/generate_job_aid.py

Contains all helper functions for job aid generation.
Called from main.py endpoint.

IMPORTANT - Two separate knowledge sources:
  - source_type='squaremethods_import', company_id=NULL
      Shared across all companies. Used ONLY for job aid generation.
      Populated by squaremethods_indexer.py script.

  - source_type='job_aid' or 'failure_mode', company_id=<uuid>
      Scoped per company. Used ONLY for chat in retrieval.py.
      Never mixed with squaremethods_import records.
"""

import json
import uuid
import logging
import re
from typing import Optional

from app.utils.db import get_db_connection
from app.services.bedrock_client import ask_bedrock
from app.services.embeddings import get_embedding

log         = logging.getLogger(__name__)
SOURCE_TYPE       = "squaremethods_import"
SHARED_COMPANY_ID = "00000000-0000-0000-0000-000000000000"
TOP_K             = 10


# ── Knowledge retrieval ───────────────────────────────────────────────────────

def retrieve_knowledge(component_type: str) -> list:
    """
    Semantic search against squaremethods_import records only.
    No company_id filter - these records are shared across all companies.
    """
    embedding     = get_embedding(f"Component: {component_type} maintenance tasks and failure modes")
    embedding_str = "[" + ",".join(map(str, embedding)) + "]"

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    content,
                    1 - (embedding <=> %s::vector) AS similarity
                FROM knowledge_embeddings
                WHERE source_type = %s
                AND company_id = %s::uuid
                AND content ILIKE %s
                ORDER BY embedding <=> %s::vector
                LIMIT %s
            """, (
                embedding_str,
                SOURCE_TYPE,
                SHARED_COMPANY_ID,
                f"%{component_type}%",
                embedding_str,
                TOP_K
            ))
            rows = cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ── Prompt builder ────────────────────────────────────────────────────────────

def build_prompt(component_type: str, knowledge: list) -> str:
    knowledge_text = "\n".join(
        f"Record {i}: {k['content']}"
        for i, k in enumerate(knowledge, 1)
    )

    return f"""You are a maintenance engineering expert generating a structured job aid for a manufacturing plant technician.

Component Type: {component_type}

Knowledge base records for this component:
{knowledge_text}

Generate a single comprehensive job aid for this component that covers all the key failure modes and maintenance tasks identified above.

Return ONLY a valid JSON object with this exact structure. No markdown, no explanation, no extra text:

{{
  "title": "Short descriptive title for the job aid",
  "instruction": "One to two sentence overview of what this job aid covers and why it matters",
  "category": "Preventive Maintenance",
  "estimated_duration": <total estimated minutes as integer>,
  "procedures": [
    {{
      "step": 1,
      "title": "Short step title",
      "instruction": "Clear work instruction a technician can follow without prior knowledge of this machine",
      "type": "action",
      "precautions": ["Safety or quality precaution if applicable"]
    }}
  ]
}}

Rules:
- type must always be "procedure"
- Write instructions for a competent technician who does not know this specific machine
- Each step must be specific and actionable, not vague
- Include safety precautions where relevant, empty array if none
- Cover all failure modes from the knowledge records
- Sequence steps logically: isolate equipment, inspect, measure, correct, verify, return to service
- estimated_duration should be realistic in minutes"""


# ── DB writes ─────────────────────────────────────────────────────────────────

def make_slug(title: str, unique_id: str) -> str:
    """Generate a URL-safe slug from title plus short unique suffix."""
    base = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')
    suffix = unique_id[:8]
    return f"{base}-{suffix}"


def save_job_aid(company_id: str, equipment_id: str, created_by: str, generated: dict) -> str:
    job_aid_id = str(uuid.uuid4())
    slug       = make_slug(generated["title"], job_aid_id)

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:

            # 1. Insert job aid
            cur.execute("""
                INSERT INTO job_aids
                    (id, company_id, title, slug, instruction, status,
                     estimated_duration, category, created_by,
                     view_count, scan_count, created_at, updated_at)
                VALUES
                    (%s::uuid, %s::uuid, %s, %s, %s, 'draft',
                     %s, %s, %s::uuid,
                     0, 0, NOW(), NOW())
            """, (
                job_aid_id,
                company_id,
                generated["title"],
                slug,
                generated.get("instruction"),
                generated.get("estimated_duration"),
                generated.get("category", "Preventive Maintenance"),
                created_by,
            ))

            # 2. Insert procedure steps
            for step in generated.get("procedures", []):
                cur.execute("""
                    INSERT INTO procedures
                        (id, company_id, job_aid_id, title, step,
                         instruction, type, precautions,
                         created_at, updated_at)
                    VALUES
                        (%s::uuid, %s::uuid, %s::uuid, %s, %s,
                         %s, %s, %s,
                         NOW(), NOW())
                """, (
                    str(uuid.uuid4()),
                    company_id,
                    job_aid_id,
                    step.get("title"),
                    step.get("step"),
                    step.get("instruction"),
                    step.get("type", "procedure"),
                    step.get("precautions", []),
                ))

            # 3. Link job aid to component node
            cur.execute("""
                INSERT INTO job_aid_equipment
                    (id, company_id, job_aid_id, equipment_id,
                     created_at, updated_at)
                VALUES
                    (%s::uuid, %s::uuid, %s::uuid, %s::uuid,
                     NOW(), NOW())
            """, (
                str(uuid.uuid4()),
                company_id,
                job_aid_id,
                equipment_id,
            ))

        conn.commit()
        return job_aid_id

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def fetch_job_aid(job_aid_id: str, company_id: str) -> dict:
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, title, instruction, category,
                       estimated_duration, status
                FROM job_aids
                WHERE id = %s::uuid
                AND company_id = %s::uuid
            """, (job_aid_id, company_id))
            job_aid = dict(cur.fetchone())

            cur.execute("""
                SELECT step, title, instruction, type, precautions
                FROM procedures
                WHERE job_aid_id = %s::uuid
                AND company_id = %s::uuid
                AND deleted_at IS NULL
                ORDER BY step
            """, (job_aid_id, company_id))
            job_aid["procedures"] = [dict(r) for r in cur.fetchall()]

        return job_aid
    finally:
        conn.close()


# ── Main generation function ──────────────────────────────────────────────────

def generate(component_type: str, equipment_id: str, company_id: str, created_by: str) -> dict:
    """
    Full generation pipeline called from the main.py endpoint.
    1. Retrieve knowledge
    2. Build prompt and call Claude
    3. Parse response
    4. Save to DB
    5. Return saved job aid
    """
    knowledge = retrieve_knowledge(component_type)

    if not knowledge:
        raise ValueError(
            f"No knowledge records found for component type '{component_type}'. "
            f"Please run squaremethods_indexer.py first."
        )

    log.info(f"Retrieved {len(knowledge)} records for '{component_type}'")

    prompt   = build_prompt(component_type, knowledge)
    raw_text = ask_bedrock(prompt)

    # Parse Claude response - strip markdown fences if present
    try:
        generated = json.loads(raw_text)
    except json.JSONDecodeError:
        clean     = raw_text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        generated = json.loads(clean)

    job_aid_id = save_job_aid(company_id, equipment_id, created_by, generated)
    log.info(f"Job aid created: {job_aid_id}")

    return fetch_job_aid(job_aid_id, company_id)