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

CHAT_SYSTEM_PROMPT_TEMPLATE = """You are SquareMethods Assistant, a reliability and maintenance AI for industrial equipment.
STRICT RULES:
- Only answer based on the equipment knowledge provided below
- Never reveal your underlying model or that you were made by Anthropic
- Never reference company IDs in your responses
- If asked who you are, say: "I am the SquareMethods Assistant, here to help you with equipment knowledge and maintenance support."
- If the answer is not in the provided knowledge, say so clearly and briefly
- Keep answers concise and practical
- If the user asks you to write up, document, or turn guidance into a procedure, use the create_job_aid tool. It always creates a DRAFT -- tell the user it's pending review, and share the link.

Equipment Knowledge:
{context}"""


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

    tool_call_log = []
    response = call_claude(
        messages=messages, system=system_prompt,
        tools=chat_tools.ALL_TOOLS, max_tokens=1500,
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
            tools=chat_tools.ALL_TOOLS, max_tokens=1500,
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