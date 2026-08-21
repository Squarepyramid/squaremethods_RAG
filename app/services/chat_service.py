"""
Chat orchestration, pulled out of main.py's /chat route to match the
pattern the rest of the app already uses (generate_job_aid_service,
ingest_document_service, etc. -- thin FastAPI routes, real logic in a
service module).

Works against the dict-shaped responses bedrock_client.call_claude()
actually returns (this replaces an earlier draft of this file that
assumed Anthropic SDK response objects -- wrong, since main.py calls
Bedrock's invoke_model directly via boto3 and json.loads()s the body).

Raises SessionNotFoundError instead of an HTTPException, so this module
has no FastAPI dependency -- the route layer decides how to translate
that into a response.
"""
import json
import re

from app.services.bedrock_client import call_claude, extract_text
from app.services.retrieval import build_context
from app.services.session import (
    create_session, get_session, save_messages,
    reset_session_for_equipment,
)
from app.services import tools as chat_tools

MAX_TOOL_ITERATIONS = 3

# Low temperature: this assistant must stick to retrieved DB content
# rather than smoothing over gaps with a plausible-sounding guess. Not a
# full guarantee against hallucination on its own -- paired with the
# GROUNDING rules below, which are what actually forbid it.
CHAT_TEMPERATURE = 0.2

CHAT_SYSTEM_PROMPT_TEMPLATE = """You are SquareMethods Assistant, a reliability and maintenance AI for industrial equipment.

NO CONVERSATION HISTORY -- THIS IS YOUR MOST IMPORTANT RULE:
- This message is the ONLY thing you know about this conversation. You are never shown any earlier turns, even from earlier in this same chat session -- there is no "before" for you to draw on.
- If the question only makes sense with context from an earlier turn (e.g. "what about that setting," "is it the same for this one"), say you don't have the earlier context and ask them to restate the full question -- do not guess what it might be referring to.
- If asked to summarize, recap, or repeat what was covered earlier in the conversation, say plainly that you don't have access to earlier turns and can only answer the question just asked -- never invent or reconstruct what an earlier turn might have said.

WHEN YOU DON'T HAVE THE ANSWER, BE BRIEF:
- If the Equipment Knowledge block doesn't cover the question, say so in one short sentence -- e.g. "That's not in our system for this equipment yet." Do not follow it with a paragraph of hedging, caveats, or suggestions to contact the manufacturer -- one sentence is enough.

GROUNDING:
- Answer ONLY using the "Equipment Knowledge" block below. It is built entirely from our own database for this equipment: its job aids and procedures, ingested manual excerpts, and failure modes with their logged resolutions (aggregated from every contribution made against them).
- Do not use general industry knowledge, best practices from your training, typical values for similar equipment, or anything from the open internet -- even if you're confident it's correct. If it is not in the block below, it is unknown to you.
- Retrieved content must match the SPECIFIC symptom or failure described, not just the same component or general topic. Mentioning the same part (e.g. "flight bar") is not enough -- an entry describing that part failing to MOVE does not cover a question about that same part failing to STOP, running continuously, overheating, or any other different or opposite symptom. Semantic search can return content that is merely topically related, not actually responsive; you must judge relevance yourself before answering, not assume anything retrieved is on-point just because it was retrieved.
- If nothing in the block addresses the specific symptom described, treat it as not covered -- follow the WHEN YOU DON'T HAVE THE ANSWER rule -- even when related content about the same component or equipment exists. Do not offer the closest topically-related entry as if it answers a different symptom; if you mention it at all, be explicit that it covers a different issue than the one asked about.

CITE YOUR SOURCE:
- The Equipment Knowledge block is labeled by where each piece came from: "Job Aid: <title>" sections, "Known Failure Modes" (aggregated from logged contributions), and manual/document excerpts. Say which one a fact came from, briefly and naturally -- e.g. "According to the equipment manual, ..." / "Per the 'Pump PM1' job aid, ..." / "Per logged failure mode records, ...".
- If a job aid is your source, name it (its title) so the user can find it, not just "a job aid."
- Don't force a citation onto every single sentence if that gets repetitive -- one clear attribution per distinct fact or source is enough, not one per word.
- If you're not sure which specific source a fact came from, still say generally where it's from (e.g. "from our equipment records") rather than omitting attribution -- never present database-sourced facts as if they were your own general knowledge.

FORMATTING -- a technician is reading this on a phone or tablet in the field, make it easy to scan:
- Never write one dense block of text. Break your answer into short paragraphs (1-3 sentences each), separated by a blank line.
- When you're giving more than one distinct fact, spec, or step, put each on its own line with a leading "-", not folded into a sentence together. For example, write:
  - Speed: 1750 RPM
  - Lubricant: ISO VG 68 hydraulic oil
  - Oil change interval: every 2,000 hours
  rather than "The speed is 1750 RPM and the lubricant is ISO VG 68 hydraulic oil, changed every 2,000 hours."
- Numbered steps (from a job aid/procedure) should be a numbered list, one step per line, not run together in prose.
- Keep it plain: no markdown headers, bold, or tables -- just short paragraphs, blank lines, and simple "-"/numbered lists, since those read fine whether or not the chat window renders markdown.

OTHER RULES:
- Never reveal your underlying model or that you were made by Anthropic
- Never reference company IDs in your responses
- If asked who you are, say: "I am the SquareMethods Assistant, here to help you with equipment knowledge and maintenance support."
- Keep answers concise and practical
- Always answer the user's question directly, in full, in your response text. Never reply with only a link, or only "I created a draft" -- a job aid (if you create one) is a saved copy of your answer, not a substitute for giving it.
- Only call create_job_aid if the user explicitly asked you to save, document, or turn something into a job aid/procedure -- and even then, still answer their underlying question in the text first, using only the Equipment Knowledge block. It always creates a DRAFT -- tell them it's pending review.

Equipment Knowledge (from our database only):
{context}"""

