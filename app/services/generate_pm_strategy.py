"""
SquareMethods - PM Strategy Generation Service
==============================================
Place this file at: app/services/generate_pm_strategy.py

Pulls the manual chunks most relevant to each PM type for an equipment
node, runs structured (forced tool-use) Claude calls to extract and
structure maintenance tasks, and returns a downloadable Excel file whose
FIRST sheet ("PM Strategy") matches the PM_strategy.xlsx import format
exactly. Use generate_with_filename() for a real, human-readable filename.

PM Types:
  WP  - Working Principle      PM5 - Overhaul
  PM1 - Inspection             PM6 - Condition Monitoring
  PM2 - Lubrication            PM7 - Cleaning
  PM3 - Calibration            PM8 - Safety Inspection
  PM4 - Replacements           PM9 - Software Back-up

CHANGE LOG (2026-09-21 -- "robust PM" revision)
-----------------------------------------------
Goal: produce the same kind of output a reliability engineer builds by
hand from an OEM manual (parts list, critical spares, setpoints,
acceptance criteria, owner, OEM vs recommended tasks, gaps/conflicts),
instead of only a verbatim restatement of the manual's maintenance table.
Everything is ADDITIVE: generate() / generate_with_filename() keep their
signatures and return types, the "PM Strategy" sheet keeps its exact
column layout and stays the first sheet, and every new stage can be
switched off by env var.

  1. SCHEDULE ANCHOR CHUNKS. A manual's maintenance schedule/lube chart
     usually lives in one or two chunks that may not rank in the top-K for
     every category's similarity query. Those chunks are now retrieved
     once (SCHEDULE_QUERY_TEXT) and appended to every PM category's
     context, deduplicated.

  2. SETPOINTS PASS (new). One up-front call extracts every operating
     setting the manual prints (pressures, temperatures, gaps, torques,
     speeds, fluid grades/quantities). The result feeds every PM prompt as
     a REFERENCE SETPOINTS block (so tasks can cite real limits) and is
     written to a "Setpoints" sheet.

  3. RICHER TASK FIELDS. EXTRACTION_TOOL gains OPTIONAL fields:
     acceptance_criteria, source ("OEM"/"Recommended"), source_ref, owner.
     They are optional in the schema, so old-shaped responses still parse.
     In the PM Strategy sheet they are appended to the Long Text
     (Instruction) cell as "Acceptance:", "Owner:" and "Source:" lines --
     no new columns, so the import format is untouched. Frequency is now
     requested in normalized codes (Shift, 1D, 1W, 2W, 1M, 3M, 6M, 1Y,
     3000H) so annual labor can be computed.

  4. PARTS LIST PASS (new). Parts lists are spread over many pages and
     are mostly tables, so similarity top-K retrieval misses most of them.
     This pass fetches ALL chunks for the equipment, keeps the ones that
     look like parts tables (_parts_chunk_score), and extracts rows in
     small batches. A batch that hits max_tokens is split in half and
     retried (up to PARTS_MAX_SPLIT_DEPTH) instead of silently truncating
     -- a single call cannot return 1,000+ rows.

  5. CRITICAL SPARES (deterministic, no LLM). Parts are classified into
     Tier A/B/C with a keyword rule set (classify_part) plus the manual's
     own wear/spare flags when printed. Deterministic on purpose: the same
     parts list always yields the same spares list.

  6. CROSS-CATEGORY DEDUPE + RENUMBER. Exact duplicate tasks (same
     normalized description + component) that leaked into two categories
     are collapsed into the most specific category (DEDUPE_PRIORITY), and
     operations are renumbered Operation_010, _020... per category.

  7. RECOMMENDED TASKS PASS (new). Tier A/B parts with no covering task,
     safety devices with no functional test, and setpoints with no check
     are sent to one call that proposes gap-filling tasks. Every such task
     is labelled "Source: Recommended (not in OEM manual) -- validate
     before use", and the prompt forbids numeric limits that are not in
     the manual or setpoints ("record baseline at first PM" instead). This
     matches the grounding rule used in /chat: nothing presented as manual
     content that isn't in the manual.

  8. REVIEW NOTES PASS (new). One call looks for conflicts (two intervals
     or values for the same item), referenced-but-missing supplier
     documents, and items needing clarification; deterministic notes are
     added for errored categories, parts without part numbers, capped
     batches, and removed duplicates. Written to a "Review Notes" sheet.

  9. PHASED DEADLINE. The shared 12-minute deadline is split: setpoints
     first, then PM categories and parts batches together, with
     FINAL_PHASE_RESERVE_SECONDS held back so recommended/review passes
     always get time. Each stage degrades to "error"/"partial" on its own;
     the job still returns a real file.

  10. TRUNCATION HANDLING. A response with stop_reason "max_tokens" is now
      treated as a failed attempt. PM categories retry with a
      conciseness instruction; parts batches split instead.

Extra sheets written after "PM Strategy": PM Summary, Setpoints,
Critical Spares, Parts List, Review Notes. If the import endpoint ever
rejects multi-sheet workbooks, set PM_EXTRA_SHEETS_ENABLED=false.

Prior revisions (2026-08-28 reliability pass: forced tool use,
temperature=0, per-category status, retries, shared deadline, per-invocation
semaphore, tenant-scoped logging; earlier: pgvector retrieval rework,
image matching behind IMAGE_MATCHING_ENABLED, generate_with_filename) are
unchanged in behavior. Their full changelog text is in git history.
"""

import io
import logging
import asyncio
import re
import time
from collections import defaultdict
from typing import Callable, Optional

import boto3
import os
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment

from app.utils.db import get_db_connection
from app.services.embeddings import get_embedding
from app.services.bedrock_client import call_claude

log = logging.getLogger(__name__)


def _env_flag(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


WORKING_PRINCIPLE = ("WP", "Working Principle")

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
PM_NAME_BY_CODE = dict(PM_TYPES)

# Embedding-only queries (never read by a model) -- keyword density matters
# more than grammar. Schedule/interval words were added to the maintenance
# categories so the manual's schedule tables rank higher.
PM_QUERY_TEXT = {
    "WP":  "working principle theory of operation how the machine physically functions mechanism stages sequence of operation description",
    "PM1": "inspection check visual condition leaks tightness fluid level wear daily weekly monthly schedule",
    "PM2": "lubrication greasing oil change lubricant grease coolant top up nipple lube points chart grease type oil type quantity interval",
    "PM3": "calibration adjustment setting pressure regulator speed torque gap clearance specification setup adjust to",
    "PM4": "replacement scheduled change part filter element belt blade knife seal fluid consumable interval wear parts",
    "PM5": "overhaul rebuild teardown reconditioning major internal assembly bearings rotors gears annual dismantle",
    "PM6": "condition monitoring measurement gauge sensor instrument threshold vibration temperature noise current trend",
    "PM7": "cleaning removing dirt grease glue debris buildup wash wipe scrape sanitation washdown",
    "PM8": "safety inspection guard interlock emergency stop safety valve decal label lockout door switch",
    "PM9": "software backup firmware configuration PLC controller HMI recipe parameters restore memory card",
}
SCHEDULE_QUERY_TEXT = (
    "maintenance schedule table preventive maintenance chart interval daily weekly "
    "monthly quarterly yearly hours every shift routine maintenance lube chart"
)
SETPOINTS_QUERY_TEXT = (
    "setting specification pressure psi bar temperature degrees gap clearance torque "
    "speed rpm tension oil type grease type quantity liters regulator setpoint adjust to"
)
RECOMMENDED_QUERY_TEXT = (
    "safety device emergency stop interlock guard sensor photo eye proximity switch "
    "wear parts adjustment troubleshooting failure malfunction"
)
REVIEW_QUERY_TEXT = (
    "refer to manufacturer manual supplier documentation see separate manual consult "
    "electrical drawings not included contact service department vendor"
)

BEDROCK_REGION = os.environ.get("AWS_REGION", "ca-central-1")
MAX_TOKENS = 8192

BEDROCK_CALL_CONCURRENCY = int(os.environ.get("PM_BEDROCK_CALL_CONCURRENCY", 3))
GENERATION_DEADLINE_SECONDS = int(os.environ.get("PM_GENERATION_DEADLINE_SECONDS", 720))
PM_CATEGORY_MAX_ATTEMPTS = int(os.environ.get("PM_CATEGORY_MAX_ATTEMPTS", 3))
PM_CATEGORY_RETRY_BACKOFF_SECONDS = 15
RETRIEVAL_TOP_K = 40

# ── New stage switches and budgets (2026-09-21) ────────────────────────────
EXTRA_SHEETS_ENABLED = _env_flag("PM_EXTRA_SHEETS_ENABLED", True)
SETPOINTS_ENABLED = _env_flag("PM_SETPOINTS_ENABLED", True)
PARTS_EXTRACTION_ENABLED = _env_flag("PM_PARTS_EXTRACTION_ENABLED", True)
RECOMMENDED_TASKS_ENABLED = _env_flag("PM_RECOMMENDED_TASKS_ENABLED", True)
REVIEW_NOTES_ENABLED = _env_flag("PM_REVIEW_NOTES_ENABLED", True)

SCHEDULE_ANCHOR_TOP_K = 8
SETPOINTS_TOP_K = 30
PARTS_BATCH_CHUNKS = int(os.environ.get("PM_PARTS_BATCH_CHUNKS", 5))
PARTS_MAX_BATCHES = int(os.environ.get("PM_PARTS_MAX_BATCHES", 40))
PARTS_MAX_SPLIT_DEPTH = 3
PARTS_CANDIDATE_MIN_SCORE = 8
MAX_RECOMMENDED_TASKS = 25
MAX_CONTEXT_TASK_LINES = 150
MAX_CONTEXT_PART_LINES = 60

# Held back from the PM/parts phase so the recommended + review passes
# always get time before the shared deadline.
FINAL_PHASE_RESERVE_SECONDS = int(os.environ.get("PM_FINAL_PHASE_RESERVE_SECONDS", 150))

# Assumptions for converting frequencies to annual labor in "PM Summary".
OPERATING_DAYS_PER_YEAR = int(os.environ.get("PM_OPERATING_DAYS_PER_YEAR", 250))
SHIFTS_PER_DAY = int(os.environ.get("PM_SHIFTS_PER_DAY", 2))
OPERATING_HOURS_PER_YEAR = int(os.environ.get("PM_OPERATING_HOURS_PER_YEAR", 6000))

# When the same task leaks into two categories, keep it in the first one
# listed here (most specific first).
DEDUPE_PRIORITY = ["PM8", "PM3", "PM4", "PM2", "PM7", "PM6", "PM9", "PM1", "PM5"]

CONCISE_RETRY_SUFFIX = (
    "\n\nIMPORTANT: a previous attempt exceeded the output limit. Keep every "
    "instruction to at most 6 short numbered steps and keep other fields brief. "
    "Do not drop tasks -- shorten them."
)

# ── Structured output tool definitions ─────────────────────────────────────

_TASK_PROPERTIES = {
    "operation": {"type": "string", "description": "Sequential step number, e.g. 'Operation_010'."},
    "task_list_description": {"type": "string"},
    "frequency": {"type": "string", "description": "Normalized code: Shift, 1D, 1W, 2W, 1M, 2M, 3M, 6M, 1Y, <n>H, Event, or blank."},
    "hrs": {"type": ["number", "string"], "description": "Decimal technician hours for the task, or blank."},
    "work_needed": {"type": "integer"},
    "system_condition": {"type": "integer"},
    "material_number": {"type": "string"},
    "component": {"type": "string"},
    "instruction": {"type": "string", "description": "Numbered steps, each on its own line, separated by a blank line."},
    "failure_modes": {"type": "string"},
    # Optional (2026-09-21). Not in "required" so older-shaped output still parses.
    "acceptance_criteria": {"type": "string", "description": "Measurable pass/fail limit, or the no-limit phrase."},
    "source": {"type": "string", "enum": ["OEM", "Recommended"]},
    "source_ref": {"type": "string", "description": "Manual section/page/table the task comes from."},
    "owner": {"type": "string", "enum": ["Operator", "Technician", "Electrician", "Specialist"]},
}
_TASK_REQUIRED = [
    "operation", "task_list_description", "frequency", "hrs", "work_needed",
    "system_condition", "material_number", "component", "instruction", "failure_modes",
]

EXTRACTION_TOOL = {
    "name": "record_extracted_steps",
    "description": (
        "Record the list of steps/tasks extracted from the equipment manual for "
        "this category. Call this even if no steps were found -- pass an empty "
        "tasks list rather than omitting the call."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "description": "One entry per extracted step or task, in sequence order.",
                "items": {"type": "object", "properties": _TASK_PROPERTIES, "required": _TASK_REQUIRED},
            },
        },
        "required": ["tasks"],
    },
}

