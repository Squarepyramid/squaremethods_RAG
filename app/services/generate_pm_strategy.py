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

PM_TYPES = [
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

def build_pm_prompt(pm_code: str, pm_name: str, manual_text: str) -> str:
    return f"""You are a maintenance engineering expert. You have been given an equipment manual below.

Your task is to extract ALL maintenance tasks that fall under the category: {pm_code} - {pm_name}

For each task you find, extract the following fields:
- operation: sequential step number as "Operation_010", "Operation_020", "Operation_030" etc (increment by 10)
- task_list_description: the step title following the component hierarchy pattern "Assembly - Subassembly - Component x[quantity]". Preserve quantities (x1, x2, x4 etc) as they indicate how many of that component exist.
- frequency: how often this task should be done (e.g. "2W" for 2 weekly, "1M" for monthly, "1Y" for yearly). Leave blank if not specified.
- hrs: estimated hours as a decimal number (e.g. 0.1, 0.5, 1.0). Leave blank if not specified.
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