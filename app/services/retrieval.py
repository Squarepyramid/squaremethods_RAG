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
import re
from collections import defaultdict
from contextlib import contextmanager

import psycopg2.extras
from app.utils.db import get_db_connection
from app.services.embeddings import get_embedding

# Similarity floor for semantic search hits that are worth surfacing to the
# model / user. Was a magic number (0.7) inline before -- lowered to 0.3
# after diagnose_similarity.py showed 0.7 was unreachable in practice: for
# equipment_id 25070e8c-4118-4f04-9b61-c6a9e6a869b9 / query "the flight bar
# is not moving", the top real hit ("drive is accomplished through") scored
# only 0.4654, and the #2 hit ("not come in or out. a.- check") -- also
# plausibly relevant troubleshooting text -- scored 0.2882. Every one of
# the 19 chunks for that equipment scored well under 0.7, so at 0.7 this
# manual could never surface anything for this or likely most queries.
# The ranking itself looked sane (BOM/parts-list junk chunks scored under
# 0.15, prose chunks scored higher), ruling out an embedding-mismatch bug
# -- this genuinely was the threshold being unreachably high, not a data
# problem. 0.3 clears the two plausible hits above while still excluding
# the near-zero junk chunks. This is a first-pass value from ONE query
# against ONE equipment's manual, not a tuned constant -- validate against
# more queries (the diagnose_similarity.py docstring suggests a second one:
# "what is the working principle of this machine") before treating this as
# final, and revisit if chat answers start looking over-eager/off-topic
# (raise it) or still empty on clearly-covered questions (lower it further).
SEMANTIC_SIMILARITY_THRESHOLD = 0.25
SEMANTIC_CANDIDATE_LIMIT = 20
LEXICAL_CANDIDATE_LIMIT = 20
FINAL_SEMANTIC_LIMIT = 6
RRF_K = 60.0

# A weak similarity hit is not enough to declare a question covered.
# Coverage is determined after hybrid retrieval/reranking, not by the vector
# threshold alone. These values are intentionally conservative because this
# is maintenance knowledge, where a wrong answer is worse than a fallback.
MIN_RERANK_SCORE = 0.18
STRONG_RERANK_SCORE = 0.30

_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "be", "to", "of",
    "and", "or", "for", "on", "in", "at", "by", "with", "from", "this",
    "that", "it", "its", "what", "why", "how", "do", "does", "did",
    "can", "could", "would", "should", "my", "our", "your", "me",
    "i", "we", "you", "about", "into", "up", "down", "not", "no",
}

_INTENT_PATTERNS = {
    "troubleshooting": re.compile(
        r"\b(not working|won't|wont|doesn't|doesnt|cannot|can't|cant|stuck|no\s+movement|"
        r"not moving|won't move|wont move|stopped|fails?|failure|fault|alarm|problem|issue|trouble|"
        r"overheat|leak|jam|misalign|slipping|no\s+power|not\s+starting)\b", re.I
    ),
    "specification": re.compile(
        r"\b(rated|rating|spec|specification|capacity|speed|rpm|voltage|current|amps?|hp|horsepower|"
        r"pressure|temperature|dimension|weight|size|model|part\s*number|p/?n|serial)\b", re.I
    ),
    "procedure": re.compile(
        r"\b(how\s+do\s+i|how\s+to|steps?|procedure|instructions?|replace|remove|install|adjust|"
        r"change|lubricat|inspect|clean|reset|calibrat|maintain|maintenance|pm)\b", re.I
    ),
    "operation": re.compile(r"\b(operate|operation|working\s+principle|works?|start|stop|run)\b", re.I),
    "safety": re.compile(r"\b(safety|safe|hazard|warning|caution|lockout|loto|ppe)\b", re.I),
    "parts": re.compile(r"\b(part|parts|replacement|spare|component|p/?n|catalog)\b", re.I),
}


def _tokens(text: str) -> list:
    if not text:
        return []
    raw = re.findall(r"[a-z0-9]+(?:[-_/][a-z0-9]+)*", text.lower())
    return [t for t in raw if len(t) > 1 and t not in _STOPWORDS]