RECOMMENDED_TOOL = {
    "name": "record_recommended_tasks",
    "description": "Record gap-filling PM tasks. Call with an empty list if none are justified.",
    "input_schema": {
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        **_TASK_PROPERTIES,
                        "pm_code": {"type": "string", "enum": [c for c, _ in PM_TYPES if c != "WP"]},
                        "rationale": {"type": "string", "description": "Which gap this closes (part, safety device, or setpoint)."},
                    },
                    "required": _TASK_REQUIRED + ["pm_code", "rationale"],
                },
            },
        },
        "required": ["tasks"],
    },
}

SETPOINTS_TOOL = {
    "name": "record_setpoints",
    "description": "Record operating settings/specifications printed in the manual. Empty list if none.",
    "input_schema": {
        "type": "object",
        "properties": {
            "setpoints": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "parameter": {"type": "string"},
                        "value": {"type": "string", "description": "Value with units exactly as printed, e.g. '80 psi', '1/8\" to 1/4\"'."},
                        "component": {"type": "string"},
                        "source_ref": {"type": "string"},
                    },
                    "required": ["parameter", "value"],
                },
            },
        },
        "required": ["setpoints"],
    },
}

PARTS_TOOL = {
    "name": "record_parts_list_rows",
    "description": "Record every parts-list row found in the text. Empty list if the text has no parts list.",
    "input_schema": {
        "type": "object",
        "properties": {
            "parts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "assembly": {"type": "string", "description": "Assembly/parts-list name the row belongs to."},
                        "parts_list_ref": {"type": "string", "description": "Parts list or drawing number of the assembly, if printed."},
                        "item_no": {"type": "string"},
                        "part_number": {"type": "string", "description": "Exactly as printed; blank if none."},
                        "description": {"type": "string"},
                        "qty": {"type": "string"},
                        "wear_part_flag": {"type": "boolean", "description": "True only if the manual marks it as a wear part."},
                        "spare_part_flag": {"type": "boolean", "description": "True only if the manual marks it as a recommended spare."},
                    },
                    "required": ["assembly", "item_no", "part_number", "description", "qty"],
                },
            },
        },
        "required": ["parts"],
    },
}

REVIEW_TOOL = {
    "name": "record_review_notes",
    "description": "Record review notes for the reliability engineer. Empty list if none.",
    "input_schema": {
        "type": "object",
        "properties": {
            "notes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "note_type": {"type": "string", "enum": ["conflict", "missing_document", "missing_part_number", "gap", "clarification"]},
                        "detail": {"type": "string"},
                        "affected": {"type": "string"},
                        "source_ref": {"type": "string"},
                    },
                    "required": ["note_type", "detail"],
                },
            },
        },
        "required": ["notes"],
    },
}

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
NOTE_FONT     = Font(name="Arial", size=9, italic=True, color="595959")
WRAP_ALIGN    = Alignment(wrap_text=True, vertical="top")
INCOMPLETE_FILL = PatternFill("solid", start_color="F2F2F2", end_color="F2F2F2")
INCOMPLETE_FONT = Font(italic=True, color="595959", name="Arial", size=10)
TIER_FILLS = {
    "A": PatternFill("solid", start_color="F4CCCC", end_color="F4CCCC"),
    "B": PatternFill("solid", start_color="FFF2CC", end_color="FFF2CC"),
    "C": PatternFill("solid", start_color="E2EFDA", end_color="E2EFDA"),
}

INSTRUCTION_COL_CHARS_PER_LINE = 60
MIN_ROW_HEIGHT = 40
LINE_HEIGHT = 14


# ── Knowledge retrieval ───────────────────────────────────────────────────────

_KNOWN_PREFIX_KEYS = ("equipment_id:", "doc_id:", "bom_items:")


def _strip_chunk_prefix(content: str) -> str:
    """
    Strip the leading "key:value | ..." metadata segments ingest_document.py
    writes into each chunk (2-, 3- and 4-segment formats), returning only
    the chunk's own text.
    """
    parts = content.split(" | ")
    split_at = 0
    for part in parts:
        if part.startswith(_KNOWN_PREFIX_KEYS):
            split_at += 1
        else:
            break
    if split_at == 0:
        return content
    return " | ".join(parts[split_at:])


def _has_any_manual_chunks(equipment_id: str, company_id: str) -> bool:
    """Cheap existence check so a node with no manual fails fast and clearly."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT 1
                FROM knowledge_embeddings
                WHERE source_type = 'manual'
                AND company_id = %s::uuid
                AND content LIKE %s
                LIMIT 1
            """, (company_id, f"equipment_id:{equipment_id}%"))
            return cur.fetchone() is not None
    finally:
        conn.close()


def fetch_all_manual_chunk_list(equipment_id: str, company_id: str) -> list:
    """
    Every manual chunk for this equipment node, prefix-stripped, in ingest
    order. Used by the parts-list pass (which needs full recall because
    parts tables are spread across many pages) and by fetch_all_manual_chunks.
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
    return [_strip_chunk_prefix(r["content"]) for r in rows]


def fetch_all_manual_chunks(equipment_id: str, company_id: str) -> str:
    """Full-recall text blob. Kept for direct/internal callers; generate() doesn't use it."""
    chunks = fetch_all_manual_chunk_list(equipment_id, company_id)
    if not chunks:
        raise ValueError(
            f"No manual chunks found for equipment {equipment_id}. "
            "Please upload and ingest a document for this node first."
        )
    full_text = "\n\n".join(chunks)
    log.info(f"[company={company_id}] Fetched {len(chunks)} chunks ({len(full_text.split())} words) for equipment {equipment_id}")
    return full_text


def fetch_relevant_chunk_list(equipment_id: str, company_id: str, query_text: str, top_k: int = RETRIEVAL_TOP_K) -> list:
    """
    pgvector cosine-distance retrieval of the top_k chunks most relevant to
    query_text, returned as a list of prefix-stripped chunk texts. Uses the
    embeddings stored at ingest time (save_chunks() in ingest_document.py).
    Swap <=> for <-> or <#> if the embedding model is tuned for another metric.
    """
    query_embedding = get_embedding(query_text)
    emb_str = "[" + ",".join(map(str, query_embedding)) + "]"

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT content
                FROM knowledge_embeddings
                WHERE source_type = 'manual'
                AND company_id = %s::uuid
                AND content LIKE %s
                ORDER BY embedding <=> %s::vector
                LIMIT %s
            """, (company_id, f"equipment_id:{equipment_id}%", emb_str, top_k))
            rows = cur.fetchall()
    finally:
        conn.close()

    chunks = [_strip_chunk_prefix(r["content"]) for r in rows]
    log.info(
        f"[company={company_id}] Retrieved {len(chunks)}/{top_k} chunks "
        f"({sum(len(c.split()) for c in chunks)} words) for equipment {equipment_id}, query={query_text[:60]!r}"
    )
    return chunks


def fetch_relevant_manual_chunks(equipment_id: str, company_id: str, query_text: str, top_k: int = RETRIEVAL_TOP_K) -> str:
    """Joined-text wrapper around fetch_relevant_chunk_list (unchanged public behavior)."""
    return "\n\n".join(fetch_relevant_chunk_list(equipment_id, company_id, query_text, top_k))


def _merge_chunks(primary: list, extra: Optional[list]) -> list:
    """Primary chunks first, then any extra chunks not already present."""
    if not extra:
        return list(primary)
    seen = set(primary)
    merged = list(primary)
    for c in extra:
        if c not in seen:
            merged.append(c)
            seen.add(c)
    return merged


# ── Parts-table detection (for the parts-list pass) ──────────────────────────

_PARTS_KEYWORDS_RE = re.compile(
    r"part\s*no|part\s*number|parts\s*list|p/n|q'?ty|quantity|item\s*no|pos\.?\s*no|dwg|"
    r"description|spare|wear\s*part|material\s*no",
    re.IGNORECASE,
)
# Alphanumeric tokens containing a digit and a separator, e.g. 500-A-10253P1, 23120 CC-01, 0084-16050.
_PART_TOKEN_RE = re.compile(r"\b[A-Z0-9]*\d[A-Z0-9]*[-/.][A-Z0-9][A-Z0-9\-/.]*\b")


def _parts_chunk_score(text: str) -> int:
    """
    Cheap heuristic for "this chunk probably contains a parts table":
    3 points per parts-list keyword + 1 per part-number-like token. Only
    used to decide which chunks are worth an extraction call -- a false
    positive costs one call that returns an empty list.
    """
    return 3 * len(_PARTS_KEYWORDS_RE.findall(text)) + len(_PART_TOKEN_RE.findall(text))


# ── Prompts ──────────────────────────────────────────────────────────────────

def build_working_principle_prompt(manual_text: str) -> str:
    """
    Working Principle: how THIS machine physically achieves its function --
    not operator procedure, not maintenance. Deliberately uses no concrete
    example (a compressor example once caused compressor vocabulary to be
    projected onto unrelated equipment). Output shape is enforced by the
    forced tool call.
    """
    return f"""You are a maintenance engineering expert. You have been given an equipment manual below.

Your task is to extract the WORKING PRINCIPLE of THIS SPECIFIC MACHINE -- the engineering explanation of HOW IT PHYSICALLY ACHIEVES ITS FUNCTION. This answers the question "how does this machine work?", not "how does an operator run it?"

