import psycopg2.extras
from app.utils.db import get_db_connection
from app.services.embeddings import get_embedding


def get_equipment(equipment_id: str, company_id: str) -> dict:
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
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
    finally:
        conn.close()


def get_job_aids(equipment_id: str, company_id: str) -> list:
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
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
    finally:
        conn.close()


def get_procedures(job_aid_ids: list, company_id: str) -> list:
    if not job_aid_ids:
        return []
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
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
    finally:
        conn.close()


def get_failure_modes(equipment_id: str, company_id: str) -> list:
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT title, status, resolutions
                FROM failure_modes
                WHERE equipment_id = %s::uuid
                AND company_id = %s::uuid
                AND deleted_at IS NULL
            """, (equipment_id, company_id))
            return cur.fetchall()
    finally:
        conn.close()


def semantic_search(query: str, company_id: str, equipment_id: str = None, limit: int = 5) -> list:
    try:
        embedding = get_embedding(query)
        embedding_str = "[" + ",".join(map(str, embedding)) + "]"
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
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
        finally:
            conn.close()
    except Exception as e:
        print(f"Semantic search error: {str(e)}")
        return []


def build_context(equipment_path: str, company_id: str, query: str) -> str:
    equipment_id = equipment_path.strip("/").split("/")[-1]

    context_parts = []

    # 1. Equipment details
    try:
        equipment = get_equipment(equipment_id, company_id)
        if equipment:
            context_parts.append(
                f"Equipment: {equipment['name']} "
                f"(Type: {equipment['equipment_type']}, "
                f"Code: {equipment['reference_code']}, "
                f"Status: {equipment['status']})"
            )
            if equipment['notes']:
                context_parts.append(f"Equipment Notes: {equipment['notes']}")
    except Exception as e:
        print(f"Equipment fetch error: {str(e)}")

    # 2. Job aids and procedures
    try:
        job_aids = get_job_aids(equipment_id, company_id)
        if job_aids:
            job_aid_ids = [str(ja['id']) for ja in job_aids]
            procedures = get_procedures(job_aid_ids, company_id)

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

                context_parts.append(ja_text)
    except Exception as e:
        print(f"Job aids fetch error: {str(e)}")

    # 3. Failure modes
    try:
        failure_modes = get_failure_modes(equipment_id, company_id)
        if failure_modes:
            fm_text = "\nKnown Failure Modes:"
            for fm in failure_modes:
                fm_text += f"\n- {fm['title']} (Status: {fm['status']})"
                if fm['resolutions']:
                    fm_text += f"\n  Resolutions: {', '.join(fm['resolutions'])}"
            context_parts.append(fm_text)
    except Exception as e:
        print(f"Failure modes fetch error: {str(e)}")

    # 4. Semantic search
    try:
        semantic_results = semantic_search(query, company_id, equipment_id)
        if semantic_results:
            sem_text = "\nAdditional relevant knowledge:"
            added = False
            for r in semantic_results:
                if r['similarity'] > 0.7:
                    sem_text += f"\n- {r['content']}"
                    added = True
            if added:
                context_parts.append(sem_text)
    except Exception as e:
        print(f"Semantic search error: {str(e)}")

    return "\n".join(context_parts) if context_parts else "No specific equipment knowledge found."