# Only offer the create_job_aid tool to the model when the user's message
# actually signals they want something saved/documented. Claude 3 Haiku
# reaches for tools eagerly, so relying on the system prompt alone let it
# treat ordinary "how do I..." questions as documentation requests and
# hide fabricated answers behind a job aid link instead of just answering.
# Gating at the request level means it's not offered the tool at all on a
# plain question -- it literally can't call it.
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


def _wants_job_aid(query: str) -> bool:
    q = query.lower()
    return any(phrase in q for phrase in JOB_AID_INTENT_PHRASES)


# Deterministic post-generation guard against a specific, confirmed-in-
# production pattern: the model citing a source category (a job aid, a
# failure mode) that was never actually retrieved for this turn. First
# caught as job-aid fabrication ("Per the 'Case Sealer Troubleshooting'
# job aid...") on equipment with ZERO job_aids rows at all (confirmed via
# direct DB query). The moment that specific citation shape was guarded
# against, the SAME underlying behavior resurfaced one turn later as
# "According to the known failure mode records..." instead -- so this is
# generalized across both categories rather than patched one label at a
# time; retrieved["sources"] is ground truth for exactly what was
# actually retrieved this turn (straight from get_job_aids()/
# get_failure_modes()), so any claim referencing a category that came
# back completely empty is fabricated with certainty, not suspicion.
#
# Two different precision levels, because the two fabrications we've
# actually seen had different shapes:
#   - job_aids: PRECISE. CHAT_SYSTEM_PROMPT_TEMPLATE's CITE YOUR SOURCE
#     section instructs a specific citation style ("Per the 'Pump PM1'
#     job aid, ..."), and both observed fabrications matched it exactly
#     -- so this extracts the quoted title and diffs it against the real
#     retrieved titles. Catches an invented EXTRA title even when some
#     real job aids exist for this equipment.
#   - failure_modes: COARSE. The two failure-mode fabrications we saw
#     did NOT share one consistent citation shape (one quoted a title,
#     one didn't), so this instead checks the category as a whole: if
#     retrieved["sources"]["failure_modes"] came back completely empty
#     for this turn, but the words "failure mode" appear anywhere in the
#     answer, that reference cannot be legitimate -- there were zero
#     real ones to reference. Coarser, no per-title diffing, but doesn't
#     depend on guessing a citation format.
_QUOTED_JOB_AID_CITATION_RE = re.compile(
    r"['‘’]([^'‘’]{2,80})['‘’]\s+job aid", re.IGNORECASE
)
_FAILURE_MODE_KEYWORD_RE = re.compile(r"failure mode", re.IGNORECASE)

