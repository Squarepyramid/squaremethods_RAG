"""
SquareMethods - Job Aid Generation Service
==========================================
Place this file at: app/services/generate_job_aid.py

Generates up to two job aids per component in a single call:
  - Preventive Maintenance  (if squaremethods_import knowledge exists)
  - Working Principle       (if squaremethods_wp knowledge exists)

Each job aid is saved independently to the DB and linked to the
equipment node via job_aid_equipment — same pattern as import_pm_strategy.py.

If neither knowledge source has data for the component, raises ValueError.
No manual knowledge is used — Excel embeddings only.
"""

import json
import uuid
import logging
import re

from app.utils.db import get_db_connection
from app.services.bedrock_client import ask_bedrock
from app.services.embeddings import get_embedding

log               = logging.getLogger(__name__)
SOURCE_TYPE_PM    = "squaremethods_import"
SOURCE_TYPE_WP    = "squaremethods_wp"
SHARED_COMPANY_ID = "00000000-0000-0000-0000-000000000000"
TOP_K             = 10


# ── Knowledge retrieval ───────────────────────────────────────────────────────

def retrieve_knowledge(component_type: str) -> tuple:
    """
    Retrieve PM and WP knowledge for a component from shared embeddings.
    Returns (pm_knowledge, wp_knowledge) — either can be an empty list.
    No manual knowledge — Excel embeddings only.
    """
    embedding_str = "[" + ",".join(map(str, get_embedding(
        f"Component: {component_type} maintenance tasks and failure modes"
    ))) + "]"

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:

            # PM knowledge
            cur.execute("""
                SELECT content,
                       1 - (embedding <=> %s::vector) AS similarity
                FROM knowledge_embeddings
                WHERE source_type = %s
                  AND company_id  = %s::uuid
                  AND content ILIKE %s
                ORDER BY embedding <=> %s::vector
                LIMIT %s
            """, (
                embedding_str,
                SOURCE_TYPE_PM,
                SHARED_COMPANY_ID,
                f"%{component_type}%",
                embedding_str,
                TOP_K,
            ))
            pm_knowledge = [dict(r) for r in cur.fetchall()]

            # Working Principle knowledge
            cur.execute("""
                SELECT content,
                       1 - (embedding <=> %s::vector) AS similarity
                FROM knowledge_embeddings
                WHERE source_type = %s
                  AND company_id  = %s::uuid
                  AND content ILIKE %s
                ORDER BY embedding <=> %s::vector
                LIMIT %s
            """, (
                embedding_str,
                SOURCE_TYPE_WP,
                SHARED_COMPANY_ID,
                f"%{component_type}%",
                embedding_str,
                TOP_K,
            ))
            wp_knowledge = [dict(r) for r in cur.fetchall()]

        return pm_knowledge, wp_knowledge
    finally:
        conn.close()


# ── Prompt builders ───────────────────────────────────────────────────────────

def build_pm_prompt(component_type: str, knowledge: list) -> str:
    knowledge_text = "\n".join(
        f"Record {i}: {k['content']}"
        for i, k in enumerate(knowledge, 1)
    )

    return f"""You are a maintenance engineering expert generating a structured job aid for a manufacturing plant technician.

Component Type: {component_type}

Knowledge Records:
{knowledge_text}

Generate a single comprehensive preventive maintenance job aid for this component.

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
      "type": "procedure",
      "precautions": ["Safety or quality precaution if applicable"],
      "image": "Copy the Image URL from the knowledge record for this task exactly. If none, use null."
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
- estimated_duration should be realistic in minutes
- image: copy the URL exactly from the knowledge record. If no image exists for this step, use null."""


def build_wp_prompt(component_type: str, knowledge: list) -> str:
    knowledge_text = "\n".join(
        f"Record {i}: {k['content']}"
        for i, k in enumerate(knowledge, 1)
    )

    return f"""You are a maintenance engineering expert generating a working principle job aid for manufacturing plant technicians.

Component Type: {component_type}

Knowledge Records:
{knowledge_text}

Generate a structured working principle job aid that explains HOW this component works, step by step.

Return ONLY a valid JSON object with this exact structure. No markdown, no explanation, no extra text:

{{
  "title": "How [component] works",
  "instruction": "One sentence explaining why understanding this component's working principle matters for reliability.",
  "category": "Working Principle",
  "estimated_duration": <reading time in minutes as integer, typically 5-15>,
  "procedures": [
    {{
      "step": 1,
      "title": "Short step title from the knowledge record",
      "instruction": "Clear explanation a technician with no prior knowledge of this machine can follow.",
      "type": "procedure",
      "precautions": [],
      "image": "Copy the Image URL from the knowledge record for this step exactly. If none, use null."
    }}
  ]
}}

Rules:
- Follow the step sequence from the knowledge records exactly. Do not reorder, skip, or merge steps.
- image must be copied verbatim from the knowledge record. Never invent a URL. If none exists, use null.
- Keep instructions clear and mechanistic — explain cause and effect, not just what happens.
- precautions should be empty array unless a genuine safety note applies.
- estimated_duration is reading and learning time, not maintenance time."""


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_slug(title: str, unique_id: str) -> str:
    base   = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')
    suffix = unique_id[:8]
    return f"{base}-{suffix}"


