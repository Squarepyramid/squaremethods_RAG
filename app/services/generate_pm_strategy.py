"""
SquareMethods - PM Strategy Generation Service
==============================================
Place this file at: app/services/generate_pm_strategy.py

Pulls all manual chunks for an equipment node, then runs nine
parallel async Claude calls (one per PM type) to extract and
structure maintenance tasks into a downloadable Excel file.

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
    Working Principle is a step-by-step narrative of how the equipment
    actually operates (startup sequence, process flow, control logic,
    shutdown), not a maintenance task list. Kept on the same row schema
    as the PM types so it drops into the same Excel sheet, but the
    fields that don't apply to an operating description (frequency,
    hrs, work_needed, failure_modes) are left blank/defaulted rather
    than asking the model to invent maintenance-style values for them.

    Two contrasting examples are given (process equipment and mobile/
    towed equipment) because a single-domain example causes the model
    to pattern-match too tightly to that domain and return an empty
    array on manuals that don't look like it, instead of generalizing.
    """
    return f"""You are a maintenance engineering expert. You have been given an equipment manual below.

Your task is to extract the WORKING PRINCIPLE -- a step-by-step explanation of how this equipment actually operates and is operated, in the order it happens. This is NOT a maintenance task list -- do not include lubrication, inspection, or repair tasks here, only how the equipment functions and is operated. Depending on the type of equipment, this may include any of:
- Startup sequence, warm-up, running state, and shutdown sequence (e.g. "Before Starting", "Starting", "Stopping" sections)
- Setup/positioning/towing/connection sequence for mobile or towed equipment (e.g. "Before Towing", "Setting Up", "Towing", "Disconnect" sections)
- Process or material flow through the system (e.g. how air is compressed, cooled, and separated from oil)
- Control panel, gauge, or switch functions -- what each control does and what it means when engaged (e.g. a control panel/instrument section listing gauges, switches, and lamps)
- Control logic or safety interlocks that govern normal operation

Pull from whichever of these sections actually exist in the manual -- do not assume it must look like a continuous process-flow narrative. A sequence of numbered operating steps (start engine, warm up, open valve, run, close valve, shut down) is just as valid a Working Principle as an internal process description.

For each step, extract:
- operation: sequential step number as "Operation_010", "Operation_020", "Operation_030" etc (increment by 10)
- task_list_description: the stage name following the pattern "Sequence/Subsystem - Stage", e.g. "Startup Sequence - Fill hopper" or "Control Panel - Engine Oil Pressure Gauge"
- frequency: leave blank (not applicable to a working principle)
- hrs: leave blank (not applicable)
- work_needed: 0 (this describes operation, not maintenance work)
- system_condition: 1 if this step occurs while the machine is running, 0 if it occurs while stopped (e.g. startup pre-checks, towing setup)
- material_number: leave blank
- component: the specific component, subsystem, or control involved in this step, e.g. "Main hopper" or "Engine Oil Pressure Gauge"
- instruction: a clear, detailed explanation of what happens or what the operator does at this stage. Number each sub-point starting at 1. Put EACH numbered point on its own line, with a blank line between points -- use a literal "\\n\\n" (newline, newline) between point N and point N+1, never a space. Be specific about how the equipment behaves, not just what a technician does.
- failure_modes: leave blank (not applicable)

Return ONLY a valid JSON array. No markdown, no explanation, no extra text.
If the manual doesn't describe how the equipment operates anywhere, return an empty array: []

Example format (process equipment):
[
  {{
    "operation": "Operation_010",
    "task_list_description": "Startup Sequence - Fill hopper",
    "frequency": "",
    "hrs": "",
    "work_needed": 0,
    "system_condition": 0,
    "material_number": "",
    "component": "Main hopper",
    "instruction": "1. Operator loads raw material into the main hopper via the top-mounted chute.\\n\\n2. A level sensor in the hopper confirms sufficient material before the feed screw is permitted to start.\\n\\n3. Once confirmed, the control system releases the interlock and allows the startup sequence to proceed.",
    "failure_modes": ""
  }}
]

Example format (mobile/towed equipment):
[
  {{
    "operation": "Operation_010",
    "task_list_description": "Starting Sequence - Power switch",
    "frequency": "",
    "hrs": "",
    "work_needed": 0,
    "system_condition": 0,
    "material_number": "",
    "component": "Power Switch",
    "instruction": "1. Turn the power switch to \\"ON\\" to activate the system prior to starting.\\n\\n2. Turn the power switch to \\"START\\" to crank the engine, holding it in that position for approximately 5 seconds after the engine starts.\\n\\n3. Release the switch, which automatically moves to the \\"ON\\" position once the engine starts and sustains running.",
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




async def call_claude_for_pm_type(
    client: httpx.AsyncClient,
    bedrock,
    pm_code: str,
    pm_name: str,
    manual_text: str,
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
                "",  # Image -- left blank for reviewer to fill
            ]
            for col_idx, value in enumerate(row_values, 1):
                cell           = ws.cell(row=current_row, column=col_idx, value=value)
                cell.font      = DATA_FONT
                cell.alignment = WRAP_ALIGN
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


# ── Main entry point ──────────────────────────────────────────────────────────

async def generate(equipment_id: str, company_id: str) -> bytes:
    """
    Full pipeline called from the FastAPI endpoint.
    1. Fetch all manual chunks for the equipment
    2. Run nine parallel Claude calls (one per PM type)
    3. Build and return the Excel file as bytes
       -- always returns a valid file even if all PM types are empty
    """
    manual_text = fetch_all_manual_chunks(equipment_id, company_id)

    bedrock = boto3.client("bedrock-runtime", region_name=BEDROCK_REGION)

    async with httpx.AsyncClient() as client:
        tasks = [
            call_claude_for_pm_type(client, bedrock, pm_code, pm_name, manual_text)
            for pm_code, pm_name in PM_TYPES
        ]
        results = await asyncio.gather(*tasks)

    pm_results = list(results)

    populated = sum(1 for _, _, steps in pm_results if steps)
    log.info(f"Generation complete. {populated} / {len(PM_TYPES)} PM types have tasks.")

    return build_excel(equipment_id, pm_results)