UNFOUNDED_CITATION_FALLBACK_ANSWER = (
    "I don't have that on file in our system for this equipment -- "
    "that's not something I can point you to right now."
)

# Used to give the model ONE corrective retry when the guard fires,
# instead of immediately discarding the whole answer. Discarding
# outright is wrong when the Equipment Knowledge block also had real,
# correct content (e.g. manual excerpts) that the model ignored in
# favor of inventing a source -- the fabrication should be corrected,
# not used as an excuse to throw away an answer that was otherwise
# right there in the retrieved data. If the retry ALSO cites something
# unfounded, THEN fall back to UNFOUNDED_CITATION_FALLBACK_ANSWER -- no
# loop, exactly one extra attempt.
CITATION_CORRECTION_TEMPLATE = """CORRECTION -- your last answer to this exact question referenced source(s) that do not exist in our data for this equipment:

{problem_lines}

Answer the same question again from scratch, using only the manual excerpts (and any real job aids/failure modes listed above) already provided in the Equipment Knowledge block, without repeating any of the fabricated references. If the manual doesn't cover it either, say so in one short sentence per the WHEN YOU DON'T HAVE THE ANSWER rule."""


def _find_unfounded_job_aid_citations(answer: str, real_job_aids: list) -> list:
    """
    Returns the list of job aid titles `answer` cites (in the
    "'Title' job aid" style) that do NOT match any title in
    `real_job_aids` (retrieved["sources"]["job_aids"] for this same
    turn). Non-empty means the model named a source it was never given.
    """
    real_titles = {ja["title"].strip().lower() for ja in real_job_aids if ja.get("title")}
    cited_titles = {m.group(1).strip() for m in _QUOTED_JOB_AID_CITATION_RE.finditer(answer)}
    return [title for title in cited_titles if title.lower() not in real_titles]


def _detect_unfounded_citations(answer: str, sources: dict) -> dict:
    """
    Runs every category check against one turn's answer + its real
    retrieved sources. Returns a dict describing what was found, e.g.
    {"job_aids": ["Case Sealer Troubleshooting"], "failure_modes": True}
    -- an empty dict means nothing suspicious was found. Add a new
    category check here (e.g. for a future "manual" fabrication pattern)
    rather than bolting on a separate parallel guard elsewhere.
    """
    problems = {}

    unfounded_job_aids = _find_unfounded_job_aid_citations(answer, sources.get("job_aids") or [])
    if unfounded_job_aids:
        problems["job_aids"] = unfounded_job_aids

    if not (sources.get("failure_modes") or []) and _FAILURE_MODE_KEYWORD_RE.search(answer):
        problems["failure_modes"] = True

    return problems


def _build_citation_correction(problems: dict, sources: dict) -> str:
    """Turns a _detect_unfounded_citations() result into the correction
    message sent back to the model for its one retry."""
    lines = []

    if "job_aids" in problems:
        cited = ", ".join(f"'{t}'" for t in problems["job_aids"])
        real = sources.get("job_aids") or []
        real_desc = (
            ", ".join(f"'{ja['title']}'" for ja in real) if real
            else "none -- there are no job aids on file for this equipment"
        )
        lines.append(f"- You cited job aid(s) {cited}, which do not exist. "
                      f"The ONLY job aids that actually exist for this equipment are: {real_desc}")

    if "failure_modes" in problems:
        lines.append("- You referenced \"failure mode\" records, but there are NO logged "
                      "failure modes on file for this equipment at all -- do not mention "
                      "failure modes, known issues, or logged records in your answer.")

    return CITATION_CORRECTION_TEMPLATE.format(problem_lines="\n".join(lines))


class SessionNotFoundError(Exception):
    """Raised when an explicit session_id was passed but doesn't resolve
    for this company (missing or belongs to someone else). NOT raised for
    an equipment mismatch -- see the comment in handle_chat_turn(): a
    session existing for the wrong equipment is repaired in place
    (reset_session_for_equipment()), not treated as a failure."""
    def __init__(self, session_id: str):
        self.session_id = session_id
        super().__init__(f"Session not found or access denied: {session_id}")


