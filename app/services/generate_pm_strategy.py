"""
SquareMethods - PM Strategy Generation Service
==============================================
Place this file at: app/services/generate_pm_strategy.py

Pulls all manual chunks for an equipment node, then runs ten
parallel async Claude calls (Working Principle + nine PM types) to
extract and structure maintenance tasks into a downloadable Excel
file. Each call also resolves stock component images for its own
steps inline, concurrently with the other calls.

The Excel output matches the PM_strategy.xlsx format exactly so
the reviewer can fill gaps, add image URLs, then upload it via
the import endpoint.

PM Types:
  WP  - Working Principle (step-by-step operating procedure, not a
        maintenance task -- placed first as the foundational section
        the rest of the strategies build on)
  PM1 - Inspection
  PM2 - Lubrication
  PM3 - Calibration
  PM4 - Replacements
  PM5 - Overhaul
  PM6 - Condition Monitoring
  PM7 - Cleaning
  PM8 - Safety Inspection
  PM9 - Software Back-up
"""

import io
import json
import logging
import asyncio
import re
from typing import Optional

import httpx
import boto3
import os
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment

from app.utils.db import get_db_connection

log = logging.getLogger(__name__)

WORKING_PRINCIPLE = ("WP", "Working Principle")

# Working Principle is placed first: it's the foundational "how this
# equipment operates" narrative that the PM tasks below build on. It runs
# through the same async gather as the PM types but uses its own prompt
# (see build_working_principle_prompt) since it isn't a maintenance task
# extraction -- there's no frequency, work_needed, or failure mode in the
# same sense as PM1-PM9.
PM_TYPES = [
    WORKING_PRINCIPLE,
    ("PM1", "Inspection"),
    ("PM2", "Lubrication"),
    ("PM3", "Calibration"),
    ("PM4", "Replacements"),
    ("PM5", "Overhaul"),
    ("PM6", "Condition Monitoring"),
    ("PM7", "Cleaning"),
    ("PM8", "Safety Inspection"),
    ("PM9", "Software Back-up"),
]

BEDROCK_MODEL   = "anthropic.claude-3-haiku-20240307-v1:0"
BEDROCK_REGION  = os.environ.get("AWS_REGION", "ca-central-1")
MAX_TOKENS      = 4096

COLUMNS = [
    "Operation",
    "Task List Description",
    "Frequency",
    "Hrs",
    "Work Needed",
    "System Condition",
    "Material Number",
    "Component",
    "Long Text (Instruction)",
    "Failure Modes",
    "Image",
]

HEADER_FILL   = PatternFill("solid", start_color="1F4E79", end_color="1F4E79")
HEADER_FONT   = Font(bold=True, color="FFFFFF", name="Arial", size=10)
SUBHEAD_FILL  = PatternFill("solid", start_color="D6E4F0", end_color="D6E4F0")
SUBHEAD_FONT  = Font(bold=True, name="Arial", size=10)
DATA_FONT     = Font(name="Arial", size=10)
WRAP_ALIGN    = Alignment(wrap_text=True, vertical="top")

# Approx characters that fit on one wrapped line within the instruction
# column width (col width 60 -> roughly 60-65 chars/line at Arial 10).
INSTRUCTION_COL_CHARS_PER_LINE = 60
MIN_ROW_HEIGHT = 40
LINE_HEIGHT = 14  # points per wrapped line, Arial 10


# ── Knowledge retrieval ───────────────────────────────────────────────────────

