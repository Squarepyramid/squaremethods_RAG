"""
Bedrock wrapper for Claude.

`ask_bedrock()` is kept exactly as it was (single prompt string -> text
answer) because generate_job_aid.py, generate_pm_strategy.py, and
ingest_document.py all call it that way today -- changing its signature
would break them for no reason.

`call_claude()` is new: it exposes Bedrock's actual Anthropic
Messages-compatible request shape (system prompt, a real multi-turn
`messages` list, and `tools`) instead of collapsing everything into one
string. This is what main.py's /chat route now uses so conversation
history is real role-tagged turns and the model can call the
create_job_aid tool. It returns the raw parsed response body (dict with
"content": [...blocks...] and "stop_reason") so the caller can drive a
tool-use loop.
"""
import json
import os

import boto3

MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "anthropic.claude-3-haiku-20240307-v1:0")
BEDROCK_REGION = os.environ.get("AWS_REGION", "ca-central-1")


def get_bedrock_client():
    return boto3.client("bedrock-runtime", region_name=BEDROCK_REGION)


def ask_bedrock(prompt: str) -> str:
    """Existing single-shot helper -- unchanged behavior for existing callers."""
    response = call_claude(messages=[{"role": "user", "content": prompt}])
    return extract_text(response)


def call_claude(*, messages: list, system: str = None, tools: list = None,
                 max_tokens: int = 1024, model_id: str = None,
                 temperature: float = None) -> dict:
    """
    messages: list of {"role": "user"|"assistant", "content": str | list[dict]}
    system:   optional top-level system prompt (kept separate from turns,
              unlike the old approach of stuffing rules into the prompt text)
    tools:    optional list of tool defs in Anthropic tool_use format
              (see tools.CREATE_JOB_AID_TOOL) -- Claude 3 on Bedrock
              supports the same "tools" shape as the direct Anthropic API.
    temperature: optional, 0-1. Lower makes answers more literal/deterministic
              and less likely to fill gaps with a plausible-sounding guess --
              chat_service.py uses a low value since answers must be strictly
              grounded in retrieved DB content. Omitted (None) leaves
              Bedrock's own default in place.

    Returns the parsed response body as-is. Check response["stop_reason"]:
    "tool_use" means response["content"] contains one or more
    {"type": "tool_use", "id", "name", "input"} blocks the caller must
    execute and feed back as a tool_result before the model will produce
    a final answer.
    """
    client = get_bedrock_client()

    payload = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if system:
        payload["system"] = system
    if tools:
        payload["tools"] = tools
    if temperature is not None:
        payload["temperature"] = temperature

    response = client.invoke_model(
        modelId=model_id or MODEL_ID,
        contentType="application/json",
        accept="application/json",
        body=json.dumps(payload),
    )
    return json.loads(response["body"].read())


def extract_text(response: dict) -> str:
    """Concatenates just the text blocks of a response -- ignores any
    tool_use blocks, so this is only correct for a response whose
    stop_reason isn't "tool_use"."""
    return "".join(
        block.get("text", "") for block in response.get("content", [])
        if block.get("type") == "text"
    )