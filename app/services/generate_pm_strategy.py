"""
SquareMethods - PM Strategy Generation Service
==============================================
Place this file at: app/services/generate_pm_strategy.py

Pulls the manual chunks most relevant to each PM type for an equipment
node, then runs ten parallel async Claude calls (Working Principle +
nine PM types) to extract and structure maintenance tasks into a
downloadable Excel file.

*** IMAGE MATCHING IS CURRENTLY DISABLED (IMAGE_MATCHING_ENABLED = False) ***
This mirrors ingest_document.py's IMAGE_EXTRACTION_ENABLED flag -- since
ingest no longer writes to equipment_manual_images, there's nothing for
this step to match against anyway, so this flag just skips the (now
pointless) DB round-trip rather than querying an always-empty table on
every generation run. The "Image" column stays in the Excel output
(the import format expects it) but every cell comes back blank while
this is off. All the matching logic (match_manual_image,
resolve_component_image_url, attach_images_to_steps,
fetch_manual_images_for_equipment) is untouched and ready to go the
moment both flags are flipped back to True.

The Excel output matches the PM_strategy.xlsx format exactly so
the reviewer can fill gaps, add image URLs, then upload it via
the import endpoint. Use generate_with_filename() rather than calling
generate() directly if you want the file to come with a real,
human-readable name (e.g. "PM_Strategy_Compressor_1_A102_2026-08-05.xlsx")
instead of having to build one yourself -- see that function's docstring.

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

CHANGE LOG (this revision):
  - RETRIEVAL REWORK (fixes slow/unreliable generation on large, normal
    -- i.e. non-scanned -- manuals): fetch_all_manual_chunks() used to
    pull EVERY chunk ever ingested for the equipment node, uncapped, and
    paste that single blob into all ten prompts unconditionally ("no
    vector search -- full recall", per the old docstring). A small
    scanned manual (a few thousand words) stayed fast; a large,
    genuinely dense native-text manual (tens of thousands of words, or
    an equipment node with more than one manual attached, since the old
    query wasn't even scoped to a single doc_id) turned into a huge
    prompt on all ten concurrent Bedrock calls -- slower, more likely to
    hit a timeout somewhere in the chain, and worse extraction quality
    from a small model (Haiku) reasoning over a much larger, noisier
    context. Replaced with fetch_relevant_manual_chunks(): a real
    pgvector similarity search, one per PM type, against a short
    type-specific query (see PM_QUERY_TEXT), capped at
    RETRIEVAL_TOP_K chunks. This uses the SAME embeddings already
    generated and stored per chunk at ingest time (see save_chunks() in
    ingest_document.py) -- they were always intended for retrieval, just
    not previously queried by vector similarity here. For a manual small
    enough that its total chunk count is under RETRIEVAL_TOP_K, this
    returns everything anyway, so small manuals (including the scanned
    ones going through Textract) see no behavior change at all.
  - Bedrock client now has an explicit timeout/retry Config
    (BEDROCK_CLIENT_CONFIG) instead of relying on botocore's undocumented
    default. Previously, a slow invoke_model call (more likely with the
    old unbounded context) would silently hit that default and get
    swallowed by call_claude_for_pm_type's broad except-clause, coming
    back as an empty PM type with no visible error -- indistinguishable
    from "Claude found nothing in this manual." The explicit
    read_timeout/connect_timeout below are sized for the now much
    smaller, bounded prompts this revision produces.
  - Image matching now tries an exact match on the manual's own part
    number (material_number) before falling back to the fuzzy
    keyword-in-page-text match on the component description. This is
    strictly additive: if material_number is blank or doesn't match
    anything, behavior is identical to before. See match_manual_image().
  - The material_number field instruction in build_pm_prompt was
    broadened from "SAP material number if mentioned" to also capture
    a manual's own parts-list number when the manual provides one --
    it was previously easy for Claude to leave this blank on manuals
    that don't use SAP terminology even when they clearly list part
    numbers (e.g. "341047").
  - fetch_all_manual_chunks / fetch_relevant_manual_chunks both handle
    the 4-segment content prefix ("equipment_id | doc_id | bom_items |
    text") written by the updated ingest_document.py, in addition to the
    older 2- and 3-segment formats, so this keeps working against both
    old and newly-ingested chunks without a backfill.
  - Image matching disabled via IMAGE_MATCHING_ENABLED (see note above).
  - Added generate_with_filename(), a thin wrapper around generate()
    that also returns a real filename instead of leaving that as a
    separate step for the caller to remember. generate() itself is
    unchanged in its return type (still bytes only) so nothing already
    calling it directly breaks.
  - CONCURRENCY CAP (fixes a real ServiceUnavailableException seen in
    production CloudWatch logs): all 10 PM types previously fired their
    invoke_model call (and their get_embedding call) fully concurrently,
    uncapped. A real log showed PM2/PM3/WP failing with
    "ServiceUnavailableException ... reached max retries: 2" within ~1.2
    seconds of each other, while the job itself completed in 38s -- i.e.
    NOT a hang/timeout, but Bedrock rejecting requests under a burst of
    10 simultaneous invoke_model calls exceeding on-demand throughput for
    this model. Added BEDROCK_INVOKE_CONCURRENCY (semaphore, default 4)
    around invoke_model and RETRIEVAL_CONCURRENCY (semaphore, default 5)
    around retrieval, so PM types run in a few concurrent waves instead
    of all 10 at once. Also raised BEDROCK_CLIENT_CONFIG's max_attempts
    from 2 to 5 as a second layer, so a transient 503 that still gets
    through the concurrency cap has more room to retry before giving up.
  - Added run_generate_pm_job(job_id, equipment_id, company_id): the
    actual SQS/queue entry point for PM generation, mirroring
    run_ingest_job() in ingest_document.py -- same pm_strategy_jobs
    table, same status='ready'/'failed' + result/error column shape.
    Previously this file only exposed generate()/generate_with_filename(),
    which return xlsx bytes directly -- fine for a synchronous caller,
    but nothing a queue worker can hand bytes back to. run_generate_pm_job
    uploads the finished file to S3 (see _upload_pm_strategy_and_presign)
    and writes a presigned download_url + filename into the job row's
    result instead. This is what actually puts PM generation on the same
    footing as ingest: no synchronous request/response window to fit
    ten concurrent Bedrock calls (plus retrieval) into, and the same
    worker Lambda / job-polling logic already built for ingest works for
    generation too.

NOTE ON WHERE THE "TIMEOUT SOMETIMES" WAS ACTUALLY COMING FROM: generation
already runs off a queue once the job is submitted -- API Gateway's 29s
ceiling only bounds the initial submit call, not this function, so it was
never the source of the reported timeouts. The real exposure was inside
this module itself: call_claude_for_pm_type()'s ten concurrent
invoke_model calls had no explicit timeout, so they inherited botocore's
undocumented default, and a slow call (more likely with the old unbounded,
full-recall prompt) would eventually hit that default and get swallowed by
the broad except-clause -- coming back as an empty PM type with no visible
error, indistinguishable from "Claude found nothing in this manual." That
still counts as a timeout from the caller's point of view even with no
API Gateway involved. The retrieval rework above shrinks and bounds every
prompt, and BEDROCK_CLIENT_CONFIG below makes the timeout explicit and
sized for that smaller prompt, so a genuinely slow call now fails fast and
visibly (logged, retried by botocore) instead of silently stalling out to
an empty result. Whatever timeout wraps the worker Lambda/consumer that
runs this job (its own function timeout, SQS visibility timeout, etc.)
should stay comfortably above BEDROCK_CLIENT_CONFIG's read_timeout * the
retry count, so a legitimately slow Bedrock call doesn't get killed by the
outer timeout before botocore's own retry/timeout logic has a chance to
resolve it.
"""

