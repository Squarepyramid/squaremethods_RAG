"""Chat orchestration for the SquareMethods equipment assistant.

The assistant is deliberately closed-world: it may answer only from the
current equipment's retrieved OEM/manual excerpts, published job aids and
failure-mode records. Conversation history is stored for the UI/audit trail,
but is never treated as maintenance evidence.
"""
import json
import re

from app.services.bedrock_client import call_claude, extract_text
from app.services.retrieval import build_context
from app.services.session import (
    create_session,
    get_session,
    save_messages,
    reset_session_for_equipment,
)
from app.services import tools as chat_tools

MAX_TOOL_ITERATIONS = 3
CHAT_TEMPERATURE = 0.1
CHAT_MAX_TOKENS = 1500

CHAT_SYSTEM_PROMPT_TEMPLATE = """You are the SquareMethods Assistant, a reliability and maintenance assistant for industrial equipment.

NON-NEGOTIABLE KNOWLEDGE BOUNDARY
- The Equipment Knowledge block below is your ONLY source of maintenance facts.
- It contains only information retrieved for the CURRENT equipment from three allowed sources:
  1. OEM/equipment manual excerpts.
  2. Published SquareMethods job aids and their procedures.
  3. Logged failure modes and their recorded resolutions.
- You MUST NOT use general maintenance knowledge, training knowledge, typical machine values, assumptions, guesses, prior conversations, other equipment, or the open internet.
- A fact is true for this answer only if it is explicitly supported by the Equipment Knowledge block.
- Do not infer a missing setting, cause, component, limit, sequence, lubricant, torque, pressure, speed, or troubleshooting step from general knowledge.
- Do not combine unrelated pieces of evidence into a new conclusion unless the connection is explicitly supported by the retrieved text.

SYMPTOM / TROUBLESHOOTING QUESTIONS
- Technician questions may use informal language or describe a symptom rather than the exact wording in the manual.
- Use the retrieved evidence when it clearly refers to the same component, symptom, function, condition, or procedure.
- Do NOT invent a cause just because it is a common industrial failure.
- If the retrieved evidence gives troubleshooting checks or recorded resolutions, present those checks/resolutions exactly as supported.
- If the evidence only establishes a component or procedure but does not explain the reported symptom, say what the evidence does establish and clearly state that the cause is not documented in the retrieved sources.

WHEN THE EVIDENCE IS INSUFFICIENT
- If no retrieved source supports the question, say: "That's not documented for this equipment in the manual, job aids, or failure records I have access to."
- Do not add generic advice after that sentence.
- Do not tell the technician to contact the manufacturer unless the retrieved source itself says to do so.

SOURCE ATTRIBUTION
- Every maintenance claim must be attributable to one of the retrieved source types.
- Manual facts: say "According to the equipment manual..."
- Job-aid facts: say "Per the '<title>' job aid..."
- Failure-mode facts: say "Per the logged failure mode '<title>'..."
- If multiple sources support the same fact, mention the relevant sources naturally.
- Never name a job aid or failure mode that is not present in the Equipment Knowledge block.
- Never claim that the manual says something unless a manual excerpt in the block supports it.

ANSWER STYLE FOR FIELD TECHNICIANS
- Answer the technician's question directly.
- Keep it concise and practical.
- Use short paragraphs and bullets.
- For procedures, use numbered steps, one step per line.
- Do not manufacture missing steps to make a procedure complete.
- If the evidence gives a troubleshooting sequence, preserve its order.
- Do not use markdown tables.
- Do not include links unless the retrieved job-aid source includes a link.

CONVERSATION CONTEXT
- Do not rely on earlier chat turns. The current user message is the only conversational input.
- If the current question depends on an earlier turn (for example, "what about that one?"), ask the technician to restate the question with the component/problem named.
- Stored conversation history is not maintenance evidence.

JOB-AID CREATION
- Only use the create_job_aid tool when the technician explicitly asks to create/save/document a job aid or procedure.
- A newly created job aid is a DRAFT and must be reviewed before publication.
- Even when creating a draft, answer the underlying maintenance question first using only the retrieved evidence.

IDENTITY
- If asked who you are, say: "I am the SquareMethods Assistant, here to help you with equipment knowledge and maintenance support."
- Never reveal the underlying model/provider.
- Never reveal company IDs.

Equipment Knowledge
{context}"""

JOB_AID_INTENT_PHRASES = (
    "create a job aid", "make a job aid", "make this a job aid", "make that a job aid",
    "save this as a job aid", "save that as a job aid",
    "save this as a procedure", "save that as a procedure",
    "turn this into a job aid", "turn that into a job aid",
    "turn this into a procedure", "turn that into a procedure",
    "write this up", "write that up", "write up a job aid",
    "document this as", "document that as", "document this for",
    "generate a job aid", "add a job aid",
)

