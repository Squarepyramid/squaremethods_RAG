"""Grounded retrieval for SquareMethods equipment chat.

Only three knowledge sources are eligible for an answer:
  1. OEM/equipment manual chunks stored in knowledge_embeddings.
  2. Published job aids and their procedures.
  3. Equipment failure modes and their logged resolutions.

The retrieval strategy is hybrid. Manual retrieval uses both vector similarity
and PostgreSQL full-text/lexical matching so technician symptoms such as
"flight bar is not moving" can find manual wording such as "flight bar fails
to reciprocate" without lowering the evidence standard to arbitrary semantic
matches. Job aids and failure modes are ranked deterministically against the
technician's words before being placed in the prompt.
"""
import os
import re
from contextlib import contextmanager

from app.utils.db import get_db_connection
from app.services.embeddings import get_embedding

# Vector search is intentionally broader than the old 0.70 hard cutoff, but
# vector hits alone are not enough: they are combined with lexical evidence.
SEMANTIC_MIN_SIMILARITY = 0.55
SEMANTIC_STRONG_SIMILARITY = 0.68
SEMANTIC_CANDIDATE_LIMIT = 12
MANUAL_CONTEXT_LIMIT = 8
JOB_AID_CONTEXT_LIMIT = 5
FAILURE_MODE_CONTEXT_LIMIT = 5

APP_BASE_URL = os.environ.get("APP_BASE_URL", "https://app.example.com").rstrip("/")

# Small deterministic stop-word list. We intentionally do not use an LLM to
# rewrite/expand the technician's query because the retrieval layer must not
# introduce outside maintenance knowledge.
_STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "been", "being", "by",
    "can", "could", "do", "does", "doesnt", "doesn't", "for", "from",
    "how", "i", "if", "in", "is", "it", "its", "me", "my", "not", "of",
    "on", "or", "our", "please", "should", "that", "the", "their", "this",
    "to", "was", "what", "when", "why", "will", "with", "would", "you",
    "your", "we", "are", "have", "has", "had", "hasnt", "haven't", "there",
    "about", "into", "just", "now", "one", "same", "tell", "show", "want",
}


def _connection(conn=None):
    """Context manager that reuses a connection when supplied."""
    @contextmanager
    def manager():
        owns_conn = conn is None
        active = conn or get_db_connection()
        try:
            yield active
        finally:
            if owns_conn:
                active.close()
    return manager()


def build_job_aid_url(slug: str) -> str:
    return f"{APP_BASE_URL}/job-aids/{slug}"


def _query_terms(query: str) -> list[str]:
    words = re.findall(r"[a-zA-Z0-9][a-zA-Z0-9_/-]*", (query or "").lower())
    terms = []
    for word in words:
        if word in _STOP_WORDS or len(word) < 3:
            continue
        if word not in terms:
            terms.append(word)
    return terms


def _token_set(text: str) -> set[str]:
    return set(_query_terms(text or ""))


def _lexical_score(query: str, text: str) -> float:
    """Simple deterministic overlap score used for job aids/failure modes."""
    q = _token_set(query)
    if not q:
        return 0.0
    t = _token_set(text)
    overlap = len(q & t)
    if not overlap:
        return 0.0
    # Reward exact phrase presence without allowing it to dominate completely.
    phrase_bonus = 0.25 if query.strip().lower() in (text or "").lower() else 0.0
    return min(1.0, overlap / len(q) + phrase_bonus)


def get_equipment(equipment_id: str, company_id: str, conn=None) -> dict:
    with _connection(conn) as c:
        with c.cursor() as cur:
            cur.execute("""
                SELECT e.id, e.name, e.reference_code, e.notes, e.status,
                       et.name AS equipment_type
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
                SELECT ja.id, ja.title, ja.slug, ja.instruction, ja.category,
                       ja.estimated_duration, ja.status, ja.image
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
                       p.precautions, p.type, p.image
                FROM procedures p
                WHERE p.job_aid_id = ANY(%s::uuid[])
                  AND p.company_id = %s::uuid
                  AND p.deleted_at IS NULL
                ORDER BY p.job_aid_id, p.step
            """, (job_aid_ids, company_id))
            return cur.fetchall()


def get_failure_modes(equipment_id: str, company_id: str, conn=None) -> list:
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


def _strip_manual_content_prefix(content: str) -> str:
    if content and content.startswith("equipment_id:"):
        parts = content.split(" | ", 3)
        if len(parts) == 4:
            return parts[3]
    return content or ""