def classify_query(query: str) -> dict:
    """Deterministic query understanding used to improve retrieval, not to answer."""
    intents = [name for name, pattern in _INTENT_PATTERNS.items() if pattern.search(query or "")]
    if not intents:
        intents = ["general_equipment"]

    # Preserve multi-word symptom phrases because they are highly valuable for
    # maintenance troubleshooting retrieval (e.g. 'not moving').
    symptom_phrases = []
    for pattern in (
        r"\bnot\s+(?:moving|starting|stopping|feeding|cycling|coming|going)\b",
        r"\b(?:won't|wont|doesn't|doesnt|cannot|can't|cant)\s+[a-z0-9_-]+(?:\s+[a-z0-9_-]+){0,2}",
        r"\b(?:overheating|overheated|leaking|jammed|slipping|misaligned)\b",
    ):
        symptom_phrases.extend(m.group(0) for m in re.finditer(pattern, query or "", re.I))

    return {
        "intent": intents[0],
        "intents": intents,
        "tokens": _tokens(query),
        "symptoms": list(dict.fromkeys(symptom_phrases)),
    }


def _text_overlap_score(query_tokens: list, text: str) -> float:
    if not query_tokens or not text:
        return 0.0
    text_tokens = set(_tokens(text))
    if not text_tokens:
        return 0.0
    matched = sum(1 for t in set(query_tokens) if t in text_tokens)
    return matched / max(1, len(set(query_tokens)))


def _phrase_score(query: str, text: str) -> float:
    q = " ".join(_tokens(query))
    t = " ".join(_tokens(text))
    if not q or not t:
        return 0.0
    if q in t:
        return 1.0
    # Reward important symptom phrases even when the complete query is not a
    # contiguous phrase in the manual.
    for phrase in classify_query(query).get("symptoms", []):
        p = " ".join(_tokens(phrase))
        if p and p in t:
            return max(0.75, _phrase_score(query, text))
    return 0.0


def _rrf(rank: int, k: float = RRF_K) -> float:
    return 1.0 / (k + rank)


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


def build_job_aid_url(slug: str) -> str:
    """
    job_aids has both `slug` and `qrcode` columns, which strongly implies
    a slug-based public URL rather than one keyed by raw UUID -- that's
    what this builds. ASSUMPTION: the exact path
    (e.g. maybe it's "/ja/{slug}" or a QR-landing route, not
    "/job-aids/{slug}") -- confirm against your frontend router and
    adjust this one function if it's wrong.
    """
    return f"{APP_BASE_URL}/job-aids/{slug}"


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


def get_job_aids(equipment_id: str, company_id: str, query: str = None,
                 limit: int = 6, conn=None) -> list:
    """Return only relevant published job aids instead of every job aid on the equipment."""
    with _connection(conn) as c:
        with c.cursor() as cur:
            if query:
                cur.execute("""
                    SELECT ja.id, ja.title, ja.slug, ja.instruction, ja.category,
                           ja.estimated_duration, ja.status, ja.image,
                           ts_rank_cd(
                               to_tsvector('simple', concat_ws(' ', ja.title, ja.category, ja.instruction)),
                               plainto_tsquery('simple', %s)
                           ) AS lexical_rank
                    FROM job_aids ja
                    JOIN job_aid_equipment jae ON ja.id = jae.job_aid_id
                    WHERE jae.equipment_id = %s::uuid
                    AND ja.company_id = %s::uuid
                    AND ja.deleted_at IS NULL
                    AND ja.status = 'published'
                    AND to_tsvector('simple', concat_ws(' ', ja.title, ja.category, ja.instruction))
                        @@ plainto_tsquery('simple', %s)
                    ORDER BY lexical_rank DESC
                    LIMIT %s
                """, (query, equipment_id, company_id, query, limit))
                rows = cur.fetchall()
                if rows:
                    return rows

            # No lexical match means the job-aid library is not evidence for
            # this turn. Do not dump unrelated procedures into the prompt.
            return []


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


def get_failure_modes(equipment_id: str, company_id: str, query: str = None,
                      limit: int = 5, conn=None) -> list:
    """Return relevant failure modes/resolutions for this turn only."""
    with _connection(conn) as c:
        with c.cursor() as cur:
            if query:
                cur.execute("""
                    SELECT id, title, status, resolutions,
                           ts_rank_cd(
                               to_tsvector('simple', concat_ws(' ', title, resolutions::text)),
                               plainto_tsquery('simple', %s)
                           ) AS lexical_rank
                    FROM failure_modes
                    WHERE equipment_id = %s::uuid
                    AND company_id = %s::uuid
                    AND deleted_at IS NULL
                    AND to_tsvector('simple', concat_ws(' ', title, resolutions::text))
                        @@ plainto_tsquery('simple', %s)
                    ORDER BY lexical_rank DESC
                    LIMIT %s
                """, (query, equipment_id, company_id, query, limit))
                rows = cur.fetchall()
                if rows:
                    return rows

            # No lexical hit means the failure-mode library is not evidence
            # for this turn. Do not expose unrelated logged resolutions.
            return []


