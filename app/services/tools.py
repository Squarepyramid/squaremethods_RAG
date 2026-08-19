"""
Tool definitions for the equipment chat, in Anthropic Messages API
tool-use format (Bedrock's Claude 3 invoke_model accepts the same shape --
see bedrock_client.call_claude).

Only one tool for now: create_job_aid. It reuses
generate_job_aid.save_job_aid() -- the exact function your existing
/job-aids/generate endpoint uses -- so a chat-authored job aid lands in
the DB identically to any other one (same job_aids/procedures/
job_aid_equipment inserts, same slug scheme, same status='draft' start).
No separate write path to drift out of sync.

Deliberately NOT letting the model set image URLs on steps it writes:
your own generation prompts (build_pm_prompt/build_wp_prompt) are
explicit that image URLs must be copied verbatim from a knowledge record
and never invented. A chat-authored job aid has no such source, so every
step it creates gets image=None -- a human can attach real images during
review.
"""
from app.services.generate_job_aid import save_job_aid, make_slug
from app.services.retrieval import build_job_aid_url
from app.utils.db import get_db_connection

CREATE_JOB_AID_TOOL = {
    "name": "create_job_aid",
    "description": (
        "Create a DRAFT job aid (a step-by-step maintenance procedure) for "
        "the equipment currently in context. Only call this when the user "
        "has EXPLICITLY asked you to save, document, or turn something "
        "into a job aid or procedure -- never in response to an ordinary "
        "question, and never as a way to avoid answering directly. Always "
        "answer the user's question in your own text first; this tool "
        "just saves a copy. The job aid is saved as a draft for a human "
        "to review and publish -- it is never shown to technicians "
        "automatically. Tell the user it's a draft awaiting review and "
        "share the link."
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
                "description": "Category of work, e.g. 'Preventive Maintenance', 'Repair', 'Working Principle'.",
            },
            "instruction": {
                "type": "string",
                "description": "One to two sentence overview shown above the step list.",
            },
            "estimated_duration": {
                "type": "integer",
                "description": "Estimated total minutes to complete the job aid.",
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
    dict suitable for the `content` of a tool_result block.
    """
    if tool_name == "create_job_aid":
        return _handle_create_job_aid(tool_input, company_id=company_id,
                                       equipment_id=equipment_id, user_id=user_id)
    raise ValueError(f"Unknown tool: {tool_name}")


def _handle_create_job_aid(tool_input: dict, *, company_id: str,
                            equipment_id: str, user_id: str) -> dict:
    title = (tool_input.get("title") or "").strip()
    if not title:
        return {"status": "error", "message": "A title is required to create a job aid."}

    generated = {
        "title": title,
        "instruction": tool_input.get("instruction"),
        "category": tool_input.get("category") or "Chat-authored",
        "estimated_duration": tool_input.get("estimated_duration"),
        "procedures": [
            {
                "step": i,
                "title": step.get("title"),
                "instruction": step.get("instruction"),
                "type": "procedure",
                "precautions": step.get("precautions") or [],
                "image": None,  # see module docstring -- never model-supplied
            }
            for i, step in enumerate(tool_input.get("steps", []), start=1)
        ],
    }

    conn = get_db_connection()
    try:
        job_aid_id = save_job_aid(conn, generated, equipment_id, company_id, user_id)
        conn.commit()
    except Exception as e:
        conn.rollback()
        return {"status": "error", "message": f"Could not create job aid: {str(e)}"}
    finally:
        conn.close()

    # save_job_aid() computes slug = make_slug(title, job_aid_id) internally
    # but doesn't return it -- recomputing here is deterministic and avoids
    # an extra round trip.
    slug = make_slug(title, job_aid_id)
    step_count = len(generated["procedures"])

    return {
        "status": "created_draft",
        "job_aid_id": job_aid_id,
        "url": build_job_aid_url(slug),
        "step_count": step_count,
        "message": (
            f"Draft job aid '{title}' created with {step_count} step(s). "
            f"It is NOT published yet -- a reviewer needs to approve it "
            f"before technicians see it."
        ),
    }