Do NOT extract:
- Operator procedures: button presses, switch positions, start/stop sequences, towing/setup instructions.
- Maintenance tasks: inspection, lubrication, replacement, cleaning, calibration.

DO extract the underlying mechanism: how the machine's core function is physically carried out, stage by stage, as material/air/fluid/energy/signal moves through the system and components interact to produce the intended output. The specific stages, components, and terminology MUST come entirely from the manual below -- do not import terminology, component names, or mechanisms from any other type of equipment.

IMPORTANT: manuals rarely have a section titled "theory of operation." This explanation is usually scattered inside maintenance, sequence-of-operation, general data, or specification sections. Read the whole excerpt for sentences that explain WHY or HOW something physically happens. If the manual genuinely doesn't explain a stage, leave it out rather than inventing it.

For each functional stage, extract:
- operation: "Operation_010", "Operation_020", ... (increment by 10), in the order the function is physically carried out
- task_list_description: "Function - Mechanism", using the manual's own terms
- frequency: blank
- hrs: blank
- work_needed: 0
- system_condition: 1
- material_number: blank
- component: the component or subsystem named in the manual that carries out this function
- instruction: a clear engineering explanation of what physically happens at this stage and why, using only what the manual describes. Number each sub-point starting at 1, each on its own line with a blank line between points (a literal "\\n\\n" between point N and N+1).
- failure_modes: blank
- source: "OEM"
- source_ref: the manual section/page this came from, if identifiable

Call the record_extracted_steps tool with your findings. If the manual has no engineering description of how the machine functions, call it with an empty tasks list.

EQUIPMENT MANUAL:
{manual_text}"""


PM_TYPE_GUIDANCE = {
    "PM1": "Inspection means a routine visual or physical CHECK with no scheduled part replacement -- e.g. checking for leaks, fastener tightness, fluid level, belt/chain condition, setup gaps. If the task's end action is replacing a part, it does NOT belong here -- that's PM4.",
    "PM2": "Lubrication means applying, topping up, or changing a lubricant, grease, or coolant itself (greasing points on a lube chart, topping up oil, changing gear oil, filling an air-line lubricator). Filter/element swaps belong in PM4.",
    "PM3": "Calibration means adjusting a device or setting to a specified reference value: regulator pressures, speeds, torques, gaps/clearances, sensor positions, temperatures, chain/belt tension to a spec -- even if the manual's heading doesn't say 'calibration'.",
    "PM4": "Replacements means the routine, scheduled swap of a wearable part or consumable on a fixed interval (filters, elements, belts, blades, knives, seals, springs, fluids). This is the ONLY category for recurring replacement tasks -- do not also list them under PM2, PM5, or PM6.",
    "PM5": "Overhaul means major teardown, rebuild, or reconditioning of an assembly. Not routine replacement (PM4) or routine fluid changes (PM2). If the manual says overhauls are outside its scope or must go to an authorized service department, return an EMPTY list.",
    "PM6": "Condition Monitoring means measuring or observing a parameter against a threshold (temperature, pressure, vibration, noise, current, oil level trend, diagnostic/fault log review) WITHOUT a replacement in the same task.",
    "PM7": "Cleaning means removing dirt, grease, glue, product, debris, or buildup from a component that stays installed. Not tasks whose primary action is replacing a part.",
    "PM8": "Safety Inspection means checking safety-critical items specifically: guards, interlocks, emergency stops, safety valves, safety decals/labels, lockout devices, and fasteners or panels tied to injury risk.",
    "PM9": "Software Back-up means backing up or restoring configuration, recipes, firmware, or software on a PLC, HMI, controller, or electronic module. If the manual describes none, return an EMPTY list.",
}

_FREQUENCY_GUIDANCE = (
    'frequency: how often, as ONE normalized code: "Shift" (every shift / before each shift), '
    '"1D" daily, "1W" weekly, "2W" every 2 weeks, "1M" monthly, "2M", "3M" quarterly, "6M", '
    '"1Y" yearly, "<n>H" for operating-hour intervals (e.g. "3000H"), "Event" for after-repair/'
    'startup/commissioning tasks. If the manual gives BOTH an hour and a calendar limit ("every '
    '3,000 h, at least every 6 months"), use the hour code and mention the calendar limit in '
    'source_ref. Leave blank if not specified.'
)

_OWNER_GUIDANCE = (
    'owner: "Operator" for simple checks, cleaning, draining, and top-ups done without tools at '
    'start of shift or while running; "Technician" for lubrication, adjustment, replacement, '
    'measurement; "Electrician" for electrical/controls work; "Specialist" for vendor or OEM work.'
)


def _setpoints_block(setpoints_text: str) -> str:
    if not setpoints_text:
        return ""
    return (
        "\n\nREFERENCE SETPOINTS (already extracted from this manual -- cite these "
        "values in acceptance_criteria when a task checks or adjusts one of them):\n"
        f"{setpoints_text}\n"
    )


def build_pm_prompt(pm_code: str, pm_name: str, manual_text: str, setpoints_text: str = "") -> str:
    category_guidance = PM_TYPE_GUIDANCE.get(pm_code, "")
    return f"""You are a maintenance engineering expert. You have been given an equipment manual excerpt below.

Your task is to extract ALL maintenance tasks that fall under the category: {pm_code} - {pm_name}

CATEGORY DEFINITION FOR {pm_code} - {pm_name}: {category_guidance}

If a task genuinely fits more than one category, extract it under the SINGLE most specific category and skip it in the others.

Tasks can come from a maintenance schedule table, a lube chart or drawing legend, a component description ("rollers should be scraped clean before each shift"), a troubleshooting note that states a routine check, or a setup section that gives a value to verify. Look in all of these.

For each task, extract:
- operation: "Operation_010", "Operation_020", ... (increment by 10)
- task_list_description: "Assembly - Subassembly - Component x[quantity]". Preserve quantities.
- {_FREQUENCY_GUIDANCE}
- hrs: technician time for the task as decimal hours (e.g. 0.1, 0.5, 1.0) -- NOT the interval. Leave blank if the manual gives no time.
- work_needed: 1 if active work is required, 0 if observation only. Default 1.
- system_condition: 0 machine stopped, 1 machine running. Default 0.
- material_number: the manual's OWN part/drawing/catalog number for this item if printed (e.g. "341047", "992-03377", "SR29-T-2-9.5"), copied exactly. Blank only if none is given.
- component: the component name only, e.g. "Bearings x8"
- instruction: step-by-step work instruction a technician can follow. Number each step from 1, each on its own line with a blank line between steps (a literal "\\n\\n" between step N and N+1). Be specific; include the lubricant/grade, tool, or reference value when the manual gives one.
- failure_modes: comma-separated failure modes this task prevents. Blank if not inferable from the manual.
- acceptance_criteria: the measurable pass/fail limit the manual gives for this task (value and unit), or the matching value from REFERENCE SETPOINTS. If there is no limit anywhere in the manual, write exactly "No limit in manual -- record baseline at first PM". NEVER invent a number.
- source: "OEM"
- source_ref: where in the manual this task comes from (section, page, table, or drawing as printed). If the manual gives DIFFERENT intervals for the same task in two places, use the SHORTER interval in frequency and state both in source_ref (e.g. "Lube chart: weekly; Sched. Maint. p.29: monthly").
- {_OWNER_GUIDANCE}

Call the record_extracted_steps tool with your findings. If no {pm_name} tasks are found, call it with an empty tasks list.{_setpoints_block(setpoints_text)}

EQUIPMENT MANUAL:
{manual_text}"""


def build_setpoints_prompt(manual_text: str) -> str:
    return f"""You are a maintenance engineering expert. From the equipment manual excerpt below, extract EVERY operating setting or specification value the manual prints, such as:
- regulator and supply pressures, temperatures, speeds, torques
- gaps, clearances, heights, positions and dimensions used for setup
- belt/chain tension specs, fluid and grease grades, fill quantities
- electrical ratings only if they are used as a check (e.g. motor full-load current)

Rules:
- Copy each value exactly as printed, with units and ranges ("40-50 psi", "1/8\\" to 1/4\\"").
- parameter: short name using the manual's terms (e.g. "R2 siderail centering pressure").
- component: the component or assembly the value applies to.
- source_ref: section/page/drawing it appears on.
- Do NOT include maintenance intervals, part numbers, or values you infer.
- If the same parameter appears with two different values, record BOTH as separate entries.

Call record_setpoints. Empty list if the manual gives no settings.