import io
import json
import logging
import asyncio
import re
import uuid
from typing import Optional

import boto3
import os
import psycopg2.extras
from botocore.config import Config
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment

from app.utils.db import get_db_connection
from app.services.embeddings import get_embedding

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

# Short, type-specific queries used to retrieve the manual chunks most
# relevant to each PM type (see fetch_relevant_manual_chunks). These are
# deliberately written as a bag of concrete keywords/synonyms rather than
# a natural-language question -- they're only ever used to produce an
# embedding for similarity search, not read by a model, so density of
# relevant vocabulary matters more than grammar.
PM_QUERY_TEXT = {
    "WP":  "working principle theory of operation how the machine physically functions mechanism stages",
    "PM1": "inspection checking visual physical condition leaks tightness fluid level wear",
    "PM2": "lubrication greasing oil change lubricant grease coolant top up nipple",
    "PM3": "calibration adjustment pressure regulator governor engine speed torque specification setting",
    "PM4": "replacement scheduled part filter element belt fluid fastener consumable change interval",
    "PM5": "overhaul rebuild teardown reconditioning major internal assembly bearings rotors gears",
    "PM6": "condition monitoring measurement gauge sensor instrument threshold vibration temperature oil analysis",
    "PM7": "cleaning removing dirt grease debris buildup housing cooler core exterior wipe",
    "PM8": "safety inspection guard safety valve emergency stop decal label fastener panel",
    "PM9": "software backup firmware configuration PLC controller electronic control module restore",
}