def _strip_manual_content_prefix(content: str) -> str:
    """
    Manual chunks (source_type='manual') are stored by ingest_document.py's
    save_chunks() with a fixed three-segment metadata prefix:
    "equipment_id:... | doc_id:... | bom_items:... | <actual chunk text>".
    That's there for clear_node_chunks()/clear_document_chunks() to find
    chunks by equipment/document -- it's not meant to be read by a human
    or an LLM. Strip it before surfacing content in the chat prompt;
    other source_types (e.g. squaremethods_import/squaremethods_wp from
    generate_job_aid.py) don't use this prefix, so this is a no-op for them.
    """
    if content.startswith("equipment_id:"):
        parts = content.split(" | ", 3)
        if len(parts) == 4:
            return parts[3]
    return content


def semantic_search(query: str, company_id: str, equipment_id: str = None,
                   limit: int = FINAL_SEMANTIC_LIMIT, conn=None) -> list:
    """Hybrid retrieval: vector candidates + PostgreSQL lexical candidates, then deterministic reranking.

    The vector search is intentionally broad (20 candidates by default). A
    second lexical search catches exact industrial terms, part numbers, model
    names, alarms, and symptom wording that embeddings can under-rank. The two
    ranked lists are merged with Reciprocal Rank Fusion and then reranked using
    token coverage and exact symptom/phrase matches.
    """
    try:
        embedding = get_embedding(query)
        embedding_str = "[" + ",".join(map(str, embedding)) + "]"
        with _connection(conn) as c:
            with c.cursor() as cur:
                equipment_clause = "AND equipment_id = %s::uuid" if equipment_id else ""
                vector_params = [embedding_str, company_id]
                if equipment_id:
                    vector_params.append(equipment_id)
                vector_params.extend([embedding_str, SEMANTIC_CANDIDATE_LIMIT])
                cur.execute(f"""
                    SELECT source_type, source_id, content, page_start, page_end, filename,
                           1 - (embedding <=> %s::vector) AS similarity
                    FROM knowledge_embeddings
                    WHERE company_id = %s::uuid
                    {equipment_clause}
                    ORDER BY embedding <=> %s::vector
                    LIMIT %s
                """, tuple(vector_params))
                vector_rows = cur.fetchall()

                lexical_params = [query, company_id]
                if equipment_id:
                    lexical_params.append(equipment_id)
                lexical_params.append(LEXICAL_CANDIDATE_LIMIT)
                cur.execute(f"""
                    SELECT source_type, source_id, content, page_start, page_end, filename,
                           ts_rank_cd(
                               to_tsvector('simple', coalesce(content, '')),
                               plainto_tsquery('simple', %s)
                           ) AS lexical_rank
                    FROM knowledge_embeddings
                    WHERE company_id = %s::uuid
                    {equipment_clause}
                    AND to_tsvector('simple', coalesce(content, '')) @@ plainto_tsquery('simple', %s)
                    ORDER BY lexical_rank DESC
                    LIMIT %s
                """, tuple([query, company_id] + ([equipment_id] if equipment_id else []) + [query, LEXICAL_CANDIDATE_LIMIT]))
                lexical_rows = cur.fetchall()

        # Merge by the actual stored chunk identity. Do not use source_id alone:
        # the same UUID space can be used by different source types.
        merged = {}
        for rank, row in enumerate(vector_rows, start=1):
            key = (row['source_type'], str(row['source_id']), row.get('page_start'), row.get('page_end'))
            item = dict(row)
            item['vector_rank'] = rank
            item['lexical_rank_position'] = None
            item['lexical_rank'] = 0.0
            merged[key] = item

        for rank, row in enumerate(lexical_rows, start=1):
            key = (row['source_type'], str(row['source_id']), row.get('page_start'), row.get('page_end'))
            if key not in merged:
                merged[key] = dict(row)
                merged[key]['similarity'] = 0.0
                merged[key]['vector_rank'] = None
            merged[key]['lexical_rank_position'] = rank
            merged[key]['lexical_rank'] = float(row.get('lexical_rank') or 0.0)

        query_info = classify_query(query)
        qtokens = query_info['tokens']
        ranked = []
        for item in merged.values():
            text = _strip_manual_content_prefix(item.get('content') or '')
            vector_rank = item.get('vector_rank')
            lexical_position = item.get('lexical_rank_position')
            vector_rrf = _rrf(vector_rank) if vector_rank else 0.0
            lexical_rrf = _rrf(lexical_position) if lexical_position else 0.0
            overlap = _text_overlap_score(qtokens, text)
            phrase = _phrase_score(query, text)
            similarity = max(0.0, float(item.get('similarity') or 0.0))

            # RRF handles the different scales of vector and lexical ranking.
            # The additional evidence terms prioritize exact symptom/component
            # language without pretending that lexical overlap alone proves
            # answerability.
            score = (
                0.42 * (vector_rrf / _rrf(1)) +
                0.28 * (lexical_rrf / _rrf(1)) +
                0.20 * overlap +
                0.10 * phrase
            )
            # Keep the raw vector similarity for diagnostics and a small tie
            # breaker so two otherwise identical RRF candidates remain stable.
            score += 0.05 * similarity
            item['content'] = text
            item['rerank_score'] = float(score)
            item['token_overlap'] = float(overlap)
            item['phrase_match'] = float(phrase)
            ranked.append(item)

        ranked.sort(key=lambda x: (x['rerank_score'], float(x.get('similarity') or 0.0)), reverse=True)
        return ranked[:limit]
    except Exception as e:
        print(f"Hybrid semantic/lexical search error: {str(e)}")
        return []


