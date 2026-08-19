"""
Tool definitions for the equipment chat, in Anthropic Messages API
tool-use format (drop straight into the `tools=[...]` param of a
client.messages.create() call).

Only one tool for now: create_job_aid. The model decides to call it when
a user asks something like "turn this into a job aid" or "write up a
procedure for X". It always creates a DRAFT (see retrieval.create_job_aid)
-- nothing the assistant writes becomes visible to technicians without a
human publishing it.

If your orchestrator uses a different LLM/tool-calling convention
(OpenAI function calling, etc.), CREATE_JOB_AID_TOOL["input_schema"] is
still the JSON Schema you need; just re-wrap the outer envelope.
"""
from app.services import retrieval

CREATE_JOB_AID_TOOL = {
    "name": "create_job_aid",
    "description": (
        "Create a DRAFT job aid (a step-by-step maintenance procedure) for "
        "the equipment currently in context. Use this when the user asks "
        "you to write up, document, or turn a conversation into a job aid "
        "or procedure. The job aid is saved as a draft for a human to "
        "review and publish -- it is never shown to technicians "
        "automatically. Always tell the user it's a draft awaiting review "
        "and share the link so they can find it."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Short, specific title, e.g. 'Replace hydraulic filter on Pump 3'.",
            },
            "category": {
                "type": "string",
                "description": "Optional category/type of work, e.g. 'Preventive Maintenance', 'Repair'.",
            },
            "instruction": {
                "type": "string",
                "description": "Optional overview/summary shown above the step list.",
            },
            "estimated_duration": {
                "type": "string",
                "description": "Optional estimated time to complete, e.g. '45 minutes'.",
            },
            "steps": {
                "type": "array",
                "description": "Ordered list of procedure steps.",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Optional short step title."},
                        "instruction": {"type": "string", "description": "What to do in this step."},
                        "precautions": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional safety precautions specific to this step.",
                        },
                        "type": {
                            "type": "string",
                            "description": "Optional step type, e.g. 'step', 'inspection', 'safety'.",
                        },
                    },
                    "required": ["instruction"],
                },
            },
        },
        "required": ["title", "steps"],
    },
}

ALL_TOOLS = [CREATE_JOB_AID_TOOL]


def execute_tool(tool_name: str, tool_input: dict, *, company_id: str,
                  equipment_id: str, user_id: str) -> dict:
    """
    Dispatches a tool_use block to its handler and returns a JSON-safe
    dict suitable for the `content` of a tool_result block. Raises
    ValueError for an unknown tool name -- let the caller decide whether
    to surface that as a tool_result error or a hard failure.
    """
    if tool_name == "create_job_aid":
        return _handle_create_job_aid(tool_input, company_id=company_id,
                                       equipment_id=equipment_id, user_id=user_id)
    raise ValueError(f"Unknown tool: {tool_name}")


def _handle_create_job_aid(tool_input: dict, *, company_id: str,
                            equipment_id: str, user_id: str) -> dict:
    try:
        result = retrieval.create_job_aid(
            company_id=company_id,
            equipment_id=equipment_id,
            created_by_user_id=user_id,
            title=tool_input["title"],
            instruction=tool_input.get("instruction"),
            category=tool_input.get("category"),
            estimated_duration=tool_input.get("estimated_duration"),
            steps=tool_input.get("steps", []),
        )
        return {
            "status": "created_draft",
            "job_aid_id": result["id"],
            "url": result["url"],
            "step_count": result["step_count"],
            "message": (
                f"Draft job aid '{result['title']}' created with "
                f"{result['step_count']} step(s). It is NOT published yet "
                f"-- a reviewer needs to approve it before technicians see it."
            ),
        }
    except Exception as e:
        return {"status": "error", "message": f"Could not create job aid: {str(e)}"}