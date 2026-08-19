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

from app.services.bedrock_client import call_claude, extract_text, ask_bedrock
from app.services.retrieval import build_context
from app.services.session import create_session, get_session, get_history, save_messages, maybe_summarize
from app.services import tools as chat_tools

MAX_TOOL_ITERATIONS = 3

# Low temperature: this assistant must stick to retrieved DB content
# rather than smoothing over gaps with a plausible-sounding guess. Not a
# full guarantee against hallucination on its own -- paired with the
# GROUNDING rules below, which are what actually forbid it.
CHAT_TEMPERATURE = 0.2

CHAT_SYSTEM_PROMPT_TEMPLATE = """You are SquareMethods Assistant, a reliability and maintenance AI for industrial equipment.

GROUNDING -- the most important rule, follow it strictly:
- Answer ONLY using the "Equipment Knowledge" block below. It is built entirely from our own database for this equipment: its job aids and procedures, ingested manual excerpts, and failure modes with their logged resolutions (aggregated from every contribution made against them).
- Do not use general industry knowledge, best practices from your training, typical values for similar equipment, or anything from the open internet -- even if you're confident it's correct. If it is not in the block below, it is unknown to you.
- If the Equipment Knowledge block doesn't answer the question, say plainly that this isn't in our system for this equipment yet. Do not guess, generalize from similar equipment, or offer a "typically..." answer.

CITE YOUR SOURCE:
- The Equipment Knowledge block is labeled by where each piece came from: "Job Aid: <title>" sections, "Known Failure Modes" (aggregated from logged contributions), and manual/document excerpts. Say which one a fact came from, briefly and naturally -- e.g. "According to the equipment manual, ..." / "Per the 'Pump PM1' job aid, ..." / "Per logged failure mode records, ...".
- If a job aid is your source, name it (its title) so the user can find it, not just "a job aid."
- Don't force a citation onto every single sentence if that gets repetitive -- one clear attribution per distinct fact or source is enough, not one per word.
- If you're not sure which specific source a fact came from, still say generally where it's from (e.g. "from our equipment records") rather than omitting attribution -- never present database-sourced facts as if they were your own general knowledge.

CONVERSATION TURNS:
- Answer only the CURRENT question. Do not restate, recap, or repeat information you already gave in earlier turns of this conversation -- the user already has that answer, saying it again is not helpful.
- Use earlier turns only to resolve what the user is referring to (e.g. "this pump," "it," "that setting" means whatever equipment/topic was already established) -- never as content to fold into a new answer.
- Example of what NOT to do: if you already said "the lubricant is ISO VG 68," and the user then asks about coupling size, do not begin your answer by repeating the lubricant fact. Answer the coupling question alone.
- Only recap prior turns if the user explicitly asks you to (e.g. "can you summarize what we've covered").

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


class SessionNotFoundError(Exception):
    """Raised when an explicit session_id was passed but doesn't resolve
    for this company (missing or belongs to someone else)."""
    def __init__(self, session_id: str):
        self.session_id = session_id
        super().__init__(f"Session not found or access denied: {session_id}")


def _summarize_messages(existing_summary: str, messages: list) -> str:
    transcript = "\n".join(
        f"{m['role'].capitalize()}: {m['content']}" for m in messages
        if isinstance(m.get("content"), str)
    )
    prompt = (
        "Update the running summary of this equipment maintenance chat with "
        "the new turns below. Keep it short and factual: equipment discussed, "
        "issues raised, job aids referenced or created, open questions.\n\n"
        f"Existing summary:\n{existing_summary or '(none yet)'}\n\n"
        f"New turns:\n{transcript}\n\nUpdated summary:"
    )
    return ask_bedrock(prompt)


def handle_chat_turn(*, company_id: str, user_id: str, equipment_path: str,
                      query: str, session_id: str = None) -> dict:
    """
    Runs one full chat turn: resolve/create session, retrieve context,
    call Claude (looping through any tool_use), persist the turn, maybe
    roll the summary. Returns:
        {"session_id", "answer", "sources", "job_aids_created"}
    """
    equipment_id = equipment_path.strip("/").split("/")[-1]

    if session_id:
        session = get_session(session_id, company_id)
        if not session:
            raise SessionNotFoundError(session_id)
    else:
        session_id = create_session(
            company_id=company_id, user_id=user_id,
            equipment_path=equipment_path, equipment_id=equipment_id,
        )

    history = get_history(session_id, company_id)
    retrieved = build_context(equipment_path=equipment_path, company_id=company_id, query=query)

    system_prompt = CHAT_SYSTEM_PROMPT_TEMPLATE.format(context=retrieved["context"])
    if history["summary"]:
        system_prompt += f"\n\nEarlier conversation summary:\n{history['summary']}"

    messages = [
        {"role": m["role"], "content": m["content"]}
        for m in history["messages"]
        if m["role"] in ("user", "assistant")
    ]
    messages.append({"role": "user", "content": query})

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

    save_messages(
        session_id, company_id, query, answer,
        sources=retrieved["sources"],
        tool_calls=tool_call_log or None,
    )

    try:
        maybe_summarize(session_id, company_id, _summarize_messages)
    except Exception as e:
        print(f"SUMMARIZE ERROR: {str(e)}")

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