BEDROCK_MODEL   = "anthropic.claude-3-haiku-20240307-v1:0"
BEDROCK_REGION  = os.environ.get("AWS_REGION", "ca-central-1")
MAX_TOKENS      = 4096

# Explicit timeout/retry config for the bedrock-runtime client. Previously
# there was no Config at all here, which meant a slow invoke_model call
# (much more likely under the old unbounded-context prompts) rode on
# botocore's undocumented default read timeout, failed silently, and got
# swallowed by call_claude_for_pm_type's broad except-clause -- coming
# back indistinguishable from "Claude legitimately found nothing in this
# manual." read_timeout is sized generously above what a bounded,
# RETRIEVAL_TOP_K-limited prompt should ever need.
#
# max_attempts was raised from 2 to 5 after a real CloudWatch log showed
# PM2/PM3/WP failing with "ServiceUnavailableException ... reached max
# retries: 2): Bedrock is unable to process your request" -- all three
# within about 1.2 seconds of each other. That's Bedrock rejecting
# requests almost instantly under load, not a hang -- the job itself
# completed in 38s (see the REPORT line for that RequestId), well inside
# any Lambda timeout. The actual cause was BEDROCK_INVOKE_CONCURRENCY
# below: firing all 10 invoke_model calls simultaneously with no cap was
# bursting past whatever on-demand throughput Claude 3 Haiku has
# available in this account/region, and 2 attempts (1 retry) wasn't
# enough to ride out a transient 503 under that burst. Capping
# concurrency (fewer simultaneous requests = less burst pressure) is the
# primary fix; more retry attempts is the second layer, for whatever
# throttling still gets through the concurrency cap.
BEDROCK_CLIENT_CONFIG = Config(
    connect_timeout=10,
    read_timeout=90,
    retries={"max_attempts": 5, "mode": "standard"},
)

# Caps on how many PM types can be in-flight on each blocking call at
# once. Previously all 10 PM types fired both their retrieval
# (get_embedding + DB query) and their invoke_model call fully
# concurrently, uncapped -- see BEDROCK_CLIENT_CONFIG's comment above for
# the real ServiceUnavailableException this caused. With these caps, 10
# PM types run in ceil(10/N) waves instead of all at once; each wave is
# still concurrent within itself, so this isn't a return to fully
# sequential processing -- worst case (every call taking the full 90s
# read_timeout) is ceil(10/4) * 90s =~ 270s, comfortably inside a Lambda
# timeout sized like ingest's. Tune down further if 503s still show up
# in the logs at this concurrency; tune up only after confirming higher
# throughput is actually available (check the Bedrock console's service
# quotas for this model), not by guessing.
BEDROCK_INVOKE_CONCURRENCY = 4   # simultaneous invoke_model (generation) calls
RETRIEVAL_CONCURRENCY      = 5   # simultaneous get_embedding + DB retrieval calls

# How many of the most relevant chunks (by embedding similarity) to pull
# per PM type. ~40 chunks at ~500 words/chunk is roughly 20k words --
# generous headroom for a single maintenance category from most manuals,
# while still bounding worst-case prompt size on very large documents.
# For a manual with fewer than this many chunks total (true for every
# manual tested so far, including the scanned ones going through
# Textract), this simply returns everything -- no behavior change for
# the currently-working case.
RETRIEVAL_TOP_K = 40

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