# NOTE: job aid creation used to live here as its own create_job_aid().
# It's gone -- tools.py now calls generate_job_aid.save_job_aid() directly
# so chat-created job aids go through the exact same insert path (job_aids
# + procedures + job_aid_equipment, slug generation, draft status) as
# every other job aid in the app, instead of a second parallel
# implementation that could drift out of sync.


def build_context(equipment_path: str, company_id: str, query: str) -> dict:
    """Build a query-focused evidence package for the chat model.

    The context is intentionally narrow: equipment metadata + relevant job aids
    + relevant failure modes + reranked hybrid manual evidence. The model is no
    longer handed every job aid/failure mode attached to the machine.
    """
    equipment_id = equipment_path.strip("/").split("/")[-1]
    query_info = classify_query(query)

    context_parts = []
    sources = {
        "equipment": None,
        "job_aids": [],
        "images": [],
        "failure_modes": [],
        "semantic": [],
    }

    with _connection() as conn:
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

        # Query-aware job-aid retrieval. We deliberately do not put every job
        # aid into the prompt; only the candidates relevant to this turn are
        # exposed as evidence.
        try:
            job_aids = get_job_aids(equipment_id, company_id, query=query, limit=6, conn=conn)
            if job_aids:
                job_aid_ids = [str(ja['id']) for ja in job_aids]
                procedures = get_procedures(job_aid_ids, company_id, conn=conn)

                for ja in job_aids:
                    ja_text = f"\nJob Aid: {ja['title']}"
                    if ja['category']:
                        ja_text += f" [{ja['category']}]"
                    if ja['instruction']:
                        ja_text += f"\nInstructions: {ja['instruction']}"

                    ja_procedures = [
                        p for p in procedures if str(p['job_aid_id']) == str(ja['id'])
                    ]
                    if ja_procedures:
                        ja_text += "\nSteps:"
                        for p in ja_procedures:
                            ja_text += f"\n  {p['step']}. {p['instruction']}"
                            if p['precautions']:
                                precautions = p['precautions']
                                if isinstance(precautions, (list, tuple)):
                                    precautions = ", ".join(str(x) for x in precautions)
                                ja_text += f" (Precautions: {precautions})"

                    job_aid_url = build_job_aid_url(ja['slug'])
                    ja_text += f"\nLink: {job_aid_url}"
                    context_parts.append(ja_text)

                    sources["job_aids"].append({
                        "id": str(ja['id']),
                        "title": ja['title'],
                        "category": ja['category'],
                        "url": job_aid_url,
                    })

                    if ja['image']:
                        sources["images"].append({
                            "job_aid_id": str(ja['id']),
                            "step": None,
                            "url": ja['image'],
                            "caption": ja['title'],
                        })
                    for p in ja_procedures:
                        if p['image']:
                            sources["images"].append({
                                "job_aid_id": str(ja['id']),
                                "step": p['step'],
                                "url": p['image'],
                                "caption": p.get('title') or f"Step {p['step']}",
                            })
        except Exception as e:
            print(f"Job aids fetch error: {str(e)}")

        # Query-aware failure-mode retrieval. Resolutions remain the aggregate
        # of logged contributions, but only relevant records are exposed.
        try:
            failure_modes = get_failure_modes(equipment_id, company_id, query=query, limit=5, conn=conn)
            if failure_modes:
                fm_text = "\nRelevant Known Failure Modes:"
                for fm in failure_modes:
                    fm_text += f"\n- {fm['title']} (Status: {fm['status']})"
                    if fm['resolutions']:
                        resolutions = fm['resolutions']
                        if isinstance(resolutions, (list, tuple)):
                            resolutions = ", ".join(str(x) for x in resolutions)
                        fm_text += f"\n  Resolutions: {resolutions}"
                    sources["failure_modes"].append({
                        "id": str(fm['id']),
                        "title": fm['title'],
                        "status": fm['status'],
                    })
                context_parts.append(fm_text)
        except Exception as e:
            print(f"Failure modes fetch error: {str(e)}")

        # Hybrid manual retrieval: 20 vector candidates + 20 lexical candidates,
        # merged/reranked down to a small evidence set.
        try:
            semantic_results = semantic_search(
                query, company_id, equipment_id,
                limit=FINAL_SEMANTIC_LIMIT, conn=conn
            )
            if semantic_results:
                sem_text = (
                    "\nFrom indexed manuals/documents (cite only the exact excerpt used; "
                    "do not infer missing specs, procedures, or page numbers):"
                )
                for r in semantic_results:
                    page_start, page_end = r.get("page_start"), r.get("page_end")
                    filename = r.get("filename")
                    if page_start is not None and page_end is not None:
                        page_ref = (
                            f" [page {page_start}]" if page_start == page_end
                            else f" [pages {page_start}-{page_end}]"
                        )
                    else:
                        page_ref = ""
                    doc_ref = f" [document: {filename}]" if filename else ""
                    sem_text += (
                        f"\n- [{r['source_type']}{doc_ref}{page_ref}; "
                        f"hybrid_score={r['rerank_score']:.3f}; "
                        f"vector_similarity={float(r.get('similarity') or 0.0):.3f}] "
                        f"{r['content']}"
                    )
                    sources["semantic"].append({
                        "source_type": r["source_type"],
                        "source_id": str(r["source_id"]),
                        "similarity": float(r.get("similarity") or 0.0),
                        "rerank_score": float(r.get("rerank_score") or 0.0),
                        "token_overlap": float(r.get("token_overlap") or 0.0),
                        "phrase_match": float(r.get("phrase_match") or 0.0),
                        "page_start": page_start,
                        "page_end": page_end,
                        "filename": filename,
                    })
                context_parts.append(sem_text)
        except Exception as e:
            print(f"Hybrid semantic search error: {str(e)}")

    top_score = max((s.get("rerank_score", 0.0) for s in sources["semantic"]), default=0.0)
    top_similarity = max((s.get("similarity", 0.0) for s in sources["semantic"]), default=0.0)
    exact_phrase = any(s.get("phrase_match", 0.0) >= 0.75 for s in sources["semantic"])

    # Evidence status is deliberately stricter than "we found something".
    # Related chunks do not automatically make a question answerable.
    if top_score >= STRONG_RERANK_SCORE or (top_score >= MIN_RERANK_SCORE and exact_phrase):
        retrieval_status = "covered"
    elif sources["semantic"] or sources["job_aids"] or sources["failure_modes"]:
        retrieval_status = "partially_covered"
    else:
        retrieval_status = "not_covered"

    context_parts.insert(0, (
        "RETRIEVAL STATUS: {status}\n"
        "QUERY INTENT: {intent}\n"
        "QUERY SYMPTOMS: {symptoms}\n"
        "RETRIEVAL NOTE: Related evidence is not automatically an answer. "
        "Use only evidence that addresses the specific question/symptom."
    ).format(
        status=retrieval_status.upper(),
        intent=query_info["intent"],
        symptoms=", ".join(query_info["symptoms"]) or "none explicitly detected",
    ))

    context = "\n".join(context_parts) if context_parts else "No specific equipment knowledge found."
    sources["retrieval"] = {
        "status": retrieval_status,
        "intent": query_info["intent"],
        "intents": query_info["intents"],
        "symptoms": query_info["symptoms"],
        "top_rerank_score": top_score,
        "top_vector_similarity": top_similarity,
        "semantic_candidate_limit": SEMANTIC_CANDIDATE_LIMIT,
        "lexical_candidate_limit": LEXICAL_CANDIDATE_LIMIT,
        "final_evidence_limit": FINAL_SEMANTIC_LIMIT,
    }
    return {"context": context, "sources": sources}