EQUIPMENT MANUAL:
{manual_text}"""


def build_parts_prompt(manual_text: str) -> str:
    return f"""You are extracting an equipment parts list. The text below is from an equipment manual and may contain one or more parts-list tables (often OCR'd from scans, so columns may be run together).

Record EVERY parts-list row you can identify:
- assembly: the assembly / parts-list title the row belongs to (carry it forward to every row under that heading).
- parts_list_ref: the parts list or drawing number of that assembly if printed (e.g. "400 A 22105PM").
- item_no: the item/position number as printed ("" if none).
- part_number: the part/drawing/catalog number EXACTLY as printed (keep spaces, hyphens, suffixes). "" if the row has none, e.g. "See electrical docs".
- description: the part name/description as printed.
- qty: quantity as printed ("1", "4 PCS", "A/R", "Not used", "1 set").
- wear_part_flag / spare_part_flag: true ONLY if the manual itself marks the row (e.g. a W or S column). Otherwise false.

Rules: do not invent rows, merge rows, or fill in missing part numbers. Skip blank template rows. Skip text that is not a parts table (troubleshooting, instructions).

Call record_parts_list_rows. Empty list if there is no parts table in this text.

MANUAL TEXT:
{manual_text}"""


def build_recommended_prompt(tasks_summary: str, gaps_summary: str, setpoints_text: str, manual_text: str) -> str:
    return f"""You are a senior reliability engineer reviewing a PM plan that was extracted from an OEM manual. The OEM content is often thin: it lists a few lubrication and replacement tasks but misses condition checks, safety function tests, and setup verification. Your job is to propose ONLY the gap-filling tasks that are clearly justified by the items listed under GAPS, using the manual excerpt for component names and context.

Propose a task only for:
1. A Tier A/B wear or critical part in GAPS with no existing task: an inspection, measurement, or replacement-on-condition task for its likely failure mode.
2. A safety device the manual mentions (emergency stop, interlock, guard door, safety limit sensor, safety valve) with no existing functional test: a periodic functional test (PM8).
3. A setpoint in REFERENCE SETPOINTS that no existing task checks: a periodic verification (PM3 or PM1).
4. The machine's primary output quality (e.g. seal, cut, weld, fill), if the manual describes it and nothing checks it: a quick per-shift check (PM6).

Hard rules:
- Do NOT duplicate or reword an EXISTING TASK.
- Numeric limits ONLY from REFERENCE SETPOINTS or the manual excerpt. Otherwise acceptance_criteria must be "No limit in manual -- record baseline at first PM and trend".
- Use the manual's component names and part numbers (material_number) where given.
- source must be "Recommended". source_ref: the part number, setpoint, or manual section that justifies the task.
- rationale: one sentence naming the gap.
- pm_code: the single most specific category (PM1-PM9).
- {_FREQUENCY_GUIDANCE}
- {_OWNER_GUIDANCE}
- instruction: numbered steps, each on its own line with a blank line between them.
- At most {MAX_RECOMMENDED_TASKS} tasks; prioritize safety tests, then Tier A parts, then setpoints, then Tier B parts.

Call record_recommended_tasks. Empty list if no gap is justified.

EXISTING TASKS (pm_code | task | component | frequency):
{tasks_summary or "(none)"}

GAPS:
{gaps_summary or "(none)"}
{_setpoints_block(setpoints_text)}
MANUAL EXCERPT:
{manual_text}"""


def build_review_prompt(tasks_summary: str, setpoints_text: str, manual_text: str) -> str:
    return f"""You are a reliability engineer doing a final review of a PM plan generated from an equipment manual. List only concrete, verifiable issues a planner must resolve before using this plan:

- conflict: the manual gives two different intervals, values, or instructions for the same item (cite both locations).
- missing_document: the manual refers to a separate manual, supplier document, or drawing that tasks depend on (e.g. "see motor manual", "see electrical documentation") -- name the document and which tasks need it.
- missing_part_number: an item the plan relies on that the manual lists without a part number.
- gap: a safety-critical or wear item the manual clearly describes that has no task in the plan.
- clarification: labels, units, or references in the manual that are inconsistent or ambiguous and could cause a wrong setting (e.g. two different names for the same valve).

Do not restate general advice. Do not list issues you cannot tie to the manual excerpt, the tasks, or the setpoints below.

Call record_review_notes. Empty list if nothing qualifies.

PLAN TASKS (pm_code | task | component | frequency | source_ref):
{tasks_summary or "(none)"}
{_setpoints_block(setpoints_text)}
MANUAL EXCERPT:
{manual_text}"""


# ── Component image lookup (UNCHANGED; disabled via IMAGE_MATCHING_ENABLED) ──
#
# Two-tier match against this equipment's own ingested manual images:
# exact material_number on the page text first, then component keyword.
# Local only (DB rows fetched once + local S3 presign), nothing can hang.

IMAGE_MATCHING_ENABLED = False

S3_BUCKET = os.environ.get("S3_BUCKET", "squaremethods")
MANUAL_IMAGE_URL_EXPIRY_SECONDS = 604800  # 7 days -- the SigV4 max

_QUANTITY_SUFFIX_RE = re.compile(r"\s*[xX]\d+\s*$")
MIN_PART_NUMBER_MATCH_LENGTH = 4


def normalize_component_for_image_search(component: str) -> Optional[str]:
    if not component:
        return None
    term = component.split(" - ")[-1]
    term = _QUANTITY_SUFFIX_RE.sub("", term)
    term = term.strip()
    return term.lower() if term else None


def fetch_manual_images_for_equipment(equipment_id: str, company_id: str) -> list:
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT s3_key, context_text, page_number
                FROM equipment_manual_images
                WHERE equipment_id = %s::uuid
                AND company_id = %s::uuid
            """, (equipment_id, company_id))
            rows = cur.fetchall()
    finally:
        conn.close()

    log.info(f"[company={company_id}] Loaded {len(rows)} manual images for equipment {equipment_id}")
    return [
        {"s3_key": r["s3_key"], "context_text": (r["context_text"] or "").lower(), "page_number": r["page_number"]}
        for r in rows
    ]


def match_manual_image(component_keyword: str, material_number: str, manual_images: list) -> tuple:
    if not manual_images:
        return None, None

    part_no = (material_number or "").strip().lower()
    if len(part_no) >= MIN_PART_NUMBER_MATCH_LENGTH:
        for img in manual_images:
            if part_no in img["context_text"]:
                return img["s3_key"], "part_number"

    if component_keyword:
        for img in manual_images:
            if component_keyword in img["context_text"]:
                return img["s3_key"], "keyword"

    return None, None


def presign_manual_image_url(s3_key: str) -> Optional[str]:
    try:
        s3 = boto3.client("s3", region_name=BEDROCK_REGION)
        return s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": S3_BUCKET, "Key": s3_key},
            ExpiresIn=MANUAL_IMAGE_URL_EXPIRY_SECONDS,
        )
    except Exception as e:
        log.warning(f"Failed to presign manual image URL for key {s3_key}: {type(e).__name__}: {e}")
        return None


def resolve_component_image_url(component_keyword: str, material_number: str, manual_images: list) -> tuple:
    manual_key, method = match_manual_image(component_keyword, material_number, manual_images)
    if not manual_key:
        return "", None
    return presign_manual_image_url(manual_key) or "", method


def attach_images_to_steps(steps: list, manual_images: list) -> dict:
    tally = {"part_number": 0, "keyword": 0}
    for step in steps:
        keyword = normalize_component_for_image_search(step.get("component", ""))
        material_number = step.get("material_number", "")
        url, method = resolve_component_image_url(keyword or "", material_number, manual_images)
        step["image_url"] = url
        if method:
            tally[method] += 1
    return tally


# ── Bedrock call layer ───────────────────────────────────────────────────────

TRUNCATED_PREFIX = "truncated"


async def _attempt_tool_call(
    prompt: str,
    tool: dict,
    result_key: str,
    company_id: str,
    label: str,
    bedrock_semaphore: asyncio.Semaphore,
    max_tokens: int = MAX_TOKENS,
) -> tuple:
    """
    One forced-tool-use call. Returns (items_or_None, error_reason_or_None, raw).
    items is None only on failure; [] is a legitimate "nothing found".

    A response with stop_reason == "max_tokens" is treated as a FAILURE
    (reason starts with TRUNCATED_PREFIX) even if a tool_use block is
    present: a truncated tool call can carry a partial list that looks
    valid but silently drops everything after the cut-off.

    bedrock_semaphore is created per generate() call (never module-level:
    a module-level asyncio.Semaphore breaks across warm Lambda invocations
    with "bound to a different event loop").
    """
    loop = asyncio.get_running_loop()
    try:
        async with bedrock_semaphore:
            raw = await loop.run_in_executor(
                None,
                lambda: call_claude(
                    messages=[{"role": "user", "content": prompt}],
                    tools=[tool],
                    tool_choice={"type": "tool", "name": tool["name"]},
                    max_tokens=max_tokens,
                    temperature=0,
                    log_context=f"[company={company_id}] {label}",
                ),
            )
    except Exception as invoke_err:
        log.error(f"[company={company_id}] {label} CALL_CLAUDE FAILED: {type(invoke_err).__name__}: {invoke_err}")
        return None, f"{type(invoke_err).__name__}: {invoke_err}", {}

    stop_reason = raw.get("stop_reason")
    if stop_reason == "max_tokens":
        reason = f"{TRUNCATED_PREFIX}: stop_reason=max_tokens at {max_tokens} tokens"
        log.warning(f"[company={company_id}] {label} {reason}")
        return None, reason, raw

    items = None
    for block in raw.get("content", []):
        if block.get("type") == "tool_use" and block.get("name") == tool["name"]:
            items = block.get("input", {}).get(result_key)
            break

    if items is None:
        reason = f"no tool_use block in response, stop_reason={stop_reason!r}"
        log.error(f"[company={company_id}] {label} {reason}. Raw content preview: {str(raw.get('content'))[:300]}")
        return None, reason, raw
    if not isinstance(items, list):
        reason = f"tool_use input.{result_key} was not a list: {type(items).__name__}"
        log.error(f"[company={company_id}] {label} {reason}")
        return None, reason, raw

    return items, None, raw


async def _attempt_bedrock_call(prompt: str, company_id: str, pm_code: str, bedrock_semaphore: asyncio.Semaphore) -> tuple:
    """Backward-compatible wrapper: one EXTRACTION_TOOL attempt for a PM category."""
    return await _attempt_tool_call(prompt, EXTRACTION_TOOL, "tasks", company_id, pm_code, bedrock_semaphore)