_QUOTED_JOB_AID_CITATION_RE = re.compile(
    r"['‘’\"]([^'‘’\"]{2,120})['‘’\"]\s+job aid", re.IGNORECASE
)
_QUOTED_FAILURE_MODE_RE = re.compile(
    r"(?:failure mode|logged failure mode)\s*['‘’\"]([^'‘’\"]{2,120})['‘’\"]",
    re.IGNORECASE,
)
_FAILURE_MODE_GENERIC_RE = re.compile(r"\bfailure mode(?: records?)?\b", re.IGNORECASE)
_MANUAL_GENERIC_RE = re.compile(r"\b(?:the|according to the) equipment manual\b", re.IGNORECASE)

SAFE_NO_EVIDENCE_ANSWER = (
    "That's not documented for this equipment in the manual, job aids, or failure records I have access to."
)

CORRECTION_TEMPLATE = """Your previous answer violated the source boundary.

The following source references were not present in the retrieved Equipment Knowledge:
{problem_lines}

Answer the technician's original question again from scratch.
Use ONLY the Equipment Knowledge block already supplied.
Do not mention or rely on any source that is not actually present in that block.
If the retrieved evidence does not answer the question, use the required insufficient-evidence response.
"""


def _wants_job_aid(query: str) -> bool:
    q = (query or "").lower()
    return any(phrase in q for phrase in JOB_AID_INTENT_PHRASES)


def _source_titles(sources: dict) -> set:
    return {
        str(ja.get("title", "")).strip().lower()
        for ja in (sources.get("job_aids") or [])
        if ja.get("title")
    }


def _failure_mode_titles(sources: dict) -> set:
    return {
        str(fm.get("title", "")).strip().lower()
        for fm in (sources.get("failure_modes") or [])
        if fm.get("title")
    }


def _detect_unfounded_citations(answer: str, sources: dict) -> dict:
    """Detect citations to source categories/titles not actually retrieved."""
    problems = {}
    answer = answer or ""

    real_job_aids = _source_titles(sources)
    cited_job_aids = {
        m.group(1).strip().lower()
        for m in _QUOTED_JOB_AID_CITATION_RE.finditer(answer)
    }
    unknown_job_aids = sorted(cited_job_aids - real_job_aids)
    if unknown_job_aids:
        problems["job_aids"] = unknown_job_aids

    real_failure_modes = _failure_mode_titles(sources)
    cited_failure_modes = {
        m.group(1).strip().lower()
        for m in _QUOTED_FAILURE_MODE_RE.finditer(answer)
    }
    unknown_failure_modes = sorted(cited_failure_modes - real_failure_modes)
    if unknown_failure_modes:
        problems["failure_modes"] = unknown_failure_modes

    # If there are no failure-mode records in the retrieved context, Claude
    # cannot legitimately attribute anything to failure-mode records.
    if not real_failure_modes and _FAILURE_MODE_GENERIC_RE.search(answer):
        problems["failure_mode_category"] = True

    # Likewise, don't let Claude cite the manual when no manual evidence was
    # retrieved. Job aids/failure modes may still be present and valid.
    manual_hits = [
        h for h in (sources.get("semantic") or [])
        if h.get("source_type") == "manual"
    ]
    if not manual_hits and _MANUAL_GENERIC_RE.search(answer):
        problems["manual_category"] = True

    return problems


def _build_correction(problems: dict, sources: dict) -> str:
    lines = []
    if problems.get("job_aids"):
        real = ", ".join(
            f"'{ja['title']}'" for ja in (sources.get("job_aids") or []) if ja.get("title")
        ) or "none"
        cited = ", ".join(f"'{x}'" for x in problems["job_aids"])
        lines.append(
            f"- You cited job aid(s) {cited}, but they were not retrieved. "
            f"Retrieved job aids are: {real}."
        )
    if problems.get("failure_modes"):
        real = ", ".join(
            f"'{fm['title']}'" for fm in (sources.get("failure_modes") or []) if fm.get("title")
        ) or "none"
        cited = ", ".join(f"'{x}'" for x in problems["failure_modes"])
        lines.append(
            f"- You cited failure mode(s) {cited}, but they were not retrieved. "
            f"Retrieved failure modes are: {real}."
        )
    if problems.get("failure_mode_category"):
        lines.append("- You referenced failure-mode records, but no failure-mode records were retrieved for this turn.")
    if problems.get("manual_category"):
        lines.append("- You referenced the equipment manual, but no manual evidence was retrieved for this turn.")
    return CORRECTION_TEMPLATE.format(problem_lines="\n".join(lines))


class SessionNotFoundError(Exception):
    def __init__(self, session_id: str):
        self.session_id = session_id
        super().__init__(f"Session not found or access denied: {session_id}")