def fetch_all_manual_chunks(equipment_id: str, company_id: str) -> str:
    """
    Fetch ALL manual chunks for this equipment node and concatenate
    them into a single text block for Claude to reason over.
    No vector search -- full recall by equipment_id prefix match.
    Handles both old and new content prefix formats.
    """
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT content
                FROM knowledge_embeddings
                WHERE source_type = 'manual'
                AND company_id = %s::uuid
                AND content LIKE %s
                ORDER BY created_at
            """, (company_id, f"equipment_id:{equipment_id}%"))
            rows = cur.fetchall()
    finally:
        conn.close()

    if not rows:
        raise ValueError(
            f"No manual chunks found for equipment {equipment_id}. "
            "Please upload and ingest a document for this node first."
        )

    clean_chunks = []
    for r in rows:
        content = r["content"]
        # Split on " | " to strip prefixes
        # Old format: "equipment_id:{id} | actual content"
        # New format: "equipment_id:{id} | doc_id:{uuid} | actual content"
        parts = content.split(" | ", 2)
        if len(parts) == 3:
            clean_chunks.append(parts[2])   # new format
        elif len(parts) == 2:
            clean_chunks.append(parts[1])   # old format
        else:
            clean_chunks.append(content)    # fallback

    full_text = "\n\n".join(clean_chunks)
    log.info(f"Fetched {len(clean_chunks)} chunks ({len(full_text.split())} words) for equipment {equipment_id}")
    return full_text


# ── Claude call ───────────────────────────────────────────────────────────────

def build_working_principle_prompt(manual_text: str) -> str:
    """
    Working Principle answers "how does this machine work?" -- the
    engineering logic of how the equipment physically achieves its
    function, NOT how an operator runs it and NOT maintenance tasks.

    The example below is intentionally abstract/generic (no real
    equipment domain, no specific component names) rather than a
    concrete worked example (e.g. an air compressor). An earlier
    version used a real compressor example, and the model latched
    onto that specific vocabulary (screw airend, oil injection,
    receiver-separator) and started projecting compressor terminology
    onto completely unrelated equipment. The example here only
    demonstrates the JSON shape and the *kind* of engineering
    reasoning expected -- every actual term must come from the
    manual provided, never from this example.
    """
    return f"""You are a maintenance engineering expert. You have been given an equipment manual below.

Your task is to extract the WORKING PRINCIPLE of THIS SPECIFIC MACHINE -- the engineering explanation of HOW IT PHYSICALLY ACHIEVES ITS FUNCTION. This answers the question "how does this machine work?", not "how does an operator run it?"

Do NOT extract:
- Operator procedures: button presses, switch positions, start/stop sequences, towing/setup instructions.
- Maintenance tasks: inspection, lubrication, replacement, cleaning, calibration.

DO extract the underlying mechanism: how the machine's core function is physically carried out, stage by stage, as material/air/fluid/energy/signal moves through the system and components interact to produce the intended output. The specific stages, components, and terminology MUST come entirely from the manual below -- do not import terminology, component names, or mechanisms from any other type of equipment. If this machine is not a compressor, do not mention compression, airends, or oil separation; if it's not a pump, do not mention impellers; describe THIS machine using only what the manual actually says about it.

IMPORTANT: manuals rarely have a section explicitly titled "theory of operation" or "working principle." This kind of engineering explanation is usually scattered as incidental description INSIDE maintenance, general data, or specification sections -- not confined to a section literally called "Operation." Read the whole manual for sentences that explain WHY or HOW something physically happens, even if that sentence sits inside a section about draining or replacing a part. Extract the engineering logic from wherever it appears in THIS manual; do not limit yourself to a single section, and do not substitute reasoning from a different type of machine when this manual's explanation is thin -- if the manual genuinely doesn't explain a stage, leave it out rather than inventing it.

