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

CHANGE LOG (2026-08-28 -- reliability pass, same incident that produced
the generate_pm_strategy.py rewrite):
  - This client had NO botocore Config at all -- every caller (chat,
    ingest, job aid, and, until today, PM strategy generation via its
    own separate boto3 client) inherited botocore's undocumented default
    timeout and got a bare 0-retry-by-default client. A slow or throttled
    call anywhere in the app was silently exposed to this. Added
    BEDROCK_CLIENT_CONFIG below (connect_timeout, read_timeout, and
    total_max_attempts=6 in "standard" retry mode, matching AWS's current
    documented guidance for handling Bedrock throttling) -- this now
    protects every call site through this module, not just one.
  - get_bedrock_client() previously constructed a brand new boto3 client
    on every single call. Now lazily constructed once per process/warm
    Lambda container and reused -- cheaper, and it means a client-level
    Config (and, if you switch retries mode to "adaptive" later, its
    learned rate-limiting state) actually persists across calls within
    one warm container instead of resetting every invocation.
  - MODEL_ID default swapped off claude-3-haiku-20240307 (now legacy on
    Bedrock) to a Haiku 4.5-class model. This is now the SINGLE place
    that default lives -- previously generate_pm_strategy.py had its own
    separate copy of this same default, which meant "update the model"
    was two edits instead of one and easy to do inconsistently. VERIFY
    THIS EXACT MODEL ID IN YOUR OWN BEDROCK CONSOLE FOR YOUR REGION
    BEFORE DEPLOYING.
  - call_claude() gained a `tool_choice` parameter. It was previously
    impossible to force a specific tool call through this wrapper (only
    "let the model decide" via a bare `tools` list) -- generate_pm_strategy.py
    needs forced tool use to guarantee every extraction call actually
    returns structured data instead of prose.
  - call_claude() gained an optional `log_context` parameter (e.g.
    "[company=<id>] PM8") and now logs stop_reason, token usage, and a
    response preview on every call. This is the same diagnostic logging
    that was previously only inside generate_pm_strategy.py's own direct
    boto3 call -- centralizing it here means /chat and ingest_document.py
    get the same visibility into truncated/throttled responses that we
    relied on to actually diagnose the PM generation incidents. Passing
    log_context is optional and backward compatible; omitting it just
    drops the prefix.
"""
import json
import logging
import os
import threading

import boto3
from botocore.config import Config

log = logging.getLogger(__name__)

MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "anthropic.claude-haiku-4-5-20251001-v1:0")
BEDROCK_REGION = os.environ.get("AWS_REGION", "ca-central-1")

# Applies to every call through this module. total_max_attempts=6 in
# "standard" mode matches AWS's current documented guidance for handling
# Bedrock throttling (Scaling and throughput best practices, Amazon
# Bedrock user guide) -- previously there was no Config at all here.
BEDROCK_CLIENT_CONFIG = Config(
    connect_timeout=10,
    read_timeout=90,
    retries={"total_max_attempts": 6, "mode": "standard"},
)

_client = None
_client_lock = threading.Lock()


def get_bedrock_client():
    """
    Lazily constructed once per process (persists across warm Lambda
    invocations) and reused, instead of a fresh boto3 client per call.
    Thread-safe double-checked init since this can be called concurrently
    from multiple asyncio-executor threads (e.g. generate_pm_strategy.py's
    10 concurrent category calls).
    """
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = boto3.client(
                    "bedrock-runtime",
                    region_name=BEDROCK_REGION,
                    config=BEDROCK_CLIENT_CONFIG,
                )
    return _client


def ask_bedrock(prompt: str) -> str:
    """Existing single-shot helper -- unchanged behavior for existing callers."""
    response = call_claude(messages=[{"role": "user", "content": prompt}])
    return extract_text(response)


def call_claude(*, messages: list, system: str = None, tools: list = None,
                 tool_choice: dict = None, max_tokens: int = 1024,
                 model_id: str = None, temperature: float = None,
                 log_context: str = "") -> dict:
    """
    messages: list of {"role": "user"|"assistant", "content": str | list[dict]}
    system:   optional top-level system prompt (kept separate from turns,
              unlike the old approach of stuffing rules into the prompt text)
    tools:    optional list of tool defs in Anthropic tool_use format
              (see tools.CREATE_JOB_AID_TOOL) -- Claude on Bedrock
              supports the same "tools" shape as the direct Anthropic API.
    tool_choice: optional, e.g. {"type": "tool", "name": "some_tool"} to
              FORCE that specific tool to be called rather than letting
              the model decide whether to use one. Needed whenever the
              caller requires structured output every time, not just
              when the model happens to think a tool is useful --
              generate_pm_strategy.py's extraction calls are the reason
              this was added.
    temperature: optional, 0-1. Lower makes answers more literal/deterministic
              and less likely to fill gaps with a plausible-sounding guess --
              chat_service.py uses a low value since answers must be strictly
              grounded in retrieved DB content, and generate_pm_strategy.py
              uses 0 since it's a "find every matching task" extraction task
              with no reason to want sampling variance. Omitted (None) leaves
              Bedrock's own default in place.
    log_context: optional string prefixed to this call's log lines, e.g.
              "[company=<id>] PM8" -- lets a multi-tenant caller keep its
              own logs filterable by tenant/job/category without this
              module needing to know what a "tenant" or "PM type" is.

    Returns the parsed response body as-is. Check response["stop_reason"]:
    "tool_use" means response["content"] contains one or more
    {"type": "tool_use", "id", "name", "input"} blocks the caller must
    execute and feed back as a tool_result before the model will produce
    a final answer. "max_tokens" means the response was cut off before
    finishing -- logged below as a warning, but still the caller's job to
    decide what to do about it (retry, raise max_tokens, accept partial).
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
    if tool_choice:
        payload["tool_choice"] = tool_choice
    if temperature is not None:
        payload["temperature"] = temperature

    prefix = f"{log_context} " if log_context else ""

    raw_response = client.invoke_model(
        modelId=model_id or MODEL_ID,
        contentType="application/json",
        accept="application/json",
        body=json.dumps(payload),
    )
    raw_body = raw_response["body"].read()
    log.info(f"{prefix}RAW BODY LENGTH: {len(raw_body)} bytes")

    response = json.loads(raw_body)

    stop_reason = response.get("stop_reason")
    usage = response.get("usage", {})
    log.info(
        f"{prefix}stop_reason={stop_reason!r} "
        f"input_tokens={usage.get('input_tokens')} output_tokens={usage.get('output_tokens')}"
    )
    if stop_reason == "max_tokens":
        log.warning(
            f"{prefix}response hit max_tokens ({max_tokens}) before finishing -- "
            f"output is likely incomplete"
        )

    return response


def extract_text(response: dict) -> str:
    """Concatenates just the text blocks of a response -- ignores any
    tool_use blocks, so this is only correct for a response whose
    stop_reason isn't "tool_use"."""
    return "".join(
        block.get("text", "") for block in response.get("content", [])
        if block.get("type") == "text"
    )