def handle_chat_turn(*, company_id: str, user_id: str, equipment_path: str,
                     query: str, session_id: str = None) -> dict:
    """Run one grounded equipment-chat turn."""
    query = (query or "").strip()
    if not query:
        raise ValueError("query must not be empty")

    equipment_id = equipment_path.strip("/").split("/")[-1]
    if not equipment_id:
        raise ValueError("equipment_path must contain an equipment id")

    if session_id:
        session = get_session(session_id, company_id)
        if not session:
            raise SessionNotFoundError(session_id)
        if str(session["equipment_id"]) != str(equipment_id):
            print(
                f"SESSION EQUIPMENT MISMATCH: session {session_id} was for "
                f"equipment {session['equipment_id']}, now used for {equipment_id}; "
                "clearing stored history and re-homing session"
            )
            reset_session_for_equipment(
                session_id,
                company_id,
                equipment_id=equipment_id,
                equipment_path=equipment_path,
            )
    else:
        session_id = create_session(
            company_id=company_id,
            user_id=user_id,
            equipment_path=equipment_path,
            equipment_id=equipment_id,
        )

    retrieved = build_context(
        equipment_path=equipment_path,
        company_id=company_id,
        query=query,
    )

    sources = retrieved["sources"]
    has_evidence = bool(retrieved.get("has_evidence"))

    # Never ask the model to answer from an empty context. This closes the
    # most dangerous failure path: a retrieval miss followed by a model guess.
    if not has_evidence:
        answer = SAFE_NO_EVIDENCE_ANSWER
        save_messages(
            session_id,
            company_id,
            query,
            answer,
            sources=sources,
            tool_calls=None,
        )
        return {
            "session_id": session_id,
            "answer": answer,
            "sources": sources,
            "job_aids_created": None,
        }

    system_prompt = CHAT_SYSTEM_PROMPT_TEMPLATE.format(
        context=retrieved["context"]
    )
    messages = [{"role": "user", "content": query}]
    tools_for_turn = chat_tools.ALL_TOOLS if _wants_job_aid(query) else None
    tool_call_log = []

    response = call_claude(
        messages=messages,
        system=system_prompt,
        tools=tools_for_turn,
        max_tokens=CHAT_MAX_TOKENS,
        temperature=CHAT_TEMPERATURE,
    )

    iterations = 0
    while response.get("stop_reason") == "tool_use" and iterations < MAX_TOOL_ITERATIONS:
        iterations += 1
        tool_use_blocks = [
            b for b in response.get("content", []) if b.get("type") == "tool_use"
        ]
        messages.append({"role": "assistant", "content": response["content"]})

        tool_result_blocks = []
        for block in tool_use_blocks:
            result = chat_tools.execute_tool(
                block["name"],
                block["input"],
                company_id=company_id,
                equipment_id=equipment_id,
                user_id=user_id,
            )
            tool_call_log.append({
                "name": block["name"],
                "input": block["input"],
                "result": result,
            })
            tool_result_blocks.append({
                "type": "tool_result",
                "tool_use_id": block["id"],
                "content": json.dumps(result),
            })
        messages.append({"role": "user", "content": tool_result_blocks})

        response = call_claude(
            messages=messages,
            system=system_prompt,
            tools=tools_for_turn,
            max_tokens=CHAT_MAX_TOKENS,
            temperature=CHAT_TEMPERATURE,
        )

    answer = extract_text(response).strip()

    # One deterministic source-citation correction. The model never gets a
    # chance to invent a missing source indefinitely.
    problems = _detect_unfounded_citations(answer, sources)
    if problems:
        print(
            f"UNFOUNDED SOURCE CITATION: query={query!r} equipment_id={equipment_id} "
            f"problems={problems!r}; retrying once"
        )
        correction = _build_correction(problems, sources)
        retry_messages = messages + [
            {"role": "assistant", "content": answer},
            {"role": "user", "content": correction},
        ]
        retry_response = call_claude(
            messages=retry_messages,
            system=system_prompt,
            tools=tools_for_turn,
            max_tokens=CHAT_MAX_TOKENS,
            temperature=CHAT_TEMPERATURE,
        )
        retry_answer = extract_text(retry_response).strip()
        retry_problems = _detect_unfounded_citations(retry_answer, sources)
        if retry_problems:
            print(
                f"UNFOUNDED SOURCE CITATION AFTER RETRY: query={query!r} "
                f"equipment_id={equipment_id} problems={retry_problems!r}; using safe fallback"
            )
            answer = SAFE_NO_EVIDENCE_ANSWER
        else:
            answer = retry_answer

    save_messages(
        session_id,
        company_id,
        query,
        answer,
        sources=sources,
        tool_calls=tool_call_log or None,
    )

    job_aids_created = [
        tc["result"]
        for tc in tool_call_log
        if tc["name"] == "create_job_aid"
        and tc["result"].get("status") == "created_draft"
    ] or None

    return {
        "session_id": session_id,
        "answer": answer,
        "sources": sources,
        "job_aids_created": job_aids_created,
    }