_KNOWN_PREFIX_KEYS = ("equipment_id:", "doc_id:", "bom_items:")


def _strip_chunk_prefix(content: str) -> str:
    """
    Strip the leading "key:value | key:value | ..." metadata segments
    ingest_document.py writes into each chunk's content column, returning
    just the chunk's own text. Handles all three prefix shapes that exist
    across old and newly-ingested rows, oldest to newest:
      1. "equipment_id:{id} | {text}"                                (2 segments)
      2. "equipment_id:{id} | doc_id:{uuid} | {text}"                (3 segments)
      3. "equipment_id:{id} | doc_id:{uuid} | bom_items:... | {text}" (4 segments, current)
    Splitting on " | " with no maxsplit and consuming only the leading
    segments that match a known "key:" prefix keeps this working against
    whichever format a given chunk happens to be in, without needing to
    touch previously-ingested rows. Rejoins the remainder with " | " in
    case the chunk's own text legitimately contains that substring.
    """
    parts = content.split(" | ")
    split_at = 0
    for part in parts:
        if part.startswith(_KNOWN_PREFIX_KEYS):
            split_at += 1
        else:
            break
    if split_at == 0:
        return content  # no recognized prefix at all
    return " | ".join(parts[split_at:])


def _has_any_manual_chunks(equipment_id: str, company_id: str) -> bool:
    """
    Cheap existence check used to fail fast with a clear error before
    kicking off ten parallel embedding + retrieval + Bedrock calls for an
    equipment node that has no ingested manual at all.
    """
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