def call_bedrock_and_parse(prompt: str) -> dict:
    raw_text = ask_bedrock(prompt)
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        clean = raw_text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        return json.loads(clean)


# ── DB writes ─────────────────────────────────────────────────────────────────

def save_job_aid(
    conn,
    generated: dict,
    equipment_id: str,
    company_id: str,
    created_by: str,
) -> str:
    """
    Save one generated job aid and its procedure steps.
    Accepts an open connection — caller handles commit/rollback.
    Returns job_aid_id.
    """
    job_aid_id = str(uuid.uuid4())
    slug       = make_slug(generated["title"], job_aid_id)

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
                     instruction, type, precautions, image,
                     created_at, updated_at)
                VALUES
                    (%s::uuid, %s::uuid, %s::uuid, %s, %s,
                     %s, 'procedure', %s, %s,
                     NOW(), NOW())
            """, (
                str(uuid.uuid4()),
                company_id,
                job_aid_id,
                step.get("title"),
                step.get("step"),
                step.get("instruction"),
                step.get("precautions", []),
                step.get("image"),
            ))

        # 3. Link job aid to equipment node
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

    log.info(f"Saved job aid {job_aid_id} ({generated['title']}) with {len(generated.get('procedures', []))} steps")
    return job_aid_id


def fetch_job_aid(conn, job_aid_id: str, company_id: str) -> dict:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, title, instruction, category,
                   estimated_duration, status
            FROM job_aids
            WHERE id         = %s::uuid
              AND company_id = %s::uuid
        """, (job_aid_id, company_id))
        job_aid = dict(cur.fetchone())

        cur.execute("""
            SELECT step, title, instruction, type, precautions, image
            FROM procedures
            WHERE job_aid_id = %s::uuid
              AND company_id = %s::uuid
              AND deleted_at IS NULL
            ORDER BY step
        """, (job_aid_id, company_id))
        job_aid["procedures"] = [dict(r) for r in cur.fetchall()]

    return job_aid


# ── Main generation function ──────────────────────────────────────────────────

def generate(
    component_type: str,
    equipment_id: str,
    company_id: str,
    created_by: str,
) -> dict:
    """
    Full generation pipeline called from the main.py endpoint.
    Signature unchanged from original.

    1. Retrieve PM and WP knowledge from shared embeddings
    2. Generate whichever job aids have knowledge available
    3. Save each independently to DB, linked to equipment
    4. Return summary matching the same pattern as import_pm_strategy.py

    Response:
    {
        "equipment_id":    "...",
        "job_aids_created": 2,
        "job_aids": [
            { "job_aid_id": "...", "category": "Preventive Maintenance", "title": "...", "steps": 5 },
            { "job_aid_id": "...", "category": "Working Principle",       "title": "...", "steps": 4 },
        ]
    }
    """
    pm_knowledge, wp_knowledge = retrieve_knowledge(component_type)

    if not pm_knowledge and not wp_knowledge:
        raise ValueError(
            f"No knowledge available for '{component_type}'. "
            f"Please update the import file and re-run the indexer."
        )

    log.info(
        f"Retrieved — PM: {len(pm_knowledge)} records, "
        f"WP: {len(wp_knowledge)} records for '{component_type}'"
    )

    conn        = get_db_connection()
    created     = []

    try:
        # Generate PM job aid if knowledge exists
        if pm_knowledge:
            pm_generated = call_bedrock_and_parse(
                build_pm_prompt(component_type, pm_knowledge)
            )
            pm_id = save_job_aid(conn, pm_generated, equipment_id, company_id, created_by)
            created.append({
                "job_aid_id": pm_id,
                "category":   pm_generated.get("category", "Preventive Maintenance"),
                "title":      pm_generated.get("title"),
                "steps":      len(pm_generated.get("procedures", [])),
            })

        # Generate Working Principle job aid if knowledge exists
        if wp_knowledge:
            wp_generated = call_bedrock_and_parse(
                build_wp_prompt(component_type, wp_knowledge)
            )
            wp_id = save_job_aid(conn, wp_generated, equipment_id, company_id, created_by)
            created.append({
                "job_aid_id": wp_id,
                "category":   wp_generated.get("category", "Working Principle"),
                "title":      wp_generated.get("title"),
                "steps":      len(wp_generated.get("procedures", [])),
            })

        conn.commit()

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    log.info(
        f"Generation complete. {len(created)} job aids created for "
        f"component '{component_type}' on equipment {equipment_id}"
    )

    return {
        "equipment_id":     equipment_id,
        "job_aids_created": len(created),
        "job_aids":         created,
    }