def handle_chat_turn(*, company_id: str, user_id: str, equipment_path: str,
                      query: str, session_id: str = None) -> dict:
    """
    Runs one full chat turn: resolve/create session, retrieve context,
    call Claude (looping through any tool_use), persist the turn.
    Answers are generated from ONLY this turn's query + freshly retrieved
    context -- no prior conversation history is fed to the model, even
    within the same session (see the `messages` comment below for why).
    Returns:
        {"session_id", "answer", "sources", "job_aids_created"}
    """
    equipment_id = equipment_path.strip("/").split("/")[-1]

    if session_id:
        session = get_session(session_id, company_id)
        if not session:
            raise SessionNotFoundError(session_id)
        if str(session["equipment_id"]) != str(equipment_id):
            # Chat generation never reads this session's history (see
            # below -- messages is built from ONLY the current query,
            # nothing from get_history()), so a mismatch here can no
            # longer leak one equipment's answer into another's the way
            # it used to. This re-home + wipe still runs anyway, purely
            # so the STORED chat_messages rows for this session_id stay
            # coherent for one piece of equipment (e.g. if a UI ever
            # lists a session's past messages) rather than silently
            # mixing two equipment's turns under one session_id.
            print(f"SESSION EQUIPMENT MISMATCH: session {session_id} was "
                  f"for equipment {session['equipment_id']}, now used for "
                  f"{equipment_id} -- clearing its stored history and re-homing it")
            reset_session_for_equipment(
                session_id, company_id,
                equipment_id=equipment_id, equipment_path=equipment_path,
            )
    else:
        session_id = create_session(
            company_id=company_id, user_id=user_id,
            equipment_path=equipment_path, equipment_id=equipment_id,
        )

    retrieved = build_context(equipment_path=equipment_path, company_id=company_id, query=query)

    system_prompt = CHAT_SYSTEM_PROMPT_TEMPLATE.format(context=retrieved["context"])

    # Deliberately NOT built from get_history() -- each turn is answered
    # from this single query plus the freshly retrieved Equipment
    # Knowledge block only. No prior turns, no rolling summary. This is
    # what stops a model that once fabricated something (e.g. an
    # invented job aid) from treating its own earlier words as
    # established fact and building further invented detail on top of
    # them turn after turn -- a real failure mode we hit in production
    # even in a correctly equipment-scoped session, since grounding only
    # governs the Equipment Knowledge block, not what the model sees in
    # its own conversation history. Tradeoff: the model can no longer
    # resolve a follow-up like "what about that setting" against an
    # earlier turn, or produce a "here's what we covered" recap on
    # request -- see the system prompt's "NO CONVERSATION HISTORY"
    # section, which tells it to say so rather than guess.
    messages = [{"role": "user", "content": query}]

    # Tool is only offered when this specific message signals creation
    # intent -- see JOB_AID_INTENT_PHRASES above. tools=None means Claude
    # isn't given the option to call it at all this turn.
    tools_for_turn = chat_tools.ALL_TOOLS if _wants_job_aid(query) else None

    tool_call_log = []
    response = call_claude(
        messages=messages, system=system_prompt,
        tools=tools_for_turn, max_tokens=1500,
        temperature=CHAT_TEMPERATURE,
    )

    iterations = 0
    while response.get("stop_reason") == "tool_use" and iterations < MAX_TOOL_ITERATIONS:
        iterations += 1
        tool_use_blocks = [b for b in response["content"] if b["type"] == "tool_use"]
        messages.append({"role": "assistant", "content": response["content"]})

        tool_result_blocks = []
        for block in tool_use_blocks:
            result = chat_tools.execute_tool(
                block["name"], block["input"],
                company_id=company_id, equipment_id=equipment_id, user_id=user_id,
            )
            tool_call_log.append({"name": block["name"], "input": block["input"], "result": result})
            tool_result_blocks.append({
                "type": "tool_result",
                "tool_use_id": block["id"],
                "content": json.dumps(result),
            })
        messages.append({"role": "user", "content": tool_result_blocks})

        response = call_claude(
            messages=messages, system=system_prompt,
            tools=tools_for_turn, max_tokens=1500,
            temperature=CHAT_TEMPERATURE,
        )

    answer = extract_text(response)

    # See _detect_unfounded_citations()'s comment above. Rather than
    # discarding the answer outright the moment a fabricated citation is
    # caught, give the model ONE corrective retry -- the Equipment
    # Knowledge block may well have had the real answer (e.g. manual
    # excerpts) sitting right there, ignored in favor of inventing a
    # source; a flat fallback would throw that real content away along
    # with the fabrication. Exactly one retry, no loop: if the retry is
    # ALSO caught (even a different category than the first attempt --
    # e.g. it swaps a fabricated job aid for a fabricated failure mode,
    # which is exactly what happened in production), fall back to the
    # safe generic message rather than keep spending calls chasing a
    # clean answer.
    problems = _detect_unfounded_citations(answer, retrieved["sources"])
    if problems:
        print(f"UNFOUNDED CITATION (attempt 1): query={query!r} "
              f"equipment_id={equipment_id} problems={problems!r} "
              f"real_sources={retrieved['sources']!r} -- retrying once")

        correction = _build_citation_correction(problems, retrieved["sources"])
        retry_messages = messages + [
            {"role": "assistant", "content": answer},
            {"role": "user", "content": correction},
        ]
        retry_response = call_claude(
            messages=retry_messages, system=system_prompt,
            tools=tools_for_turn, max_tokens=1500,
            temperature=CHAT_TEMPERATURE,
        )
        retry_answer = extract_text(retry_response)
        retry_problems = _detect_unfounded_citations(retry_answer, retrieved["sources"])

        if retry_problems:
            # Logging the actual semantic hits (not just whether any
            # existed) is the point here, not just "still fabricated" --
            # this is what tells us, without having to reproduce the
            # query by hand again, whether the model ignored real manual
            # content that WAS retrieved (severe -- grounding failing
            # even with the right answer sitting in context) or whether
            # nothing relevant was retrieved at all (points back to
            # semantic_search()/SEMANTIC_SIMILARITY_THRESHOLD, not the
            # model). retrieved["sources"]["semantic"] has similarity
            # scores per hit; retrieved["context"] is the exact text the
            # model saw.
            semantic_hits = retrieved["sources"]["semantic"]
            print(f"UNFOUNDED CITATION (attempt 2, giving up): query={query!r} "
                  f"equipment_id={equipment_id} still problems={retry_problems!r} "
                  f"after correction -- using fallback. "
                  f"semantic_hits={semantic_hits!r} "
                  f"context_given={retrieved['context']!r}")
            answer = UNFOUNDED_CITATION_FALLBACK_ANSWER
        else:
            answer = retry_answer

    # Still saved for storage/display purposes (e.g. a UI showing a
    # session's past messages) -- just never read back into a future
    # call_claude() turn. See the comment above where `messages` is
    # built for why generation no longer touches this.
    save_messages(
        session_id, company_id, query, answer,
        sources=retrieved["sources"],
        tool_calls=tool_call_log or None,
    )

    # No maybe_summarize() call here anymore -- the rolling summary it
    # produced was only ever consumed by the "Earlier conversation
    # summary" block in system_prompt, which no longer exists. Calling
    # it now would just be a wasted Bedrock call computing a summary
    # nothing reads. session.py's maybe_summarize()/get_history() still
    # exist and still work if some other future feature wants a
    # conversation summary -- they're just not wired into this turn
    # anymore. Flagging rather than deleting them outright, since unlike
    # update_session_equipment()/get_active_session() (removed earlier
    # for being unused AND actively wrong), these are unused-for-now but
    # not wrong -- worth a deliberate call on whether to keep them.

    job_aids_created = [
        tc["result"] for tc in tool_call_log
        if tc["name"] == "create_job_aid" and tc["result"].get("status") == "created_draft"
    ] or None

    return {
        "session_id": session_id,
        "answer": answer,
        "sources": retrieved["sources"],
        "job_aids_created": job_aids_created,
    }