async def _call_with_retries(
    build_prompt: Callable[[bool], str],
    tool: dict,
    result_key: str,
    company_id: str,
    label: str,
    deadline: float,
    bedrock_semaphore: asyncio.Semaphore,
    retry_on_truncation: bool = True,
    max_attempts: int = PM_CATEGORY_MAX_ATTEMPTS,
) -> tuple:
    """
    Application-level retry loop (on top of call_claude()'s botocore
    retries). build_prompt(concise) returns the prompt; concise=True after
    a truncated attempt so the retry asks for shorter output.

    Returns (items, status, last_reason) with status "ok" / "empty" /
    "error". An empty list is a valid result and is never retried. With
    retry_on_truncation=False a truncated attempt returns immediately so
    the caller can split the input instead (parts batches).
    """
    last_reason = "not attempted"
    concise = False
    for attempt in range(1, max_attempts + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            log.error(f"[company={company_id}] {label} out of time budget before attempt {attempt}/{max_attempts} (last failure: {last_reason})")
            break
        if attempt > 1:
            log.warning(f"[company={company_id}] {label} attempt {attempt}/{max_attempts} (previous attempt failed: {last_reason})")

        items, error_reason, _raw = await _attempt_tool_call(
            build_prompt(concise), tool, result_key, company_id, label, bedrock_semaphore
        )
        if error_reason is None:
            return items, ("ok" if items else "empty"), None

        last_reason = error_reason
        if error_reason.startswith(TRUNCATED_PREFIX):
            if not retry_on_truncation:
                return [], "error", last_reason
            concise = True

        if attempt < max_attempts:
            backoff = min(PM_CATEGORY_RETRY_BACKOFF_SECONDS * attempt, max(remaining - 5, 0))
            if backoff > 0:
                await asyncio.sleep(backoff)

    log.error(f"[company={company_id}] {label} exhausted all attempts, last failure: {last_reason}")
    return [], "error", last_reason


async def call_claude_for_pm_type(
    pm_code: str,
    pm_name: str,
    equipment_id: str,
    company_id: str,
    manual_images: list,
    deadline: float,
    bedrock_semaphore: asyncio.Semaphore,
    setpoints_text: str = "",
    anchor_chunks: Optional[list] = None,
) -> tuple:
    """
    Returns (pm_code, pm_name, steps, status), status in "ok"/"empty"/"error".

    New optional args (2026-09-21, defaults keep old behavior):
      setpoints_text -- REFERENCE SETPOINTS block for acceptance criteria.
      anchor_chunks  -- maintenance-schedule chunks appended to this
                        category's retrieved context (deduplicated).
    Retrieval runs once; only the Bedrock call is retried.
    """
    loop = asyncio.get_running_loop()
    query_text = PM_QUERY_TEXT.get(pm_code, pm_name)
    try:
        chunks = await loop.run_in_executor(
            None, fetch_relevant_chunk_list, equipment_id, company_id, query_text, RETRIEVAL_TOP_K
        )
    except Exception as retrieval_err:
        log.error(f"[company={company_id}] {pm_code} RETRIEVAL FAILED: {type(retrieval_err).__name__}: {retrieval_err}")
        return pm_code, pm_name, [], "error"

    manual_text = "\n\n".join(_merge_chunks(chunks, anchor_chunks))

    def build_prompt(concise: bool) -> str:
        if pm_code == "WP":
            p = build_working_principle_prompt(manual_text)
        else:
            p = build_pm_prompt(pm_code, pm_name, manual_text, setpoints_text)
        return p + (CONCISE_RETRY_SUFFIX if concise else "")

    log.info(f"[company={company_id}] {pm_code} PROMPT LENGTH (chars): {len(build_prompt(False))}")

    steps, status, _reason = await _call_with_retries(
        build_prompt, EXTRACTION_TOOL, "tasks", company_id, pm_code, deadline, bedrock_semaphore
    )
    for s in steps:
        s.setdefault("source", "OEM")

    log.info(f"[company={company_id}] {pm_code} ({pm_name}): {len(steps)} steps extracted, status={status}")

    tally = attach_images_to_steps(steps, manual_images)
    if steps:
        log.info(
            f"[company={company_id}] {pm_code} images: {tally['part_number'] + tally['keyword']}/{len(steps)} steps matched "
            f"({tally['part_number']} by part number, {tally['keyword']} by keyword)"
        )
    return pm_code, pm_name, steps, status


# ── Setpoints pass ───────────────────────────────────────────────────────────

def format_setpoints_text(setpoints: list) -> str:
    lines = []
    for sp in setpoints:
        comp = f" [{sp.get('component')}]" if sp.get("component") else ""
        ref = f" (src: {sp.get('source_ref')})" if sp.get("source_ref") else ""
        lines.append(f"- {sp.get('parameter', '')}{comp}: {sp.get('value', '')}{ref}")
    return "\n".join(lines)


async def extract_setpoints(equipment_id: str, company_id: str, deadline: float, bedrock_semaphore: asyncio.Semaphore) -> tuple:
    """Returns (setpoints, status)."""
    loop = asyncio.get_running_loop()
    try:
        chunks = await loop.run_in_executor(
            None, fetch_relevant_chunk_list, equipment_id, company_id, SETPOINTS_QUERY_TEXT, SETPOINTS_TOP_K
        )
    except Exception as e:
        log.error(f"[company={company_id}] SETPOINTS RETRIEVAL FAILED: {type(e).__name__}: {e}")
        return [], "error"
    text = "\n\n".join(chunks)
    items, status, _ = await _call_with_retries(
        lambda concise: build_setpoints_prompt(text) + (CONCISE_RETRY_SUFFIX if concise else ""),
        SETPOINTS_TOOL, "setpoints", company_id, "SETPOINTS", deadline, bedrock_semaphore,
    )
    items = [s for s in items if (s.get("parameter") or "").strip() and (s.get("value") or "").strip()]
    log.info(f"[company={company_id}] SETPOINTS: {len(items)} extracted, status={status}")
    return items, status


# ── Parts list pass ──────────────────────────────────────────────────────────

async def _extract_parts_batch(chunks: list, company_id: str, label: str, deadline: float,
                               bedrock_semaphore: asyncio.Semaphore, depth: int = 0) -> tuple:
    """
    Extract parts rows from a batch of chunks. On truncation, split the
    batch in half and recurse (up to PARTS_MAX_SPLIT_DEPTH). Returns
    (rows, ok: bool).
    """
    text = "\n\n".join(chunks)
    rows, status, reason = await _call_with_retries(
        lambda concise: build_parts_prompt(text),
        PARTS_TOOL, "parts", company_id, label, deadline, bedrock_semaphore,
        retry_on_truncation=False,
    )
    if status != "error":
        return rows, True
    if reason and reason.startswith(TRUNCATED_PREFIX) and len(chunks) > 1 and depth < PARTS_MAX_SPLIT_DEPTH:
        mid = len(chunks) // 2
        log.info(f"[company={company_id}] {label} truncated; splitting {len(chunks)} chunks into {mid}+{len(chunks) - mid}")
        (a_rows, a_ok), (b_rows, b_ok) = await asyncio.gather(
            _extract_parts_batch(chunks[:mid], company_id, f"{label}a", deadline, bedrock_semaphore, depth + 1),
            _extract_parts_batch(chunks[mid:], company_id, f"{label}b", deadline, bedrock_semaphore, depth + 1),
        )
        return a_rows + b_rows, (a_ok and b_ok)
    return [], False


def _dedupe_parts(rows: list) -> list:
    """Chunk overlap can emit the same row twice; keep the first."""
    seen, out = set(), []
    for r in rows:
        key = (
            (r.get("assembly") or "").strip().lower(),
            (r.get("item_no") or "").strip().lower(),
            (r.get("part_number") or "").strip().lower(),
            (r.get("description") or "").strip().lower(),
        )
        if key in seen or not (r.get("description") or r.get("part_number")):
            continue
        seen.add(key)
        out.append(r)
    return out


async def extract_parts_list(equipment_id: str, company_id: str, deadline: float,
                             bedrock_semaphore: asyncio.Semaphore) -> tuple:
    """
    Returns (parts, status, notes). status: "ok", "partial", "empty", "error".
    Full recall over ALL chunks (not top-K): parts tables are spread across
    many pages and rank poorly against any single query.
    """
    loop = asyncio.get_running_loop()
    notes = []
    try:
        all_chunks = await loop.run_in_executor(None, fetch_all_manual_chunk_list, equipment_id, company_id)
    except Exception as e:
        log.error(f"[company={company_id}] PARTS FETCH FAILED: {type(e).__name__}: {e}")
        return [], "error", notes

    scored = [(i, c, _parts_chunk_score(c)) for i, c in enumerate(all_chunks)]
    candidates = [(i, c, s) for i, c, s in scored if s >= PARTS_CANDIDATE_MIN_SCORE]
    cap = PARTS_BATCH_CHUNKS * PARTS_MAX_BATCHES
    if len(candidates) > cap:
        notes.append({
            "note_type": "gap",
            "detail": f"Parts extraction capped at {cap} of {len(candidates)} candidate chunks; lowest-scoring chunks skipped. Raise PM_PARTS_MAX_BATCHES to cover the full parts list.",
            "affected": "Parts List, Critical Spares", "source_ref": "system",
        })
        keep = {i for i, _, _ in sorted(candidates, key=lambda t: -t[2])[:cap]}
        candidates = [t for t in candidates if t[0] in keep]
    candidate_texts = [c for _, c, _ in candidates]  # ingest order preserved
    log.info(f"[company={company_id}] PARTS: {len(candidate_texts)}/{len(all_chunks)} chunks look like parts tables")
    if not candidate_texts:
        return [], "empty", notes

    batches = [candidate_texts[i:i + PARTS_BATCH_CHUNKS] for i in range(0, len(candidate_texts), PARTS_BATCH_CHUNKS)]
    results = await asyncio.gather(*[
        _extract_parts_batch(b, company_id, f"PARTS#{n + 1}", deadline, bedrock_semaphore)
        for n, b in enumerate(batches)
    ])
    rows = [r for batch_rows, _ok in results for r in batch_rows]
    failed = sum(1 for _rows, ok in results if not ok)
    parts = _dedupe_parts(rows)

    if failed:
        notes.append({
            "note_type": "gap",
            "detail": f"{failed} of {len(batches)} parts-list batches failed or timed out; the parts list and spares list may be incomplete. Regenerate before relying on them.",
            "affected": "Parts List, Critical Spares", "source_ref": "system",
        })
    status = "error" if failed == len(batches) else ("partial" if failed else ("ok" if parts else "empty"))
    log.info(f"[company={company_id}] PARTS: {len(parts)} unique rows from {len(batches)} batches, status={status}")
    return parts, status, notes


# ── Critical spares (deterministic) ─────────────────────────────────────────
#
# Keyword rules, evaluated in order: tier-A wear/critical terms first, then
# structural/fastener exclusions, then tier B, then tier C. First match wins.
# Deterministic on purpose -- same parts list, same spares list, every run.
# These are STARTING tiers; the sheet says so and planners re-rank with
# failure history.

_A_RULES = [
    (r"\bknife|\bblade|\bcutter\b", "Cutting", "Edge wear or chipping; poor cut"),
    (r"(?<!for )mechanical (shaft )?seal|seal kit|\bsealer\b|seal ring", "Sealing", "Seal wear or leakage"),
    (r"heater|heating element", "Heating", "Element burnout; temperature not reached"),
    (r"nozzle", "Dispensing", "Clogging; uneven or missing pattern"),
    (r"heated hose|automatic hose|hose, heated", "Dispensing", "Hose heater failure"),
    (r"\bbelt\b|timing belt|round belt|conveyor belt|conveyer belt", "Belts", "Wear, cracking, stretch, slip"),
    (r"lamella|\bvane\b|impeller|\brotor\b|\blining\b|wear plate|wear strip|wear ring|rubber base", "Wear parts", "Wear; loss of capacity or function"),
    (r"rubber bushing|coupling insert|\bspider\b|coupling element", "Couplings", "Elastomer wear or cracking"),
    (r"sensor|proximity|\bprox\b|photo ?(eye|cell|electric)|hall effect|encoder|\bswitch\b", "Sensors", "Sensor failure or misalignment stops the cycle"),
    (r"solenoid|valve unit", "Pneumatic valves", "Coil or spool failure"),
    (r"servo", "Drives", "Motor/encoder failure; long lead time"),
    (r"filter element|element, filter", "Filtration", "Element loading"),
]
_B_RULES = [
    (r"\bchain\b|chain link|connecting link", "Chains", "Elongation or stiff links; tension loss"),
    (r"cylinder", "Pneumatics", "Seal leakage; slow or weak actuation"),
    (r"ball bushing|linear bush|linear bearing|slide bush|oilite|drymet|\bbushing\b|\bbush\b", "Bushings", "Wear or play; binding"),
    (r"oil seal|dust seal|radial seal|o-ring|\bgasket\b|packing|wiper|\bfelt\b", "Seals", "Leakage or dry running"),
    (r"spring", "Springs", "Fatigue or breakage; force loss"),
    (r"rubber rol|compression roller|roller, compression|squeezer|pinch roll", "Rollers", "Wear, flat spots, buildup"),
    (r"clutch|coupling|rod end", "Power transmission", "Slip, wear or play"),
    (r"\bmotor\b|reducer|gearbox|\bi=\s?\d|\b\d+(\.\d+)?:1\b|gear unit|gear box|\bpump\b|melter|drive unit|\bgun\b|smooth start|shuttle valve|lock-out valve", "Strategic", "Failure stops the line; long lead time"),
]
_C_RULES = [
    (r"bearing|pillow|flange unit", "Bearings", "Noise, heat, seizure"),
    (r"sprocket|sproket|pulley|\bgear\b|gear-|spur|bevel", "Power transmission", "Tooth wear"),
    (r"roller|rollar", "Rollers", "Seized or worn roller"),
    (r"regulator|regulater|gauge|gage|flow control|speed control|quick exhaust", "Pneumatic controls", "Drift or damage"),
    (r"\bfan\b", "Cooling", "Broken fan; overheating"),
]
_A_RULES_C = [(re.compile(p, re.I), c, f) for p, c, f in _A_RULES]
_B_RULES_C = [(re.compile(p, re.I), c, f) for p, c, f in _B_RULES]
_C_RULES_C = [(re.compile(p, re.I), c, f) for p, c, f in _C_RULES]

# Head nouns of parts that don't belong on a spares list (structure,
# fasteners, brackets...). Checked against the HEAD noun only (last word
# of the part name before any comma, "for", or parenthesis), so "Sensor
# bracket" and "Bolt for wear plate" are excluded while "Chain link" and
# "O-ring" are not.
_EXCLUDED_HEAD_NOUNS = set("""
bolt bolts screw screws nut nuts washer washers rivet stud spacer shim bracket brkt brkt. brackt blacket plate plates
cover frame panel label sign decal post channel standoff stand weldment housing house base leg bar extrusion guard
handle knob pull clip cotter keystock key kye support stay block box stopper hinge catch grommet arrow duct liner
tube pipe elbow nipple union tee plug cap collar pin shaft rod rail track flange strut hook holder sleeve boss joint
dog flag lug nose mount sub-base fitting ring glass
""".split())
# Head phrases that ARE wear parts even though their head noun is excluded.
_HEAD_OVERRIDE_RE = re.compile(r"wear plate|wear strip|wear ring|rubber base|seal ring", re.I)


def _head_phrase(description: str) -> str:
    d = re.sub(r"\(.*?\)", " ", (description or "").lower())
    d = d.split(",")[0]
    d = re.split(r"\s+for\s+", d)[0]
    return re.sub(r"\s+", " ", d).strip()


def classify_part(description: str, wear_flag: bool = False, spare_flag: bool = False) -> Optional[tuple]:
    """
    Returns (tier, category, likely_failure_mode) or None for parts that
    don't belong on a spares list. The manual's own W/S flags override:
    W+S -> A, W or S alone -> at least B.
    """
    full = (description or "").lower()
    head = _head_phrase(full)
    head_noun = head.split()[-1] if head.split() else ""
    excluded = head_noun.rstrip(".") in _EXCLUDED_HEAD_NOUNS and not _HEAD_OVERRIDE_RE.search(head)

    result = None
    if not excluded:
        for tier, rules, texts in (("A", _A_RULES_C, (head, full)), ("B", _B_RULES_C, (head, full)), ("C", _C_RULES_C, (head,))):
            for text in texts:
                for rx, cat, fm in rules:
                    if rx.search(text):
                        result = (tier, cat, fm)
                        break
                if result:
                    break
            if result:
                break

    if wear_flag and spare_flag:
        return ("A", result[1] if result else "Manual wear part", result[2] if result else "Wear (flagged by manual)")
    if wear_flag or spare_flag:
        if result is None:
            return ("B", "Manual-flagged part", "Flagged as wear/spare part by manual")
        if result[0] == "C":
            return ("B", result[1], result[2])
    return result


def _is_missing_part_number(pn: str) -> bool:
    p = (pn or "").strip().lower()
    return (not p) or p.startswith("see ") or "not listed" in p or p in ("n/a", "-", "ref")


def build_critical_spares(parts: list) -> list:
    """One row per assembly+part number, with machine-wide quantity per part number."""
    qty_total = defaultdict(float)
    for p in parts:
        pn = (p.get("part_number") or "").strip()
        m = re.match(r"^\s*(\d+(?:\.\d+)?)\b", p.get("qty") or "")
        if pn and m and not _is_missing_part_number(pn):
            qty_total[pn.lower()] += float(m.group(1))

    spares, seen = [], set()
    for p in parts:
        cls = classify_part(p.get("description", ""), bool(p.get("wear_part_flag")), bool(p.get("spare_part_flag")))
        if not cls:
            continue
        pn = (p.get("part_number") or "").strip()
        key = ((p.get("assembly") or "").lower(), pn.lower(), (p.get("description") or "").lower())
        if key in seen:
            continue
        seen.add(key)
        tier, cat, fm = cls
        total = qty_total.get(pn.lower())
        spares.append({
            "tier": tier, "category": cat, "failure_mode": fm,
            "assembly": p.get("assembly", ""), "item_no": p.get("item_no", ""),
            "description": p.get("description", ""), "part_number": pn,
            "qty": p.get("qty", ""),
            "total_qty": (int(total) if total and float(total).is_integer() else total) or "",
            "manual_flags": "".join(f for f, on in (("W", p.get("wear_part_flag")), ("S", p.get("spare_part_flag"))) if on),
        })
    spares.sort(key=lambda s: (s["tier"], s["assembly"], s["description"]))
    return spares


# ── Post-processing: dedupe, renumber, coverage ─────────────────────────────

def _norm(s: str) -> str:
    s = (s or "").lower()
    s = _QUANTITY_SUFFIX_RE.sub("", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def dedupe_across_categories(pm_results: list) -> tuple:
    """
    Collapse exact duplicates (same normalized task description + component)
    across PM categories into the most specific one (DEDUPE_PRIORITY).
    WP is never touched. Returns (new_pm_results, removed_count).
    """
    rank = {code: i for i, code in enumerate(DEDUPE_PRIORITY)}
    owner = {}
    for code, _name, steps, _status in pm_results:
        if code == "WP":
            continue
        for s in steps:
            key = (_norm(s.get("task_list_description")), _norm(s.get("component")))
            if key == ("", ""):
                continue
            if key not in owner or rank.get(code, 99) < rank.get(owner[key], 99):
                owner[key] = code

    removed, out, kept_keys = 0, [], set()
    for code, name, steps, status in pm_results:
        if code == "WP":
            out.append((code, name, steps, status))
            continue
        new_steps = []
        for s in steps:
            key = (_norm(s.get("task_list_description")), _norm(s.get("component")))
            if key != ("", "") and (owner.get(key) != code or (code, key) in kept_keys):
                removed += 1
                continue
            kept_keys.add((code, key))
            new_steps.append(s)
        out.append((code, name, new_steps, status))
    return out, removed


def renumber_operations(steps: list) -> None:
    for i, s in enumerate(steps, 1):
        s["operation"] = f"Operation_{i * 10:03d}"


def _task_covers_part(step: dict, spare: dict) -> bool:
    pn = (spare.get("part_number") or "").strip().lower()
    mat = (step.get("material_number") or "").lower()
    if len(pn) >= MIN_PART_NUMBER_MATCH_LENGTH and pn in mat:
        return True
    words = [w for w in _norm(spare.get("description")).split() if len(w) > 3]
    hay = _norm(f"{step.get('component', '')} {step.get('task_list_description', '')}")
    return bool(words) and all(w in hay for w in words[:2])


def find_uncovered_spares(pm_results: list, spares: list) -> list:
    all_steps = [s for code, _n, steps, _st in pm_results if code != "WP" for s in steps]
    return [sp for sp in spares if sp["tier"] in ("A", "B") and not any(_task_covers_part(s, sp) for s in all_steps)]


def summarize_tasks_for_prompt(pm_results: list, with_ref: bool = False) -> str:
    lines = []
    for code, _n, steps, _st in pm_results:
        if code == "WP":
            continue
        for s in steps:
            parts = [code, s.get("task_list_description", ""), s.get("component", ""), s.get("frequency", "")]
            if with_ref:
                parts.append(s.get("source_ref", ""))
            lines.append(" | ".join(str(p) for p in parts))
    return "\n".join(lines[:MAX_CONTEXT_TASK_LINES])


async def generate_recommended_tasks(pm_results: list, uncovered: list, setpoints_text: str,
                                     equipment_id: str, company_id: str, deadline: float,
                                     bedrock_semaphore: asyncio.Semaphore) -> tuple:
    """Returns (tasks_with_pm_code, status)."""
    loop = asyncio.get_running_loop()
    try:
        chunks = await loop.run_in_executor(
            None, fetch_relevant_chunk_list, equipment_id, company_id, RECOMMENDED_QUERY_TEXT, 25
        )
    except Exception as e:
        log.error(f"[company={company_id}] RECOMMENDED RETRIEVAL FAILED: {type(e).__name__}: {e}")
        return [], "error"

    gaps_lines = [
        f"- Tier {sp['tier']} part: {sp['description']} (P/N {sp['part_number'] or 'n/a'}, {sp['assembly']}); likely failure: {sp['failure_mode']}"
        for sp in uncovered[:MAX_CONTEXT_PART_LINES]
    ]
    gaps_lines.append("- Check the manual excerpt for safety devices and the setpoints list for values with no existing check.")
    tasks_summary = summarize_tasks_for_prompt(pm_results)
    text = "\n\n".join(chunks)

    items, status, _ = await _call_with_retries(
        lambda concise: build_recommended_prompt(tasks_summary, "\n".join(gaps_lines), setpoints_text, text)
        + (CONCISE_RETRY_SUFFIX if concise else ""),
        RECOMMENDED_TOOL, "tasks", company_id, "RECOMMENDED", deadline, bedrock_semaphore,
    )
    existing = {(_norm(s.get("task_list_description")), _norm(s.get("component")))
                for _c, _n, steps, _s in pm_results for s in steps}
    out = []
    for t in items[:MAX_RECOMMENDED_TASKS]:
        if t.get("pm_code") not in PM_NAME_BY_CODE or t.get("pm_code") == "WP":
            continue
        if (_norm(t.get("task_list_description")), _norm(t.get("component"))) in existing:
            continue
        t["source"] = "Recommended"
        out.append(t)
    log.info(f"[company={company_id}] RECOMMENDED: {len(out)} tasks kept of {len(items)} proposed, status={status}")
    return out, status


async def generate_review_notes(pm_results: list, setpoints_text: str, equipment_id: str, company_id: str,
                                deadline: float, bedrock_semaphore: asyncio.Semaphore) -> tuple:
    loop = asyncio.get_running_loop()
    try:
        chunks = await loop.run_in_executor(
            None, fetch_relevant_chunk_list, equipment_id, company_id, REVIEW_QUERY_TEXT, 20
        )
    except Exception as e:
        log.error(f"[company={company_id}] REVIEW RETRIEVAL FAILED: {type(e).__name__}: {e}")
        return [], "error"
    text = "\n\n".join(chunks)
    tasks_summary = summarize_tasks_for_prompt(pm_results, with_ref=True)
    items, status, _ = await _call_with_retries(
        lambda concise: build_review_prompt(tasks_summary, setpoints_text, text) + (CONCISE_RETRY_SUFFIX if concise else ""),
        REVIEW_TOOL, "notes", company_id, "REVIEW", deadline, bedrock_semaphore,
    )
    log.info(f"[company={company_id}] REVIEW: {len(items)} notes, status={status}")
    return items, status


def deterministic_review_notes(pm_results: list, setpoints: list, parts: list, duplicates_removed: int,
                               stage_status: dict) -> list:
    notes = []
    for code, name, _steps, status in pm_results:
        if status == "error":
            notes.append({"note_type": "gap", "detail": f"{code} - {name} was not generated this run. Regenerate before using the plan.",
                          "affected": code, "source_ref": "system"})
    for stage, st in stage_status.items():
        if st == "error":
            notes.append({"note_type": "gap", "detail": f"The {stage} stage failed this run; its sheet may be empty. Regenerate.",
                          "affected": stage, "source_ref": "system"})

    missing = [p for p in parts if _is_missing_part_number(p.get("part_number"))
               and classify_part(p.get("description", ""), bool(p.get("wear_part_flag")), bool(p.get("spare_part_flag")))]
    if missing:
        sample = "; ".join(f"{p.get('description')} ({p.get('assembly')})" for p in missing[:8])
        notes.append({"note_type": "missing_part_number",
                      "detail": f"{len(missing)} spares-relevant parts have no part number in the manual (e.g. {sample}). Get them from electrical drawings or the OEM.",
                      "affected": "Critical Spares", "source_ref": "Parts List"})

    by_param, label = defaultdict(set), {}
    for sp in setpoints:
        key = _norm(sp.get("parameter"))
        by_param[key].add((sp.get("value") or "").strip())
        label.setdefault(key, (sp.get("parameter") or "").strip())
    for param, values in by_param.items():
        if param and len(values) > 1:
            notes.append({"note_type": "conflict", "detail": f"Setpoint '{label[param]}' appears with different values: {', '.join(sorted(values))}. Confirm the correct one.",
                          "affected": "Setpoints", "source_ref": "Setpoints"})

    if duplicates_removed:
        notes.append({"note_type": "clarification",
                      "detail": f"{duplicates_removed} duplicate task(s) found in more than one PM category were kept only in the most specific category.",
                      "affected": "PM Strategy", "source_ref": "system"})
    return notes


# ── Frequency → annual labor ─────────────────────────────────────────────────

_FREQ_WORDS = {
    "SHIFT": None, "EVERYSHIFT": None, "PERSHIFT": None,
    "DAILY": ("D", 1), "WEEKLY": ("W", 1), "BIWEEKLY": ("W", 2), "MONTHLY": ("M", 1),
    "BIMONTHLY": ("M", 2), "QUARTERLY": ("M", 3), "SEMIANNUAL": ("M", 6), "SEMIANNUALLY": ("M", 6),
    "ANNUAL": ("Y", 1), "ANNUALLY": ("Y", 1), "YEARLY": ("Y", 1),
}


def occurrences_per_year(freq: str) -> Optional[float]:
    """
    Normalized frequency code -> occurrences per year, using the
    OPERATING_* assumptions. None for blank/Event/unparseable, so the
    summary shows a blank rather than a wrong number.
    """
    f = re.sub(r"[\s\-_]", "", (freq or "").upper())
    if not f or f.startswith("EVENT"):
        return None
    if f in ("SHIFT", "S", "1S", "EVERYSHIFT", "PERSHIFT"):
        return float(OPERATING_DAYS_PER_YEAR * SHIFTS_PER_DAY)
    if f in _FREQ_WORDS and _FREQ_WORDS[f]:
        unit, n = _FREQ_WORDS[f]
        f = f"{n}{unit}"
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(D|W|M|Y|H|HR|HRS|HOURS)", f)
    if not m:
        return None
    n, unit = float(m.group(1)), m.group(2)
    if n <= 0:
        return None
    if unit == "D":
        return OPERATING_DAYS_PER_YEAR / n
    if unit == "W":
        return 52 / n
    if unit == "M":
        return 12 / n
    if unit == "Y":
        return 1 / n
    return OPERATING_HOURS_PER_YEAR / n


def _hours(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ── Excel builder ─────────────────────────────────────────────────────────────

def compose_instruction(step: dict) -> str:
    """
    Long Text (Instruction) cell content: the instruction plus Acceptance,
    Owner and Source lines when present. Keeps the import format's column
    set unchanged while still carrying the new fields.
    """
    parts = [(step.get("instruction") or "").strip()]
    acc = (step.get("acceptance_criteria") or "").strip()
    if acc:
        parts.append(f"Acceptance: {acc}")
    owner = (step.get("owner") or "").strip()
    if owner:
        parts.append(f"Owner: {owner}")
    source = (step.get("source") or "").strip()
    ref = (step.get("source_ref") or "").strip()
    if source == "Recommended":
        basis = "; ".join(b for b in (ref, (step.get("rationale") or "").strip()) if b)
        parts.append("Source: Recommended (not in OEM manual) -- validate before use" + (f". Basis: {basis}" if basis else ""))
    elif ref:
        parts.append(f"Source: OEM manual -- {ref}")
    return "\n\n".join(p for p in parts if p)


def _estimate_row_height(instruction: str) -> int:
    if not instruction:
        return MIN_ROW_HEIGHT
    line_count = 0
    for segment in instruction.split("\n"):
        if segment == "":
            line_count += 1
        else:
            line_count += max(1, -(-len(segment) // INSTRUCTION_COL_CHARS_PER_LINE))
    return max(MIN_ROW_HEIGHT, line_count * LINE_HEIGHT)


def _write_table(ws, start_row: int, headers: list, rows: list, widths: list, wrap_cols=None, tier_col=None) -> int:
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = w
    for c, h in enumerate(headers, 1):
        cell = ws.cell(row=start_row, column=c, value=h)
        cell.font, cell.fill = HEADER_FONT, HEADER_FILL
        cell.alignment = Alignment(wrap_text=True, vertical="center")
    r = start_row + 1
    for row in rows:
        for c, v in enumerate(row, 1):
            cell = ws.cell(row=r, column=c, value=v)
            cell.font = DATA_FONT
            cell.alignment = WRAP_ALIGN
            if tier_col and c == tier_col and v in TIER_FILLS:
                cell.fill = TIER_FILLS[v]
                cell.alignment = Alignment(horizontal="center", vertical="top")
        r += 1
    ws.freeze_panes = ws.cell(row=start_row + 1, column=1)
    if rows:
        ws.auto_filter.ref = f"A{start_row}:{ws.cell(row=r - 1, column=len(headers)).coordinate}"
    return r


def _title(ws, title: str, note: str) -> None:
    ws.cell(row=1, column=1, value=title).font = Font(bold=True, name="Arial", size=12)
    ws.cell(row=2, column=1, value=note).font = NOTE_FONT


def _add_summary_sheet(wb, pm_results: list) -> None:
    ws = wb.create_sheet("PM Summary")
    _title(ws, "PM Summary (one row per task)",
           f"Annual hours assume {OPERATING_DAYS_PER_YEAR} operating days/yr, {SHIFTS_PER_DAY} shifts/day, "
           f"{OPERATING_HOURS_PER_YEAR} operating hours/yr (env PM_OPERATING_*). Blank = interval or task time not given.")
    rows = []
    totals = defaultdict(float)
    for code, name, steps, _status in pm_results:
        if code == "WP":
            continue
        for s in steps:
            occ = occurrences_per_year(s.get("frequency", ""))
            hrs = _hours(s.get("hrs"))
            annual = round(occ * hrs, 1) if (occ is not None and hrs is not None) else None
            owner = s.get("owner") or ""
            if annual:
                totals[owner or "Unassigned"] += annual
            rows.append([
                f"{code}-{s.get('operation', '')}", f"{code} - {name}", s.get("task_list_description", ""),
                s.get("component", ""), s.get("frequency", ""), round(occ, 1) if occ is not None else None,
                hrs, annual, owner, "Running" if str(s.get("system_condition")) == "1" else "Stopped",
                s.get("source") or "OEM", s.get("source_ref", ""), s.get("acceptance_criteria", ""),
            ])
    r = _write_table(ws, 4, ["Task ID", "PM Type", "Task", "Component", "Frequency", "Occurrences / Year",
                             "Est. Hrs", "Annual Hrs", "Owner", "Machine State", "Source", "Source Ref", "Acceptance Criteria"],
                     rows, [18, 22, 40, 24, 10, 11, 8, 9, 12, 11, 12, 30, 40])
    r += 1
    ws.cell(row=r, column=1, value="Annual hours by owner").font = SUBHEAD_FONT
    for owner, hrs in sorted(totals.items()):
        r += 1
        ws.cell(row=r, column=1, value=owner).font = DATA_FONT
        ws.cell(row=r, column=2, value=round(hrs, 1)).font = DATA_FONT
    oem = sum(1 for row in rows if row[10] == "OEM")
    r += 2
    ws.cell(row=r, column=1, value=f"Tasks: {len(rows)} ({oem} OEM, {len(rows) - oem} Recommended)").font = DATA_FONT


def _add_extra_sheets(wb, pm_results: list, extras: dict) -> None:
    _add_summary_sheet(wb, pm_results)

    ws = wb.create_sheet("Setpoints")
    _title(ws, "Setpoints (as printed in the manual)",
           "Referenced by PM acceptance criteria. Enter plant-approved values in 'Plant Standard' where they differ.")
    _write_table(ws, 4, ["Parameter", "Value", "Component", "Source Ref", "Plant Standard"],
                 [[s.get("parameter", ""), s.get("value", ""), s.get("component", ""), s.get("source_ref", ""), None]
                  for s in extras.get("setpoints", [])], [40, 28, 26, 26, 18])

    ws = wb.create_sheet("Critical Spares")
    _title(ws, "Critical Spares (starting tiers -- re-rank with failure history)",
           "Tier A: stock on site. Tier B: stock or confirm lead time. Tier C: buy on condition. "
           "Rule-based from part descriptions plus the manual's own W/S flags.")
    _write_table(ws, 4, ["Tier", "Category", "Assembly", "Item No.", "Description", "Part No.", "Qty",
                         "Total Qty (same P/N)", "Manual Flags", "Likely Failure Mode", "Min On-Hand", "Lead Time"],
                 [[s["tier"], s["category"], s["assembly"], s["item_no"], s["description"], s["part_number"], s["qty"],
                   s["total_qty"], s["manual_flags"], s["failure_mode"], None, None] for s in extras.get("spares", [])],
                 [6, 18, 26, 7, 34, 22, 8, 10, 8, 36, 11, 11], tier_col=1)

    ws = wb.create_sheet("Parts List")
    _title(ws, "Parts List (as printed in the manual)", "Verify against the manual before ordering.")
    _write_table(ws, 4, ["Assembly", "Parts List Ref", "Item No.", "Part No.", "Description", "Qty", "Wear (W)", "Spare (S)"],
                 [[p.get("assembly", ""), p.get("parts_list_ref", ""), p.get("item_no", ""), p.get("part_number", ""),
                   p.get("description", ""), p.get("qty", ""), "W" if p.get("wear_part_flag") else "",
                   "S" if p.get("spare_part_flag") else ""] for p in extras.get("parts", [])],
                 [30, 20, 8, 24, 44, 9, 8, 8])

    ws = wb.create_sheet("Review Notes")
    _title(ws, "Review Notes (resolve before using the plan)",
           "Conflicts, missing documents, missing part numbers, and gaps found during generation.")
    order = {"conflict": 0, "missing_document": 1, "gap": 2, "missing_part_number": 3, "clarification": 4}
    notes = sorted(extras.get("review_notes", []), key=lambda n: order.get(n.get("note_type"), 9))
    _write_table(ws, 4, ["Type", "Detail", "Affected", "Source Ref"],
                 [[n.get("note_type", ""), n.get("detail", ""), n.get("affected", ""), n.get("source_ref", "")] for n in notes],
                 [18, 80, 24, 24])


def build_excel(equipment_id: str, pm_results: list, extras: Optional[dict] = None) -> bytes:
    """
    Sheet 1 "PM Strategy" matches PM_strategy.xlsx exactly (same columns,
    same block layout). pm_results: (pm_code, pm_name, steps, status)
    tuples. status "error" gets a quiet but visible "not generated" row.

    extras (optional, 2026-09-21): {"setpoints", "parts", "spares",
    "review_notes"} -> extra sheets after "PM Strategy" when
    EXTRA_SHEETS_ENABLED. Omit to get the old single-sheet output.
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "PM Strategy"

    col_widths = [15, 55, 12, 8, 13, 17, 16, 35, 60, 35, 40]
    for i, w in enumerate(col_widths, 1):
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = w

    current_row = 1
    for pm_code, pm_name, steps, status in pm_results:
        header_cell = ws.cell(row=current_row, column=1, value=f"{pm_code} - {pm_name}")
        header_cell.font = Font(bold=True, color="FFFFFF", name="Arial", size=11)
        header_cell.fill = HEADER_FILL
        header_cell.alignment = Alignment(vertical="center")
        ws.row_dimensions[current_row].height = 20
        ws.merge_cells(start_row=current_row, start_column=1, end_row=current_row, end_column=len(COLUMNS))
        current_row += 1

        for col_idx, col_name in enumerate(COLUMNS, 1):
            cell = ws.cell(row=current_row, column=col_idx, value=col_name)
            cell.font = SUBHEAD_FONT
            cell.fill = SUBHEAD_FILL
            cell.alignment = Alignment(wrap_text=True, vertical="center", horizontal="center")
        ws.row_dimensions[current_row].height = 30
        current_row += 1

        if status == "error":
            error_cell = ws.cell(
                row=current_row, column=1,
                value=f"{pm_code} not generated this run -- recommend regenerating this category before sending.",
            )
            error_cell.font = INCOMPLETE_FONT
            error_cell.fill = INCOMPLETE_FILL
            error_cell.alignment = Alignment(vertical="center")
            ws.row_dimensions[current_row].height = 20
            ws.merge_cells(start_row=current_row, start_column=1, end_row=current_row, end_column=len(COLUMNS))
            current_row += 1

        for step in steps:
            instruction = compose_instruction(step)
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
                image_url,
            ]
            for col_idx, value in enumerate(row_values, 1):
                cell = ws.cell(row=current_row, column=col_idx, value=value)
                cell.font = DATA_FONT
                cell.alignment = WRAP_ALIGN
                if col_idx == len(COLUMNS) and image_url:
                    cell.hyperlink = image_url
                    cell.font = Font(name="Arial", size=10, color="0563C1", underline="single")
            ws.row_dimensions[current_row].height = _estimate_row_height(instruction)
            current_row += 1

        current_row += 1

    ws.freeze_panes = "A3"

    if extras is not None and EXTRA_SHEETS_ENABLED:
        _add_extra_sheets(wb, pm_results, extras)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.read()


def fetch_equipment_info(equipment_id: str, company_id: str) -> dict:
    """Equipment name + reference_code for the filename (soft-delete aware)."""
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
        log.warning(f"[company={company_id}] No equipment record found for {equipment_id}, falling back to equipment_id in filename")
        return {"name": "", "reference_code": ""}
    return {"name": row.get("name") or "", "reference_code": row.get("reference_code") or ""}


def build_output_filename(equipment_id: str, equipment_info: dict) -> str:
    from datetime import date

    label = " ".join(part for part in [equipment_info.get("name"), equipment_info.get("reference_code")] if part).strip()
    if not label:
        label = equipment_id
    slug = re.sub(r"[^A-Za-z0-9]+", "_", label).strip("_")[:80]
    return f"PM_Strategy_{slug}_{date.today().isoformat()}.xlsx"


# ── Main entry point ──────────────────────────────────────────────────────────

async def generate(equipment_id: str, company_id: str) -> bytes:
    """
    Full pipeline (return type unchanged: bytes).

    Phase 1  setpoints pass + schedule anchor retrieval (concurrent)
    Phase 2  WP + PM1-PM9 extraction and the parts-list pass (concurrent,
             all sharing one per-invocation semaphore), bounded by
             deadline - FINAL_PHASE_RESERVE_SECONDS
    Post     cross-category dedupe, critical spares, coverage check
    Phase 3  recommended-tasks pass + review-notes pass (concurrent),
             bounded by the full deadline
    Build    Excel: "PM Strategy" (import format) + extra sheets

    Every stage degrades independently; a real file is always returned.
    """
    if not _has_any_manual_chunks(equipment_id, company_id):
        raise ValueError(
            f"No manual chunks found for equipment {equipment_id}. "
            "Please upload and ingest a document for this node first."
        )

    manual_images = fetch_manual_images_for_equipment(equipment_id, company_id) if IMAGE_MATCHING_ENABLED else []

    start = time.monotonic()
    deadline = start + GENERATION_DEADLINE_SECONDS
    final_phase_on = RECOMMENDED_TASKS_ENABLED or REVIEW_NOTES_ENABLED
    main_deadline = deadline - (FINAL_PHASE_RESERVE_SECONDS if final_phase_on else 0)

    # Per-invocation semaphore -- never module-level (warm-container event-loop bug).
    bedrock_semaphore = asyncio.Semaphore(BEDROCK_CALL_CONCURRENCY)
    loop = asyncio.get_running_loop()
    stage_status = {}

    # ── Phase 1 ──
    async def _anchor():
        try:
            return await loop.run_in_executor(
                None, fetch_relevant_chunk_list, equipment_id, company_id, SCHEDULE_QUERY_TEXT, SCHEDULE_ANCHOR_TOP_K
            )
        except Exception as e:
            log.error(f"[company={company_id}] SCHEDULE ANCHOR RETRIEVAL FAILED: {type(e).__name__}: {e}")
            return []

    async def _setpoints():
        if not SETPOINTS_ENABLED:
            return [], "disabled"
        return await extract_setpoints(equipment_id, company_id, main_deadline, bedrock_semaphore)

    anchor_chunks, (setpoints, sp_status) = await asyncio.gather(_anchor(), _setpoints())
    stage_status["Setpoints"] = sp_status
    setpoints_text = format_setpoints_text(setpoints)

    # ── Phase 2 ── PM categories and parts batches share one semaphore, so
    # total concurrent Bedrock calls for this job stay at BEDROCK_CALL_CONCURRENCY.
    pm_coros = [
        call_claude_for_pm_type(pm_code, pm_name, equipment_id, company_id, manual_images, main_deadline,
                                bedrock_semaphore, setpoints_text=setpoints_text, anchor_chunks=anchor_chunks)
        for pm_code, pm_name in PM_TYPES
    ]

    async def _parts():
        if not PARTS_EXTRACTION_ENABLED:
            return [], "disabled", []
        return await extract_parts_list(equipment_id, company_id, main_deadline, bedrock_semaphore)

    *pm_results, (parts, parts_status, parts_notes) = await asyncio.gather(*pm_coros, _parts())
    pm_results = list(pm_results)
    stage_status["Parts List"] = parts_status

    # ── Post-processing ──
    pm_results, duplicates_removed = dedupe_across_categories(pm_results)
    spares = build_critical_spares(parts)
    uncovered = find_uncovered_spares(pm_results, spares)
    log.info(f"[company={company_id}] {len(spares)} spares ({sum(1 for s in spares if s['tier'] == 'A')} Tier A), "
             f"{len(uncovered)} Tier A/B without a covering task, {duplicates_removed} duplicate tasks removed")

    # ── Phase 3 ──
    async def _recommended():
        if not RECOMMENDED_TASKS_ENABLED:
            return [], "disabled"
        return await generate_recommended_tasks(pm_results, uncovered, setpoints_text, equipment_id, company_id,
                                                deadline, bedrock_semaphore)

    async def _review():
        if not REVIEW_NOTES_ENABLED:
            return [], "disabled"
        return await generate_review_notes(pm_results, setpoints_text, equipment_id, company_id, deadline, bedrock_semaphore)

    (recommended, rec_status), (llm_notes, review_status) = await asyncio.gather(_recommended(), _review())
    stage_status["Recommended tasks"] = rec_status
    stage_status["Review notes"] = review_status

    by_code = defaultdict(list)
    for t in recommended:
        by_code[t.pop("pm_code")].append(t)
    merged = []
    for code, name, steps, status in pm_results:
        steps = steps + by_code.get(code, [])
        if code != "WP":
            renumber_operations(steps)
        if by_code.get(code):
            attach_images_to_steps(by_code[code], manual_images)
        merged.append((code, name, steps, status))
    pm_results = merged

    review_notes = list(parts_notes) + list(llm_notes) + deterministic_review_notes(
        pm_results, setpoints, parts, duplicates_removed, stage_status
    )
    if recommended:
        review_notes.append({
            "note_type": "clarification",
            "detail": f"{len(recommended)} Recommended task(s) were added to close gaps the OEM manual leaves open. "
                      "They are marked 'Source: Recommended' in the instruction text -- review and approve each before use.",
            "affected": "PM Strategy", "source_ref": "system",
        })

    ok_count = sum(1 for _, _, _, st in pm_results if st == "ok")
    empty_count = sum(1 for _, _, _, st in pm_results if st == "empty")
    error_count = sum(1 for _, _, _, st in pm_results if st == "error")
    log.info(
        f"[company={company_id}] Generation complete for equipment {equipment_id} in {time.monotonic() - start:.0f}s. "
        f"{ok_count} ok / {empty_count} empty / {error_count} errored (of {len(PM_TYPES)} PM types); "
        f"stages: {stage_status}; {len(parts)} parts, {len(spares)} spares, {len(setpoints)} setpoints, "
        f"{len(recommended)} recommended tasks, {len(review_notes)} review notes."
    )
    if error_count or any(v == "error" for v in stage_status.values()):
        log.warning(f"[company={company_id}] Partial generation for equipment {equipment_id}; flagged in the output file.")

    extras = {"setpoints": setpoints, "parts": parts, "spares": spares, "review_notes": review_notes}
    return build_excel(equipment_id, pm_results, extras)


async def generate_with_filename(equipment_id: str, company_id: str) -> tuple:
    """
    Preferred entry point for API endpoints: (excel_bytes, filename), e.g.
    "PM_Strategy_P185WJD_Compressor_1_A102_2026-08-05.xlsx".
    """
    excel_bytes = await generate(equipment_id, company_id)
    equipment_info = fetch_equipment_info(equipment_id, company_id)
    filename = build_output_filename(equipment_id, equipment_info)
    return excel_bytes, filename