For each functional stage, extract:
- operation: sequential step number as "Operation_010", "Operation_020", "Operation_030" etc (increment by 10), in the order the function is physically carried out
- task_list_description: the functional stage following the pattern "Function - Mechanism", using the manual's own terms for the function and the component that carries it out
- frequency: leave blank (not applicable)
- hrs: leave blank (not applicable)
- work_needed: 0 (this describes function, not maintenance work)
- system_condition: 1 (this describes the machine's normal running function)
- material_number: leave blank
- component: the specific component or subsystem named in the manual that carries out this function
- instruction: a clear, detailed engineering explanation of what physically happens at this stage and why, using only terms and mechanisms the manual actually describes. Number each sub-point starting at 1. Put EACH numbered point on its own line, with a blank line between points -- use a literal "\\n\\n" (newline, newline) between point N and point N+1, never a space.
- failure_modes: leave blank (not applicable)

Return ONLY a valid JSON array. No markdown, no explanation, no extra text.
If the manual has no engineering description of how the machine functions anywhere, return an empty array: []

Example format (illustrates JSON shape and reasoning style only -- "Stage" and "Component" below are placeholders; replace with whatever this manual's actual machine and components are):
[
  {{
    "operation": "Operation_010",
    "task_list_description": "[First functional stage from the manual] - [Component that performs it]",
    "frequency": "",
    "hrs": "",
    "work_needed": 0,
    "system_condition": 1,
    "material_number": "",
    "component": "[Component name from the manual]",
    "instruction": "1. [What physically enters or triggers this stage, per the manual].\\n\\n2. [What the component does to it, and how, per the manual].\\n\\n3. [What results, and what happens next in the sequence, per the manual].",
    "failure_modes": ""
  }}
]

EQUIPMENT MANUAL:
{manual_text}"""


PM_TYPE_GUIDANCE = {
    "PM1": "Inspection means a routine visual or physical CHECK with no scheduled part replacement -- e.g. checking for leaks, checking fastener tightness, checking fluid level, visually inspecting hoses or tires. If the task's end action is replacing a part, it does NOT belong here -- that's PM4.",
    "PM2": "Lubrication means applying, topping up, or changing a lubricant, grease, or coolant itself (e.g. greasing a fitting, topping up engine oil, changing compressor oil). Do not include filter/element replacement here unless the task is specifically about the fluid, not the filter -- filter/element swaps belong in PM4.",
    "PM3": "Calibration means adjusting a device or setting to a specified reference value. This includes pressure regulator adjustment, governor or engine speed adjustment, sensor calibration, torque specifications, and any 'adjust to X value' instruction in the manual -- even if the manual's section heading doesn't use the word 'calibration'. If the manual has any section on adjusting pressure, speed, torque, or regulator settings, it belongs here.",
    "PM4": "Replacements means the routine, scheduled swap of a wearable part or consumable on a fixed interval (filters, elements, belts, fluids, fasteners). This is the ONLY category that should contain recurring element/filter/fluid replacement tasks -- do not also list the same task under PM2, PM5, or PM6.",
    "PM5": "Overhaul means major teardown, rebuild, or reconditioning of an assembly (e.g. rebuilding the airend internals, engine rebuild, replacing internal bearings/rotors/gears as part of a rebuild). Do NOT include routine scheduled part replacement here (that's PM4) or routine fluid changes (that's PM2). If the manual explicitly states major overhauls are outside its scope or should be referred to an authorized service department, return an EMPTY array for this category -- do not invent overhaul tasks by relabeling routine maintenance content.",
    "PM6": "Condition Monitoring means measuring or observing a parameter against a threshold using a gauge, sensor, or instrument, WITHOUT performing a replacement as part of the same task (e.g. monitoring discharge temperature, monitoring vibration, oil sampling/analysis, watching a diagnostic lamp). If a task's end action is replacing a part on a schedule, it belongs in PM4, not here.",
    "PM7": "Cleaning means removing dirt, grease, debris, or buildup from a component that stays installed (e.g. cleaning a cooler core exterior, wiping down a housing interior). Do not include tasks whose primary action is replacing a part.",
    "PM8": "Safety Inspection means checking safety-critical items specifically: guards, safety valves, safety decals/labels, emergency stops, and fasteners or panels tied to injury or noise-containment risk.",
    "PM9": "Software Back-up means backing up or restoring configuration, firmware, or software on a PLC, controller, or electronic control module. If the manual describes no software, controller, or firmware, return an EMPTY array -- do not invent a software task.",
}


def build_pm_prompt(pm_code: str, pm_name: str, manual_text: str) -> str:
    category_guidance = PM_TYPE_GUIDANCE.get(pm_code, "")
    return f"""You are a maintenance engineering expert. You have been given an equipment manual below.

Your task is to extract ALL maintenance tasks that fall under the category: {pm_code} - {pm_name}

CATEGORY DEFINITION FOR {pm_code} - {pm_name}: {category_guidance}

If a task genuinely fits more than one category, extract it under the SINGLE most specific category above and skip it in the others -- do not extract the same task into multiple PM types.

For each task you find, extract the following fields:
- operation: sequential step number as "Operation_010", "Operation_020", "Operation_030" etc (increment by 10)
- task_list_description: the step title following the component hierarchy pattern "Assembly - Subassembly - Component x[quantity]". Preserve quantities (x1, x2, x4 etc) as they indicate how many of that component exist.
- frequency: how often this task should be done (e.g. "2W" for 2 weekly, "1M" for monthly, "1Y" for yearly). Leave blank if not specified.
- hrs: estimated TECHNICIAN TIME to perform this specific task, as a decimal number of hours (e.g. 0.1, 0.5, 1.0). This is NOT the maintenance interval -- if the manual says "every 1000 hours," that 1000 belongs in frequency, never here. Leave blank if no time estimate is given.
- work_needed: 1 if active work is required, 0 if observation only. Default to 1.
- system_condition: 0 for machine stopped, 1 for machine running. Default to 0.
- material_number: SAP material number if mentioned, otherwise leave blank.
- component: the specific component name only (without the assembly hierarchy), e.g. "Bearings x8"
- instruction: step-by-step work instruction a technician can follow. Number each step starting at 1. Put EACH numbered step on its own line, with a blank line between steps -- use a literal "\\n\\n" (newline, newline) between step N and step N+1, never a space. Be specific.
- failure_modes: comma-separated list of failure modes this task prevents. Leave blank if not specified.

Return ONLY a valid JSON array. No markdown, no explanation, no extra text.
If no tasks of type {pm_name} are found in the manual, return an empty array: []

Example format:
[
  {{
    "operation": "Operation_010",
    "task_list_description": "Main Drive assembly - Bearings x4",
    "frequency": "2W",
    "hrs": 0.1,
    "work_needed": 1,
    "system_condition": 0,
    "material_number": "",
    "component": "Bearings x4",
    "instruction": "1. Isolate and lock out the drive.\\n\\n2. Remove guard.\\n\\n3. Apply 2 shots of grease per nipple using grease gun.",
    "failure_modes": "Bearing seizure, overheating"
  }}
]

EQUIPMENT MANUAL:
{manual_text}"""


# ── Stock component image lookup ─────────────────────────────────────────────
#
# No internal curated image library exists yet, so this falls back to
# Openverse (https://openverse.org) -- an aggregator of Creative Commons
# and public domain images (Wikimedia Commons, Flickr Commons, museum
# collections, etc.), rather than raw Google/Bing image search results,
# which are not cleared for reuse. This gives a generic stock reference
# for a component TYPE (e.g. "a bearing"), not a photo of this specific
# unit's actual part -- the reviewer is expected to swap it if it's the
# wrong image, same as the existing "reviewer fills gaps" workflow.
#
# Image lookup happens INLINE inside call_claude_for_pm_type(), right
# after each PM/WP type's steps come back from Claude -- so it runs
# concurrently with the other nine calls rather than as a separate pass
# afterward. WP is included: its "component" field names real physical
# parts (e.g. "Airend", "Pressure Regulator"), same as PM1-PM9's.
#
# Because up to 10 calls can be requesting images at the same moment,
# two things guard against redundant/excessive network calls:
# - image_ctx (cache + per-keyword locks) is created ONCE in generate()
#   and threaded into every call, so if two types both need "bearings"
#   at the same time, only one of them hits the network -- the second
#   waits on the same lock and reuses the result.
# - OPENVERSE_MAX_CONCURRENCY caps how many Openverse requests are in
#   flight at once across the whole run, since its public unauthenticated
#   API has a modest rate limit.
#
# Known limitations, by design for a v1:
# - Match quality depends entirely on how common/generic the component
#   name is. "Bearing", "V-belt", "oil filter" will hit; a very specific
#   OEM part name likely won't -- that's expected to come back blank.
# - Some Openverse results carry a CC BY / CC BY-SA license, which
#   technically requires attribution if this ever leaves your org (sent
#   to a customer, embedded in a deliverable). This implementation does
#   not track/store attribution -- flagging this now rather than silently
#   shipping it, since it's a legal detail worth a deliberate decision
#   once this graduates past internal use.
# - If you run this at higher volume than one equipment at a time,
#   register for an Openverse API key/token to raise the rate limit
#   rather than relying on the concurrency cap alone.

OPENVERSE_API_URL = "https://api.openverse.org/v1/images/"
OPENVERSE_MAX_CONCURRENCY = 4
OPENVERSE_USER_AGENT = "SquareMethods-PM-Strategy/1.0 (https://squaremethods.com; contact: support@squaremethods.com)"
_QUANTITY_SUFFIX_RE = re.compile(r"\s*[xX]\d+\s*$")


def normalize_component_for_image_search(component: str) -> Optional[str]:
    """
    Turn a Component field value into a bare search keyword.
    "Main Drive assembly - Bearings x4"  -> "bearings"
    "Compressor Oil Filter Element"      -> "compressor oil filter element"
    "Bearings x8"                       -> "bearings"
    Returns None for empty/unusable input so callers can skip the lookup.
    """
    if not component:
        return None

    # Strip assembly/subassembly hierarchy -- keep only the last segment
    # after " - ", since that's the actual component, not its location.
    term = component.split(" - ")[-1]

    # Strip trailing quantity markers like "x4", "X12"
    term = _QUANTITY_SUFFIX_RE.sub("", term)

    term = term.strip()
    return term.lower() if term else None


async def fetch_stock_image_url(
    client: httpx.AsyncClient,
    keyword: str,
    semaphore: asyncio.Semaphore,
) -> Optional[str]:
    """
    Query Openverse for a single reusable stock image URL matching the
    keyword. Returns None on any failure or empty result -- a missing
    image is not an error, it just leaves the Excel cell blank for the
    reviewer to fill, same as today. The semaphore caps how many of
    these are in flight at once across the whole generate() run.
    """
    async with semaphore:
        try:
            response = await client.get(
                OPENVERSE_API_URL,
                params={
                    "q": keyword,
                    "license_type": "commercial,modification",  # excludes NC/ND-only licenses
                    "page_size": 1,
                },
                headers={"User-Agent": OPENVERSE_USER_AGENT},
                timeout=8.0,
            )
            response.raise_for_status()
            data = response.json()
            results = data.get("results") or []
            if not results:
                return None
            return results[0].get("url") or results[0].get("thumbnail")
        except Exception as e:
            log.warning(f"Stock image lookup failed for '{keyword}': {type(e).__name__}: {e}")
            return None


async def get_or_fetch_stock_image_url(
    client: httpx.AsyncClient,
    keyword: str,
    image_ctx: dict,
) -> Optional[str]:
    """
    Cache-aware wrapper around fetch_stock_image_url(), shared across
    all ten concurrent PM/WP-type calls in a single generate() run. If
    two types both need an image for "bearings" at the same moment, the
    per-keyword lock (created on first use, guarded by cache_lock)
    ensures only one of them actually hits the network -- the second
    waits and reuses the result instead of firing a duplicate request.

    image_ctx is built once in generate() and threaded through every
    call: {"cache": dict, "locks": dict, "cache_lock": asyncio.Lock,
    "semaphore": asyncio.Semaphore}.
    """
    cache = image_ctx["cache"]
    locks = image_ctx["locks"]
    cache_lock = image_ctx["cache_lock"]
    semaphore = image_ctx["semaphore"]

    if keyword in cache:
        return cache[keyword]

    async with cache_lock:
        lock = locks.setdefault(keyword, asyncio.Lock())

    async with lock:
        if keyword in cache:  # someone else populated it while we waited
            return cache[keyword]
        url = await fetch_stock_image_url(client, keyword, semaphore)
        cache[keyword] = url
        return url


async def attach_images_to_steps(client: httpx.AsyncClient, steps: list, image_ctx: dict) -> None:
    """
    Mutates each step dict in place with an "image_url" key. All steps
    in this one PM/WP type's result are looked up concurrently with
    each other, and -- via the shared image_ctx -- safely concurrently
    with every other PM/WP type's lookups running at the same time.
    """
    async def resolve(step: dict) -> None:
        keyword = normalize_component_for_image_search(step.get("component", ""))
        if not keyword:
            step["image_url"] = ""
            return
        step["image_url"] = await get_or_fetch_stock_image_url(client, keyword, image_ctx) or ""

    if steps:
        await asyncio.gather(*(resolve(step) for step in steps))


async def call_claude_for_pm_type(
    client: httpx.AsyncClient,
    bedrock,
    pm_code: str,
    pm_name: str,
    manual_text: str,
    image_ctx: dict,
) -> tuple[str, str, list]:
    log.debug(f"{pm_code} ENTERED call_claude_for_pm_type, prompt building now")
    if pm_code == "WP":
        prompt = build_working_principle_prompt(manual_text)
    else:
        prompt = build_pm_prompt(pm_code, pm_name, manual_text)
    log.debug(f"{pm_code} PROMPT BUILT, length={len(prompt)}")

    body = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens":        MAX_TOKENS,
        "messages": [
            {"role": "user", "content": prompt}
        ],
    })

    log.info(f"{pm_code} PROMPT LENGTH (chars): {len(prompt)}")

    try:
        loop = asyncio.get_event_loop()

        try:
            response = await loop.run_in_executor(
                None,
                lambda: bedrock.invoke_model(
                    modelId     = BEDROCK_MODEL,
                    body        = body,
                    contentType = "application/json",
                    accept      = "application/json",
                )
            )
        except Exception as invoke_err:
            log.error(f"{pm_code} INVOKE_MODEL FAILED: {type(invoke_err).__name__}: {invoke_err}")
            raise

        try:
            raw_body = response["body"].read()
            log.info(f"{pm_code} RAW BODY LENGTH: {len(raw_body)} bytes")
            log.info(f"{pm_code} RAW BODY PREVIEW: {raw_body[:500]}")
        except Exception as read_err:
            log.error(f"{pm_code} BODY READ FAILED: {type(read_err).__name__}: {read_err}")
            raise

        raw = json.loads(raw_body)
        raw_text = raw["content"][0]["text"].strip()

        # Claude sometimes prepends explanatory text before the JSON array
        # (e.g. "Based on the equipment manual provided, here are the
        # maintenance tasks..."), despite being instructed to return only
        # JSON. Extract the array substring directly instead of assuming
        # the entire response is valid JSON.
        start_bracket = raw_text.find("[")
        end_bracket   = raw_text.rfind("]")

        if start_bracket != -1 and end_bracket != -1 and end_bracket > start_bracket:
            json_str = raw_text[start_bracket:end_bracket + 1]
            try:
                steps = json.loads(json_str)
            except json.JSONDecodeError as parse_err:
                log.error(f"{pm_code} JSON parse failed after extraction: {parse_err}. Extracted: {json_str[:300]}")
                steps = []
        else:
            log.warning(f"{pm_code} no JSON array found in response, treating as empty. Raw text: {raw_text[:300]}")
            steps = []

        if not isinstance(steps, list):
            steps = []

        log.info(f"{pm_code} ({pm_name}): {len(steps)} steps extracted")

        # Resolve stock images for this type's steps now, concurrently with
        # the other steps in this same list. Runs for WP too -- its
        # components (e.g. "Airend", "Pressure Regulator") are genuine
        # physical parts, same as PM1-PM9's.
        await attach_images_to_steps(client, steps, image_ctx)
        matched = sum(1 for s in steps if s.get("image_url"))
        if steps:
            log.info(f"{pm_code} images: {matched} / {len(steps)} steps matched a stock image")

        return pm_code, pm_name, steps

    except Exception as e:
        log.error(f"Claude call failed for {pm_code} ({pm_name}): {type(e).__name__}: {e}")
        return pm_code, pm_name, []


# ── Excel builder ─────────────────────────────────────────────────────────────

def _estimate_row_height(instruction: str) -> int:
    """
    Instruction text contains numbered steps separated by blank lines
    (\\n\\n). Estimate wrapped line count so multi-step instructions
    don't get visually clipped at a fixed row height.
    """
    if not instruction:
        return MIN_ROW_HEIGHT

    line_count = 0
    for segment in instruction.split("\n"):
        if segment == "":
            line_count += 1  # blank line between steps
        else:
            # account for wrapping within a single step line
            line_count += max(1, -(-len(segment) // INSTRUCTION_COL_CHARS_PER_LINE))

    return max(MIN_ROW_HEIGHT, line_count * LINE_HEIGHT)


def build_excel(equipment_id: str, pm_results: list[tuple]) -> bytes:
    """
    Build the PM strategy Excel file from Claude's structured output.
    Format matches PM_strategy.xlsx exactly.

    pm_results: list of (pm_code, pm_name, steps_list) tuples in PM1-PM9 order.
    All PM types are written -- empty ones show the header and column row
    with no data rows, ready for the reviewer to fill in manually.
    """
    wb   = Workbook()
    ws   = wb.active
    ws.title = "PM Strategy"

    col_widths = [15, 55, 12, 8, 13, 17, 16, 35, 60, 35, 40]
    for i, w in enumerate(col_widths, 1):
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = w

    current_row = 1

    for pm_code, pm_name, steps in pm_results:

        # PM type header row (e.g. "PM2 - Lubrication")
        header_cell = ws.cell(row=current_row, column=1, value=f"{pm_code} - {pm_name}")
        header_cell.font = Font(bold=True, color="FFFFFF", name="Arial", size=11)
        header_cell.fill = HEADER_FILL
        header_cell.alignment = Alignment(vertical="center")
        ws.row_dimensions[current_row].height = 20
        ws.merge_cells(
            start_row=current_row, start_column=1,
            end_row=current_row, end_column=len(COLUMNS)
        )
        current_row += 1

        # Column headers
        for col_idx, col_name in enumerate(COLUMNS, 1):
            cell           = ws.cell(row=current_row, column=col_idx, value=col_name)
            cell.font      = SUBHEAD_FONT
            cell.fill      = SUBHEAD_FILL
            cell.alignment = Alignment(wrap_text=True, vertical="center", horizontal="center")
        ws.row_dimensions[current_row].height = 30
        current_row += 1

        # Data rows -- skipped naturally if steps is empty
        for step in steps:
            instruction = step.get("instruction", "")
            image_url = step.get("image_url", "")
            row_values = [
                step.get("operation", ""),
                step.get("task_list_description", ""),
                step.get("frequency", ""),
                step.get("hrs", ""),
                step.get("work_needed", ""),
                step.get("system_condition", ""),
                step.get("material_number", ""),
                step.get("component", ""),
                instruction,
                step.get("failure_modes", ""),
                image_url,  # stock reference URL if matched, blank otherwise -- reviewer edits/replaces as needed
            ]
            for col_idx, value in enumerate(row_values, 1):
                cell           = ws.cell(row=current_row, column=col_idx, value=value)
                cell.font      = DATA_FONT
                cell.alignment = WRAP_ALIGN
                if col_idx == len(COLUMNS) and image_url:
                    cell.hyperlink = image_url
                    cell.font = Font(name="Arial", size=10, color="0563C1", underline="single")
            ws.row_dimensions[current_row].height = _estimate_row_height(instruction)
            current_row += 1

        # Spacer row between PM type blocks
        current_row += 1

    # Freeze the top row of the first block
    ws.freeze_panes = "A3"

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.read()


def fetch_equipment_info(equipment_id: str, company_id: str) -> dict:
    """
    Pull the equipment's display name and reference code for use in the
    output filename. Matches the `equipment` table schema: `name` is the
    human-readable label, `reference_code` is the unique per-company
    identifier -- both go in the filename so two units with the same
    name (different reference_code) don't produce identical filenames.
    Respects the soft-delete pattern (deleted_at IS NULL) used elsewhere
    in the equipment import system.
    """
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT name, reference_code
                FROM equipment
                WHERE id = %s::uuid
                AND company_id = %s::uuid
                AND deleted_at IS NULL
            """, (equipment_id, company_id))
            row = cur.fetchone()
    finally:
        conn.close()

    if not row:
        log.warning(f"No equipment record found for {equipment_id}, falling back to equipment_id in filename")
        return {"name": "", "reference_code": ""}

    return {"name": row.get("name") or "", "reference_code": row.get("reference_code") or ""}


def build_output_filename(equipment_id: str, equipment_info: dict) -> str:
    """
    Build a human-readable filename like:
      PM_Strategy_P185WJD_Compressor_1_A102_2026-07-26.xlsx
    (name + reference_code). Falls back to the equipment_id if no
    record is found, so the file is still uniquely identifiable rather
    than failing.
    """
    from datetime import date

    label = " ".join(part for part in [equipment_info.get("name"), equipment_info.get("reference_code")] if part).strip()
    if not label:
        label = equipment_id

    # slugify: keep letters/numbers, collapse everything else to a single underscore
    slug = re.sub(r"[^A-Za-z0-9]+", "_", label).strip("_")
    slug = slug[:80]  # keep filenames reasonable on Windows/shared drives

    return f"PM_Strategy_{slug}_{date.today().isoformat()}.xlsx"


# ── Main entry point ──────────────────────────────────────────────────────────

async def generate(equipment_id: str, company_id: str) -> bytes:
    """
    Full pipeline called from the FastAPI endpoint.
    1. Fetch all manual chunks for the equipment
    2. Run ten parallel Claude calls (Working Principle + nine PM types),
       each resolving its own stock component images inline
    3. Build and return the Excel file as bytes
       -- always returns a valid file even if all PM types are empty

    Return type is unchanged (bytes only) to avoid breaking existing
    callers. To get a human-readable filename, call
    fetch_equipment_info() + build_output_filename() separately in your
    endpoint -- see the usage note below.

    Example endpoint usage:
        excel_bytes = await generate(equipment_id, company_id)
        equipment_info = fetch_equipment_info(equipment_id, company_id)
        filename = build_output_filename(equipment_id, equipment_info)
        return Response(
            content=excel_bytes,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    """
    manual_text = fetch_all_manual_chunks(equipment_id, company_id)

    bedrock = boto3.client("bedrock-runtime", region_name=BEDROCK_REGION)

    async with httpx.AsyncClient() as client:
        # Built once and shared across all ten concurrent PM/WP calls, so
        # image lookups happening inside each call at the same time can
        # dedupe against each other rather than each firing independently.
        image_ctx = {
            "cache": {},
            "locks": {},
            "cache_lock": asyncio.Lock(),
            "semaphore": asyncio.Semaphore(OPENVERSE_MAX_CONCURRENCY),
        }

        tasks = [
            call_claude_for_pm_type(client, bedrock, pm_code, pm_name, manual_text, image_ctx)
            for pm_code, pm_name in PM_TYPES
        ]
        results = await asyncio.gather(*tasks)

    pm_results = list(results)

    populated = sum(1 for _, _, steps in pm_results if steps)
    log.info(f"Generation complete. {populated} / {len(PM_TYPES)} PM types have tasks.")

    return build_excel(equipment_id, pm_results)