def fetch_all_manual_chunks(equipment_id: str, company_id: str) -> str:
    """
    Fetch ALL manual chunks for this equipment node and concatenate them
    into a single text block. Kept for any direct/internal callers that
    want true full recall (e.g. a debugging tool, or a future feature
    that isn't per-PM-type) -- generate() itself no longer calls this;
    see fetch_relevant_manual_chunks() for what it uses instead, and the
    CHANGE LOG above for why.
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

    clean_chunks = [_strip_chunk_prefix(r["content"]) for r in rows]
    full_text = "\n\n".join(clean_chunks)
    log.info(f"Fetched {len(clean_chunks)} chunks ({len(full_text.split())} words) for equipment {equipment_id}")
    return full_text


def fetch_relevant_manual_chunks(equipment_id: str, company_id: str, query_text: str, top_k: int = RETRIEVAL_TOP_K) -> str:
    """
    Vector-similarity retrieval of the top_k manual chunks most relevant
    to query_text, instead of dumping every chunk ever ingested for the
    equipment node into the prompt unconditionally. Uses the SAME
    embeddings already generated and stored per chunk at ingest time
    (see save_chunks() in ingest_document.py) -- this was always their
    intended purpose, just not previously queried here.

    Uses pgvector's cosine-distance operator (<=>), the standard choice
    for most text-embedding models. If the embeddings this deployment
    generates are tuned for a different distance metric, swap the
    operator below (<-> for Euclidean, <#> for inner product) to match.

    Falls back to an empty string (not an error) if this equipment has
    chunks but none happen to match well -- callers already handle an
    empty manual_text gracefully (Claude just returns an empty step list
    for that PM type, same as "the manual doesn't cover this category").
    The one-time upfront existence check in generate() is what catches
    "no manual ingested at all" -- this function assumes that's already
    been verified.
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

    clean_chunks = [_strip_chunk_prefix(r["content"]) for r in rows]
    full_text = "\n\n".join(clean_chunks)
    log.info(
        f"Retrieved {len(clean_chunks)}/{top_k} chunks ({len(full_text.split())} words) "
        f"for equipment {equipment_id}, query={query_text[:60]!r}"
    )
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
- material_number: the manual's OWN part/component number for this item, if the manual provides one -- this includes a formal SAP material number, but just as importantly it includes any parts-list, drawing, or catalog number the manual itself prints next to this component (e.g. "341047", "992-03377", "8-110-626-107"). Copy it exactly as printed, including hyphens/periods. Leave blank only if the manual truly gives no such number for this component.
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


# ── Component image lookup ───────────────────────────────────────────────────
#
# Images come ONLY from this equipment's own ingested manual
# (equipment_manual_images table, populated at ingestion time by
# ingest_document.py's PDF image extraction). No external image search
# (Openverse or otherwise) -- if the manual has no relevant image, the
# cell is left blank for the reviewer to fill, rather than substituting
# a generic stock photo that isn't actually this equipment's part.
#
# Matching is now two-tier:
#   1. Exact match on material_number (the manual's own part number, e.g.
#      "341047") against a drawing page's extracted text. This is a much
#      stronger, lower-false-positive signal than a keyword match, since
#      part numbers are specific tokens rather than descriptive words that
#      might appear on several unrelated pages ("bearing" turning up on
#      six different drawings). It only fires when Claude actually
#      populated material_number for that step AND that exact string
#      appears on some page's extracted text -- otherwise it's a silent
#      no-op and behavior falls through to tier 2, unchanged from before.
#   2. Fallback: keyword-in-page-text match on the component description,
#      same coarse approach as before.
#
# This is a fully local operation: the S3 key is already in the DB, and
# generating a presigned URL is a local signing call, not a network
# request. There's no timeout, retry, or concurrency-limiting logic
# needed here because there's nothing that can hang or rate-limit.
#
# Image lookup happens INLINE inside call_claude_for_pm_type(), right
# after each PM/WP type's steps come back from Claude. WP is included:
# its "component" field names real physical parts (e.g. "Airend",
# "Pressure Regulator"), same as PM1-PM9's.
#
# Known limitation, by design: match quality depends on the ingested
# manual actually having a relevant image AND that image's page text
# mentioning the component (by part number or by name). A manual with
# no images, or a components section whose images sit on pages that
# don't share the component's wording or number, will come back blank --
# there's no fallback beyond tier 2.

# Master switch for image matching, mirroring ingest_document.py's
# IMAGE_EXTRACTION_ENABLED. Off for now because ingest no longer writes
# to equipment_manual_images -- querying it here would just be a wasted
# round-trip against a table that's always empty. This only gates the
# fetch in generate() below; match_manual_image/resolve_component_image_url/
# attach_images_to_steps are untouched and already degrade correctly to
# all-blank image_urls when handed an empty manual_images list, so
# there's nothing else to change when this flips back to True (besides
# also re-enabling IMAGE_EXTRACTION_ENABLED in ingest_document.py so
# there's actually something to fetch).
IMAGE_MATCHING_ENABLED = False

S3_BUCKET = os.environ.get("S3_BUCKET", "squaremethods")
MANUAL_IMAGE_URL_EXPIRY_SECONDS = 604800  # 7 days -- the SigV4 max; images stay private, not permanently public

# Where a generated Excel file is uploaded so the queue worker (see
# run_generate_pm_job below) can hand back a download link instead of
# trying to return xlsx bytes from an SQS-triggered invocation with
# nothing listening for an HTTP response. Same private-by-default,
# presign-on-demand pattern as manual images -- these can contain
# proprietary equipment/maintenance detail, so no permanent public URL.
PM_STRATEGY_S3_PREFIX = "pm-strategy-output"
PM_STRATEGY_URL_EXPIRY_SECONDS = 604800  # 7 days -- the SigV4 max

_QUANTITY_SUFFIX_RE = re.compile(r"\s*[xX]\d+\s*$")

# Guard against spurious substring matches on short/generic material
# numbers (e.g. a stray "24" matching all over a page of tables). Real
# manual part numbers in the manuals we've seen are consistently longer
# than this, so it's a cheap, conservative filter rather than a tuned one.
MIN_PART_NUMBER_MATCH_LENGTH = 4


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


def fetch_manual_images_for_equipment(equipment_id: str, company_id: str) -> list:
    """
    Pulls every extracted manual image row for this equipment ONCE, up
    front, so matching against many components doesn't mean many DB
    round-trips. Returns a list of {s3_key, context_text, page_number}
    dicts. Empty list if the equipment has no ingested images (e.g. a
    DOCX manual, or a PDF with no images that cleared the size filter).
    """
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

    log.info(f"Loaded {len(rows)} manual images for equipment {equipment_id}")
    return [
        {"s3_key": r["s3_key"], "context_text": (r["context_text"] or "").lower(), "page_number": r["page_number"]}
        for r in rows
    ]


def match_manual_image(component_keyword: str, material_number: str, manual_images: list) -> tuple:
    """
    Two-tier match against this equipment's own ingested manual images.
    Returns (s3_key, match_method) where match_method is "part_number",
    "keyword", or None if nothing matched. Returning the method alongside
    the key is just for the summary logging in call_claude_for_pm_type --
    callers that don't care can ignore it.
    """
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
    """
    Generate a presigned GET URL for a manual image. Images are stored
    privately (see ingest_document.py) since manuals may be confidential
    OEM documents -- this is a local signing operation, not a network
    call, so it can't hang or fail on connectivity.
    """
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
    """
    Fully synchronous and local -- no network call, so nothing here can
    hang or need a timeout. Returns (url, match_method); url is "" if
    the equipment's manual has no matching image, in which case the
    Excel cell is simply left blank.
    """
    manual_key, method = match_manual_image(component_keyword, material_number, manual_images)
    if not manual_key:
        return "", None
    return presign_manual_image_url(manual_key) or "", method


def attach_images_to_steps(steps: list, manual_images: list) -> dict:
    """
    Mutates each step dict in place with an "image_url" key, matched
    against this equipment's own ingested manual images. Purely
    synchronous -- no async/await needed since there's no network call
    involved anywhere in this path.

    Returns a small {"part_number": n, "keyword": n} tally for logging.
    """
    tally = {"part_number": 0, "keyword": 0}
    for step in steps:
        keyword = normalize_component_for_image_search(step.get("component", ""))
        material_number = step.get("material_number", "")
        url, method = resolve_component_image_url(keyword or "", material_number, manual_images)
        step["image_url"] = url
        if method:
            tally[method] += 1
    return tally


async def call_claude_for_pm_type(
    bedrock,
    pm_code: str,
    pm_name: str,
    equipment_id: str,
    company_id: str,
    manual_images: list,
    retrieval_semaphore: asyncio.Semaphore,
    invoke_semaphore: asyncio.Semaphore,
) -> tuple[str, str, list]:
    log.debug(f"{pm_code} ENTERED call_claude_for_pm_type, retrieval starting now")
    loop = asyncio.get_event_loop()

    # Retrieval runs per-PM-type and concurrently with the other nine
    # (via run_in_executor, since get_embedding()/psycopg2 are blocking
    # calls) rather than once upfront for the whole document -- see the
    # CHANGE LOG at the top of this file for why full-recall was replaced
    # with per-type similarity search. Bounded by retrieval_semaphore
    # (RETRIEVAL_CONCURRENCY) so all 10 PM types don't hit get_embedding
    # and the DB simultaneously -- see BEDROCK_INVOKE_CONCURRENCY's
    # comment for why uncapped concurrency here was a real problem for
    # the invoke_model call; the same risk applies to embedding calls,
    # just not yet observed in a log the way the Bedrock 503s were.
    query_text = PM_QUERY_TEXT.get(pm_code, pm_name)
    try:
        async with retrieval_semaphore:
            manual_text = await loop.run_in_executor(
                None, fetch_relevant_manual_chunks, equipment_id, company_id, query_text
            )
    except Exception as retrieval_err:
        log.error(f"{pm_code} RETRIEVAL FAILED: {type(retrieval_err).__name__}: {retrieval_err}")
        return pm_code, pm_name, []

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
        try:
            # Bounded by invoke_semaphore (BEDROCK_INVOKE_CONCURRENCY) --
            # see that constant's comment. This is the fix for the actual
            # ServiceUnavailableException seen in production logs: all 10
            # PM types firing invoke_model at once was bursting past
            # Bedrock's on-demand throughput for this model.
            async with invoke_semaphore:
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

        # Resolve manual images for this type's steps now. Purely local
        # (DB already fetched, presigning is local signing) -- no network
        # call, so this can't hang. Runs for WP too -- its components
        # (e.g. "Airend", "Pressure Regulator") are genuine physical
        # parts, same as PM1-PM9's.
        tally = attach_images_to_steps(steps, manual_images)
        matched = tally["part_number"] + tally["keyword"]
        if steps:
            log.info(
                f"{pm_code} images: {matched}/{len(steps)} steps matched "
                f"({tally['part_number']} by part number, {tally['keyword']} by keyword)"
            )

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
                image_url,  # manual reference URL if matched, blank otherwise -- reviewer edits/replaces as needed
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


# ── Output delivery: S3 upload + presigned URL (for the queue worker) ─────────

def _upload_pm_strategy_and_presign(excel_bytes: bytes, filename: str, equipment_id: str, company_id: str) -> str:
    """
    Upload a generated Excel file to S3 and return a presigned download
    URL. Used only by the queue entry point (run_generate_pm_job) below --
    a synchronous caller of generate()/generate_with_filename() already
    gets the bytes directly and has no need for this.

    Key is namespaced by company/equipment/uuid so re-running generation
    for the same equipment doesn't collide with or overwrite a previous
    run's output -- each job's file is independently addressable and
    independently expiring. Same private-by-default pattern as manual
    images (see presign_manual_image_url): the object itself is never
    public, only a time-limited signed URL is handed back.
    """
    s3 = boto3.client("s3", region_name=BEDROCK_REGION)
    s3_key = f"{PM_STRATEGY_S3_PREFIX}/{company_id}/{equipment_id}/{uuid.uuid4()}/{filename}"

    s3.put_object(
        Bucket=S3_BUCKET,
        Key=s3_key,
        Body=excel_bytes,
        ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    return s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": S3_BUCKET, "Key": s3_key},
        ExpiresIn=PM_STRATEGY_URL_EXPIRY_SECONDS,
    )


# ── Main entry point ──────────────────────────────────────────────────────────

async def generate(equipment_id: str, company_id: str) -> bytes:
    """
    Full pipeline called from the FastAPI endpoint.
    1. Fail fast if this equipment has no ingested manual at all
    2. Run ten parallel Claude calls (Working Principle + nine PM types),
       each retrieving its own most-relevant manual chunks (see
       fetch_relevant_manual_chunks) and resolving its own stock
       component images inline
    3. Build and return the Excel file as bytes
       -- always returns a valid file even if all PM types are empty

    Return type is unchanged (bytes only) so nothing already calling
    generate() directly breaks. For a real filename bundled with the
    bytes instead of a generic "download.xlsx", call
    generate_with_filename() instead -- see that function below.

    Example endpoint usage (preferred -- real filename included):
        excel_bytes, filename = await generate_with_filename(equipment_id, company_id)
        return Response(
            content=excel_bytes,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    """
    if not _has_any_manual_chunks(equipment_id, company_id):
        raise ValueError(
            f"No manual chunks found for equipment {equipment_id}. "
            "Please upload and ingest a document for this node first."
        )

    bedrock = boto3.client("bedrock-runtime", region_name=BEDROCK_REGION, config=BEDROCK_CLIENT_CONFIG)

    # Fetched once, up front -- this equipment's own manual images are
    # matched in-memory against every step's component keyword, rather
    # than one DB round-trip per keyword. No network client needed for
    # image lookup anymore: it's a local list match + local S3 presign.
    #
    # Skipped entirely while IMAGE_MATCHING_ENABLED is False: ingest no
    # longer writes to equipment_manual_images, so this table is always
    # empty right now and querying it would just be wasted latency. The
    # downstream matching code already handles an empty list correctly
    # (every step's image_url comes back ""), so this is the only line
    # that needs to change to turn image matching off end-to-end.
    manual_images = fetch_manual_images_for_equipment(equipment_id, company_id) if IMAGE_MATCHING_ENABLED else []

    # See BEDROCK_INVOKE_CONCURRENCY / RETRIEVAL_CONCURRENCY's comments --
    # these semaphores are what actually stop all 10 PM types from
    # bursting Bedrock at once, which is what produced the
    # ServiceUnavailableException seen in production. Created fresh per
    # generate() call (not module-level) since an asyncio.Semaphore is
    # bound to the event loop it's created on, and asyncio.run() (see
    # run_generate_pm_job) creates a new loop per job.
    retrieval_semaphore = asyncio.Semaphore(RETRIEVAL_CONCURRENCY)
    invoke_semaphore     = asyncio.Semaphore(BEDROCK_INVOKE_CONCURRENCY)

    tasks = [
        call_claude_for_pm_type(
            bedrock, pm_code, pm_name, equipment_id, company_id, manual_images,
            retrieval_semaphore, invoke_semaphore,
        )
        for pm_code, pm_name in PM_TYPES
    ]
    results = await asyncio.gather(*tasks)

    pm_results = list(results)

    populated = sum(1 for _, _, steps in pm_results if steps)
    log.info(f"Generation complete. {populated} / {len(PM_TYPES)} PM types have tasks.")

    return build_excel(equipment_id, pm_results)


async def generate_with_filename(equipment_id: str, company_id: str) -> tuple:
    """
    Preferred entry point for API endpoints: does everything generate()
    does, and also returns a real, human-readable filename alongside the
    bytes -- e.g. "PM_Strategy_P185WJD_Compressor_1_A102_2026-08-05.xlsx"
    instead of whatever generic name a browser falls back to when a
    Content-Disposition header isn't set.

    This exists so the filename can't be forgotten as a separate step --
    generate() itself still returns bytes only and is unchanged, for
    anything already calling it directly.

    Returns: (excel_bytes: bytes, filename: str)
    """
    excel_bytes = await generate(equipment_id, company_id)
    equipment_info = fetch_equipment_info(equipment_id, company_id)
    filename = build_output_filename(equipment_id, equipment_info)
    return excel_bytes, filename


# ── Queue entry point ─────────────────────────────────────────────────────────

def run_generate_pm_job(job_id: str, equipment_id: str, company_id: str):
    """
    Called when Lambda is triggered by SQS for a generate_pm_strategy job --
    the same queue/job pattern ingest_document.py's run_ingest_job already
    runs on, via the same pm_strategy_jobs table. PM generation was always
    submitted as a job rather than answered inline (see the module
    docstring's "NOTE ON WHERE THE TIMEOUT WAS ACTUALLY COMING FROM"), but
    up to now nothing in this file was actually shaped as the queue's
    entry point -- generate()/generate_with_filename() return xlsx bytes
    in memory, which is fine for a synchronous caller but has nothing to
    hand bytes back to when invoked off SQS. This function is that entry
    point: same job_id/status/result/error shape as run_ingest_job, so
    the same worker Lambda and the same pm_strategy_jobs polling logic on
    the frontend work for both ingest and generation without needing to
    special-case which kind of job it's looking at.

    Because ten concurrent Bedrock calls plus (optionally) ten retrieval
    round-trips can genuinely take a few minutes on a large manual, this
    runs with the same freedom from a synchronous request/response window
    that made ingest's up-to-15-minute runs possible -- whatever SQS
    visibility timeout and Lambda function timeout you have configured
    for the ingest consumer should also comfortably cover a generation
    run; there's nothing generation-specific that needs a different
    ceiling.

    Unlike run_ingest_job's result (a small JSON summary with no binary
    payload to move), a generated strategy IS a binary file, so this
    uploads it to S3 first (see _upload_pm_strategy_and_presign) and
    writes only a presigned download_url + filename into the job's
    result JSON -- the job row itself never carries the xlsx bytes.
    """
    try:
        excel_bytes, filename = asyncio.run(generate_with_filename(equipment_id, company_id))
        download_url = _upload_pm_strategy_and_presign(excel_bytes, filename, equipment_id, company_id)

        result = {
            "equipment_id":  equipment_id,
            "company_id":    company_id,
            "filename":      filename,
            "download_url":  download_url,
            "expires_in":    PM_STRATEGY_URL_EXPIRY_SECONDS,
            "status":        "generated",
        }

        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE pm_strategy_jobs
                    SET status = 'ready', result = %s::jsonb, updated_at = NOW()
                    WHERE id = %s::uuid
                """, (psycopg2.extras.Json(result), job_id))
            conn.commit()
        finally:
            conn.close()

        log.info(f"PM STRATEGY GENERATION JOB COMPLETE [{job_id}]")

    except Exception as e:
        log.error(f"PM STRATEGY GENERATION JOB ERROR [{job_id}]: {str(e)}")
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE pm_strategy_jobs
                    SET status = 'failed', error = %s, updated_at = NOW()
                    WHERE id = %s::uuid
                """, (str(e), job_id))
            conn.commit()
        finally:
            conn.close()