def semantic_search(query: str, company_id: str, equipment_id: str = None,
                    limit: int = SEMANTIC_CANDIDATE_LIMIT, conn=None) -> list:
    """Return manual-only vector candidates for the current equipment.

    The source_type='manual' restriction is deliberate: job aids and failure
    modes have their own structured retrieval paths and should not enter the
    manual evidence channel through an unrelated embedding.
    """
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
                          AND equipment_id = %s::uuid
                          AND source_type = 'manual'
                        ORDER BY embedding <=> %s::vector
                        LIMIT %s
                    """, (
                        embedding_str,
                        company_id,
                        equipment_id,
                        embedding_str,
                        limit,
                    ))
                else:
                    cur.execute("""
                        SELECT source_type, source_id, content,
                               1 - (embedding <=> %s::vector) AS similarity
                        FROM knowledge_embeddings
                        WHERE company_id = %s::uuid
                          AND source_type = 'manual'
                        ORDER BY embedding <=> %s::vector
                        LIMIT %s
                    """, (embedding_str, company_id, embedding_str, limit))
                return cur.fetchall()
    except Exception as exc:
        print(f"Semantic search error: {exc}")
        return []


def lexical_manual_search(query: str, company_id: str, equipment_id: str,
                          limit: int = SEMANTIC_CANDIDATE_LIMIT,
                          conn=None) -> list:
    """Find manual chunks using PostgreSQL full-text + exact phrase matching.

    This is the safety net for symptom language that differs from the manual's
    wording. It searches only manual chunks belonging to the current company
    and equipment.
    """
    try:
        with _connection(conn) as c:
            with c.cursor() as cur:
                cur.execute("""
                    SELECT source_type, source_id, content,
                           ts_rank_cd(
                               to_tsvector('simple', content),
                               plainto_tsquery('simple', %s)
                           ) AS lexical_score,
                           CASE
                               WHEN lower(content) LIKE lower(%s) THEN 1.0
                               ELSE 0.0
                           END AS phrase_score
                    FROM knowledge_embeddings
                    WHERE company_id = %s::uuid
                      AND equipment_id = %s::uuid
                      AND source_type = 'manual'
                      AND (
                          to_tsvector('simple', content) @@ plainto_tsquery('simple', %s)
                          OR lower(content) LIKE lower(%s)
                      )
                    ORDER BY phrase_score DESC, lexical_score DESC
                    LIMIT %s
                """, (
                    query,
                    f"%{query}%",
                    company_id,
                    equipment_id,
                    query,
                    f"%{query}%",
                    limit,
                ))
                return cur.fetchall()
    except Exception as exc:
        print(f"Lexical manual search error: {exc}")
        return []


def _merge_manual_results(semantic_results: list, lexical_results: list) -> list:
    """Merge vector and lexical candidates without duplicating chunks."""
    merged = {}
    for row in semantic_results or []:
        key = (str(row.get("source_type")), str(row.get("source_id")))
        merged[key] = {
            **row,
            "similarity": float(row.get("similarity") or 0.0),
            "lexical_score": 0.0,
            "phrase_score": 0.0,
        }
    for row in lexical_results or []:
        key = (str(row.get("source_type")), str(row.get("source_id")))
        if key not in merged:
            merged[key] = {
                **row,
                "similarity": 0.0,
                "lexical_score": float(row.get("lexical_score") or 0.0),
                "phrase_score": float(row.get("phrase_score") or 0.0),
            }
        else:
            merged[key]["lexical_score"] = max(
                merged[key].get("lexical_score", 0.0),
                float(row.get("lexical_score") or 0.0),
            )
            merged[key]["phrase_score"] = max(
                merged[key].get("phrase_score", 0.0),
                float(row.get("phrase_score") or 0.0),
            )

    for row in merged.values():
        row["evidence_score"] = min(
            1.0,
            max(
                row.get("similarity", 0.0),
                row.get("lexical_score", 0.0),
                row.get("phrase_score", 0.0),
            )
        )
    return sorted(
        merged.values(),
        key=lambda r: (
            r.get("phrase_score", 0.0),
            r.get("evidence_score", 0.0),
            r.get("similarity", 0.0),
            r.get("lexical_score", 0.0),
        ),
        reverse=True,
    )


def _select_manual_results(merged: list, query: str) -> list:
    """Keep only defensible manual evidence.

    A result is accepted when it has strong vector similarity, or meaningful
    lexical evidence, or an exact phrase match. This removes the old behavior
    where a single 0.69 result meant "nothing found" while also preventing a
    weak semantic neighbor from becoming evidence.
    """
    selected = []
    for row in merged:
        similarity = row.get("similarity", 0.0)
        lexical = row.get("lexical_score", 0.0)
        phrase = row.get("phrase_score", 0.0)
        if (
            phrase >= 1.0
            or lexical >= 0.05
            or similarity >= SEMANTIC_MIN_SIMILARITY
        ):
            selected.append(row)
        if len(selected) >= MANUAL_CONTEXT_LIMIT:
            break
    return selected


def _job_aid_relevance(query: str, job_aid: dict, procedures: list) -> float:
    related = [p for p in procedures if str(p["job_aid_id"]) == str(job_aid["id"])]
    text_parts = [
        job_aid.get("title"),
        job_aid.get("category"),
        job_aid.get("instruction"),
    ]
    for procedure in related:
        text_parts.extend([
            procedure.get("title"),
            procedure.get("instruction"),
            " ".join(procedure.get("precautions") or []),
        ])
    return _lexical_score(query, " ".join(str(x or "") for x in text_parts))


def _failure_mode_relevance(query: str, failure_mode: dict) -> float:
    resolutions = failure_mode.get("resolutions") or []
    return _lexical_score(
        query,
        " ".join([
            str(failure_mode.get("title") or ""),
            str(failure_mode.get("status") or ""),
            " ".join(str(x) for x in resolutions),
        ]),
    )


def build_context(equipment_path: str, company_id: str, query: str) -> dict:
    """Build only evidence relevant to the current equipment and question."""
    equipment_id = equipment_path.strip("/").split("/")[-1]
    context_parts = []
    sources = {
        "equipment": None,
        "job_aids": [],
        "images": [],
        "failure_modes": [],
        "semantic": [],
    }
    retrieval_debug = {
        "manual_candidates": 0,
        "manual_selected": 0,
        "job_aid_candidates": 0,
        "job_aids_selected": 0,
        "failure_mode_candidates": 0,
        "failure_modes_selected": 0,
    }

    with _connection() as conn:
        # Equipment identity is context, not maintenance evidence by itself.
        try:
            equipment = get_equipment(equipment_id, company_id, conn=conn)
            if equipment:
                context_parts.append(
                    f"CURRENT EQUIPMENT IDENTITY (not maintenance evidence):\n"
                    f"Name: {equipment['name']}\n"
                    f"Type: {equipment['equipment_type']}\n"
                    f"Reference code: {equipment['reference_code']}\n"
                    f"Status: {equipment['status']}"
                )
                if equipment.get("notes"):
                    context_parts.append(f"Equipment notes: {equipment['notes']}")
                sources["equipment"] = {
                    "id": str(equipment["id"]),
                    "name": equipment["name"],
                    "status": equipment["status"],
                }
        except Exception as exc:
            print(f"Equipment fetch error: {exc}")

        # Published job aids + procedures. Rank them before adding to context
        # so an equipment with hundreds of job aids does not flood the model.
        try:
            job_aids = get_job_aids(equipment_id, company_id, conn=conn)
            retrieval_debug["job_aid_candidates"] = len(job_aids)
            if job_aids:
                job_aid_ids = [str(ja["id"]) for ja in job_aids]
                procedures = get_procedures(job_aid_ids, company_id, conn=conn)
                ranked = sorted(
                    job_aids,
                    key=lambda ja: _job_aid_relevance(query, ja, procedures),
                    reverse=True,
                )
                selected_job_aids = [
                    ja for ja in ranked
                    if _job_aid_relevance(query, ja, procedures) > 0
                ][:JOB_AID_CONTEXT_LIMIT]
                retrieval_debug["job_aids_selected"] = len(selected_job_aids)

                for ja in selected_job_aids:
                    relevance = _job_aid_relevance(query, ja, procedures)
                    ja_procedures = [
                        p for p in procedures
                        if str(p["job_aid_id"]) == str(ja["id"])
                    ]
                    ja_text = f"Job Aid: {ja['title']}"
                    if ja.get("category"):
                        ja_text += f" [{ja['category']}]"
                    ja_text += f"\nRelevance to current question: {relevance:.2f}"
                    if ja.get("instruction"):
                        ja_text += f"\nInstructions: {ja['instruction']}"
                    if ja_procedures:
                        ja_text += "\nSteps:"
                        for p in ja_procedures:
                            ja_text += f"\n{p['step']}. {p['instruction']}"
                            if p.get("precautions"):
                                ja_text += f" (Precautions: {', '.join(p['precautions'])})"
                    job_aid_url = build_job_aid_url(ja["slug"])
                    ja_text += f"\nLink: {job_aid_url}"
                    context_parts.append(ja_text)

                    sources["job_aids"].append({
                        "id": str(ja["id"]),
                        "title": ja["title"],
                        "category": ja["category"],
                        "url": job_aid_url,
                    })

                    if ja.get("image"):
                        sources["images"].append({
                            "job_aid_id": str(ja["id"]),
                            "step": None,
                            "url": ja["image"],
                            "caption": ja["title"],
                        })
                    for p in ja_procedures:
                        if p.get("image"):
                            sources["images"].append({
                                "job_aid_id": str(ja["id"]),
                                "step": p["step"],
                                "url": p["image"],
                                "caption": p.get("title") or f"Step {p['step']}",
                            })
        except Exception as exc:
            print(f"Job aids fetch error: {exc}")

        # Failure modes are structured company knowledge. Only relevant
        # records are placed in the prompt; resolutions remain untouched.
        try:
            failure_modes = get_failure_modes(equipment_id, company_id, conn=conn)
            retrieval_debug["failure_mode_candidates"] = len(failure_modes)
            ranked_failure_modes = sorted(
                failure_modes,
                key=lambda fm: _failure_mode_relevance(query, fm),
                reverse=True,
            )
            selected_failure_modes = [
                fm for fm in ranked_failure_modes
                if _failure_mode_relevance(query, fm) > 0
            ][:FAILURE_MODE_CONTEXT_LIMIT]
            retrieval_debug["failure_modes_selected"] = len(selected_failure_modes)

            if selected_failure_modes:
                fm_text = "Known Failure Modes / Logged Resolutions:"
                for fm in selected_failure_modes:
                    relevance = _failure_mode_relevance(query, fm)
                    fm_text += f"\nFailure Mode: {fm['title']} (Status: {fm['status']}; relevance: {relevance:.2f})"
                    if fm.get("resolutions"):
                        fm_text += "\nRecorded resolutions:"
                        for resolution in fm["resolutions"]:
                            fm_text += f"\n- {resolution}"
                    sources["failure_modes"].append({
                        "id": str(fm["id"]),
                        "title": fm["title"],
                        "status": fm["status"],
                    })
                context_parts.append(fm_text)
        except Exception as exc:
            print(f"Failure modes fetch error: {exc}")

        # Manual retrieval is hybrid: vector + lexical. Both are equipment-
        # scoped and source_type='manual', so nothing outside the OEM/manual
        # channel can enter this section.
        try:
            semantic_results = semantic_search(
                query, company_id, equipment_id, conn=conn
            )
            lexical_results = lexical_manual_search(
                query, company_id, equipment_id, conn=conn
            )
            merged = _merge_manual_results(semantic_results, lexical_results)
            selected_manual = _select_manual_results(merged, query)
            retrieval_debug["manual_candidates"] = len(merged)
            retrieval_debug["manual_selected"] = len(selected_manual)

            if selected_manual:
                manual_text = (
                    "OEM / Equipment Manual Evidence:\n"
                    "The following excerpts are retrieved from the current equipment's indexed manual. "
                    "They are the only manual facts available for this answer."
                )
                for row in selected_manual:
                    manual_text += (
                        f"\n\n[Manual evidence | evidence score {row['evidence_score']:.3f}; "
                        f"vector {row.get('similarity', 0.0):.3f}; "
                        f"lexical {row.get('lexical_score', 0.0):.3f}]\n"
                        f"{_strip_manual_content_prefix(row['content'])}"
                    )
                    sources["semantic"].append({
                        "source_type": "manual",
                        "source_id": str(row["source_id"]),
                        "similarity": float(row.get("similarity") or 0.0),
                        "lexical_score": float(row.get("lexical_score") or 0.0),
                        "evidence_score": float(row.get("evidence_score") or 0.0),
                    })
                context_parts.append(manual_text)
        except Exception as exc:
            print(f"Manual retrieval error: {exc}")

    # Maintenance evidence exists only when at least one manual excerpt,
    # relevant published job aid, or relevant failure mode was retrieved.
    has_evidence = bool(
        sources["semantic"]
        or sources["job_aids"]
        or sources["failure_modes"]
    )

    context = "\n\n".join(context_parts) if context_parts else "No maintenance evidence was retrieved."
    return {
        "context": context,
        "sources": sources,
        "has_evidence": has_evidence,
        "retrieval_debug": retrieval_debug,
    }