"""
SquareMethods Knowledge Indexer
================================
Syncs the Excel master file to two database targets:

  1. equipment_type_defaults
       - Upserts all components from the Dropdown options sheet
       - Soft-deletes any component in the DB not in the Excel

  2. knowledge_embeddings
       - source_type = 'squaremethods_import'  (PM tasks, one record per component)
       - source_type = 'squaremethods_wp'       (Working principle, one record per component)
       - Both use company_id = SHARED_COMPANY_ID (zero UUID)

Excel is always master. Run this any time the file changes.

Usage:
    PYTHONPATH=/workspaces/squaremethods_RAG python app/squaremethods_indexer.py --file app/squaremethods_import.xlsx --dry-run
    PYTHONPATH=/workspaces/squaremethods_RAG python app/squaremethods_indexer.py --file app/squaremethods_import.xlsx
"""

import uuid
import re
import hashlib
import argparse
import logging

import pandas as pd
import psycopg2.extras

from app.utils.db import get_db_connection
from app.services.embeddings import get_embedding

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s"
)
log = logging.getLogger(__name__)

SHEET_DROPDOWN    = "Dropdown options"
SHEET_TASKS       = "PM TASKS"
SHEET_WP          = "WORKING PRINCIPLE"
SOURCE_TYPE_PM    = "squaremethods_import"
SOURCE_TYPE_WP    = "squaremethods_wp"
BATCH_SIZE        = 50
SHARED_COMPANY_ID = "00000000-0000-0000-0000-000000000000"


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_slug(name: str) -> str:
    return re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')


# ── Data loading ──────────────────────────────────────────────────────────────

def load_all_components(filepath: str) -> list:
    df = pd.read_excel(filepath, sheet_name=SHEET_DROPDOWN)
    raw = df["COMPONENT"].dropna().str.strip().tolist()

    # Detect duplicates (case-insensitive) and warn
    seen = {}
    duplicates = []
    for name in raw:
        key = name.lower()
        if key in seen:
            duplicates.append(f"'{name}' (already seen as '{seen[key]}')")
        else:
            seen[key] = name

    if duplicates:
        log.warning(f"Duplicate components found in Dropdown options sheet — these will be ignored:")
        for d in duplicates:
            log.warning(f"  DUPLICATE: {d}")

    # Deduplicate keeping first occurrence, preserve original casing
    seen_keys = set()
    components = []
    for name in raw:
        key = name.lower()
        if key not in seen_keys:
            seen_keys.add(key)
            components.append(name)

    components = sorted(components)
    log.info(f"Loaded {len(components)} unique component types from dropdown.")
    return components


def load_tasks(filepath: str, valid_components: set) -> pd.DataFrame:
    df = pd.read_excel(filepath, sheet_name=SHEET_TASKS, header=1)
    df.columns = [str(c).strip() for c in df.columns]
    df["Component"] = df["Component"].astype(str).str.strip()
    df = df[df["Component"].isin(valid_components)].copy()
    df = df[df["TASK"].notna() & (df["TASK"].astype(str).str.strip() != "")].copy()
    log.info(f"Loaded {len(df)} PM task records across {df['Component'].nunique()} components.")
    return df


def load_working_principle(filepath: str, valid_components: set) -> pd.DataFrame:
    try:
        df = pd.read_excel(filepath, sheet_name=SHEET_WP, header=1)
    except Exception:
        log.warning(f"Sheet '{SHEET_WP}' not found. Skipping working principle indexing.")
        return pd.DataFrame()
    df.columns = [str(c).strip() for c in df.columns]
    df["COMPONENT"] = df["COMPONENT"].astype(str).str.strip()
    df = df[df["COMPONENT"].isin(valid_components)].copy()
    df = df[df["TITLE"].notna() & (df["TITLE"].astype(str).str.strip() != "")].copy()
    df = df.sort_values(["COMPONENT", "STEP"])
    log.info(f"Loaded {len(df)} working principle steps across {df['COMPONENT'].nunique()} components.")
    return df


# ── Content builders ──────────────────────────────────────────────────────────

def build_pm_content(component_type: str, rows: pd.DataFrame = None) -> str:
    lines = [f"Component: {component_type}", "Type: Preventive Maintenance"]

    if rows is None or rows.empty:
        lines.append("Failure Modes: Not yet documented")
        lines.append("Tasks: Not yet documented")
        return "\n".join(lines)

    all_failure_modes = set()
    for col in ["FAILURE MODE 1", "FAILURE MODE 2", "FAILURE MODE 3"]:
        if col in rows.columns:
            fms = rows[col].dropna().str.strip().tolist()
            all_failure_modes.update([f for f in fms if f])

    if all_failure_modes:
        lines.append(f"Failure Modes: {', '.join(sorted(all_failure_modes))}")
    else:
        lines.append("Failure Modes: Not yet documented")

    lines.append("Tasks:")
    seen_tasks = set()
    for _, row in rows.iterrows():
        task = str(row["TASK"]).strip()
        if task in seen_tasks:
            continue
        seen_tasks.add(task)

        task_fms = [
            str(row.get(col, "")).strip()
            for col in ["FAILURE MODE 1", "FAILURE MODE 2", "FAILURE MODE 3"]
            if pd.notna(row.get(col)) and str(row.get(col, "")).strip()
        ]
        task_line = f"  - Task: {task}"
        if task_fms:
            task_line += f" | Failure Modes: {', '.join(task_fms)}"
        if pd.notna(row.get("PM TYPE")) and str(row.get("PM TYPE", "")).strip():
            task_line += f" | PM Type: {str(row['PM TYPE']).strip()}"
        if pd.notna(row.get("DISCIPLINE")) and str(row.get("DISCIPLINE", "")).strip():
            task_line += f" | Discipline: {str(row['DISCIPLINE']).strip()}"
        if pd.notna(row.get("FREQUENCY")):
            task_line += f" | Frequency: {int(row['FREQUENCY'])} days"
        if pd.notna(row.get("DETAILED PROCEDURE")) and str(row.get("DETAILED PROCEDURE", "")).strip():
            proc = str(row["DETAILED PROCEDURE"]).strip()[:300]
            task_line += f" | Procedure: {proc}"
        if pd.notna(row.get("IMAGE_URL")) and str(row.get("IMAGE_URL", "")).strip():
            task_line += f" | Image: {str(row['IMAGE_URL']).strip()}"

        lines.append(task_line)

    return "\n".join(lines)


def build_wp_content(component_type: str, rows: pd.DataFrame) -> str:
    lines = [f"Component: {component_type}", "Type: Working Principle", "Steps:"]
    for _, row in rows.iterrows():
        step_line = f"  - Step {int(row['STEP'])}: {str(row['TITLE']).strip()}"
        if pd.notna(row.get("INSTRUCTION")) and str(row.get("INSTRUCTION", "")).strip():
            step_line += f" | Instruction: {str(row['INSTRUCTION']).strip()[:300]}"
        if pd.notna(row.get("IMAGE_URL")) and str(row.get("IMAGE_URL", "")).strip():
            step_line += f" | Image: {str(row['IMAGE_URL']).strip()}"
        lines.append(step_line)
    return "\n".join(lines)


# ── equipment_type_defaults sync ──────────────────────────────────────────────

def sync_equipment_type_defaults(conn, components: list, dry_run: bool):
    log.info("Syncing equipment_type_defaults...")

    if dry_run:
        log.info(f"[dry-run] Would upsert {len(components)} components into equipment_type_defaults.")
        log.info(f"[dry-run] Would soft-delete any row not in the Excel list.")
        return

    with conn.cursor() as cur:
        for name in components:
            slug = make_slug(name)

            # Update existing row if name matches (restores soft-deleted too)
            cur.execute("""
                UPDATE equipment_type_defaults
                SET slug       = %s,
                    is_active  = true,
                    deleted_at = NULL,
                    updated_at = NOW()
                WHERE LOWER(name) = LOWER(%s)
            """, (slug, name))

            # Insert only if no row with this name exists (case-insensitive)
            cur.execute("""
                INSERT INTO equipment_type_defaults
                    (id, name, slug, is_active, created_at, updated_at)
                SELECT gen_random_uuid(), %s, %s, true, NOW(), NOW()
                WHERE NOT EXISTS (
                    SELECT 1 FROM equipment_type_defaults
                    WHERE LOWER(name) = LOWER(%s)
                )
            """, (name, slug, name))

        # Soft-delete anything not in the Excel (case-insensitive)
        lower_components = [c.lower() for c in components]
        cur.execute("""
            UPDATE equipment_type_defaults
            SET is_active  = false,
                deleted_at = NOW(),
                updated_at = NOW()
            WHERE LOWER(name) NOT IN %s
              AND deleted_at IS NULL
        """, (tuple(lower_components),))

        cur.execute("""
            SELECT
                COUNT(*) FILTER (WHERE is_active = true AND deleted_at IS NULL) AS active,
                COUNT(*) FILTER (WHERE deleted_at IS NOT NULL)                  AS soft_deleted
            FROM equipment_type_defaults
        """)
        result = cur.fetchone()
        log.info(f"equipment_type_defaults — active: {result['active']}, soft-deleted: {result['soft_deleted']}")

    conn.commit()


# ── DB helpers ────────────────────────────────────────────────────────────────

def load_existing_hashes(conn, source_type: str) -> set:
    """Return the set of content_hash values already stored for this source_type."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT content_hash FROM knowledge_embeddings
            WHERE source_type = %s
              AND company_id  = %s::uuid
              AND content_hash IS NOT NULL
        """, (source_type, SHARED_COMPANY_ID))
        return {row['content_hash'] for row in cur.fetchall()}


def delete_stale_records(conn, source_type: str, current_hashes: set):
    """Remove records whose content_hash is no longer in the current Excel."""
    if not current_hashes:
        return
    with conn.cursor() as cur:
        cur.execute("""
            DELETE FROM knowledge_embeddings
            WHERE source_type = %s
              AND company_id  = %s::uuid
              AND content_hash NOT IN %s
        """, (source_type, SHARED_COMPANY_ID, tuple(current_hashes)))
        deleted = cur.rowcount
    conn.commit()
    if deleted:
        log.info(f"Removed {deleted} stale '{source_type}' records no longer in Excel.")


def flush_batch(conn, batch: list):
    sql = """
        INSERT INTO knowledge_embeddings
            (id, company_id, source_type, source_id, content, embedding,
             content_hash, created_at, updated_at)
        VALUES %s
    """
    psycopg2.extras.execute_values(conn.cursor(), sql, batch)
    conn.commit()


def index_records(conn, records: list, source_type: str, label: str, dry_run: bool):
    if dry_run:
        log.info(f"[dry-run] {label} — would index {len(records)} records.")
        for component_type, content in records[:2]:
            print(f"\n{'='*60}")
            print(content)
        return

    # Load hashes already in the DB for this source_type
    existing_hashes = load_existing_hashes(conn, source_type)

    batch     = []
    indexed   = 0
    skipped   = 0
    new_hashes = set()

    for component_type, content in records:
        h = hashlib.md5(content.encode()).hexdigest()
        new_hashes.add(h)

        if h in existing_hashes:
            skipped += 1
            continue  # Content unchanged — skip Bedrock call

        embedding = get_embedding(content)
        emb_str   = "[" + ",".join(map(str, embedding)) + "]"
        record_id = str(uuid.uuid4())

        batch.append((
            record_id,
            SHARED_COMPANY_ID,
            source_type,
            record_id,
            content,
            emb_str,
            h,
            "NOW()",
            "NOW()",
        ))

        if len(batch) >= BATCH_SIZE:
            flush_batch(conn, batch)
            indexed += len(batch)
            batch    = []
            log.info(f"{label} — indexed {indexed} / {len(records)}...")

    if batch:
        flush_batch(conn, batch)
        indexed += len(batch)

    # Remove any records in the DB that are no longer in the Excel
    delete_stale_records(conn, source_type, new_hashes)

    log.info(f"{label} — complete. {indexed} new, {skipped} unchanged (skipped).")


# ── Main ──────────────────────────────────────────────────────────────────────

def run_indexer(filepath: str, dry_run: bool = False):
    log.info("Starting SquareMethods indexer.")

    # ── Load Excel ────────────────────────────────────────────────────────
    all_components = load_all_components(filepath)
    valid_set      = set(all_components)
    df_tasks       = load_tasks(filepath, valid_set)
    df_wp          = load_working_principle(filepath, valid_set)

    # ── Build PM records ──────────────────────────────────────────────────
    pm_grouped = {
        comp: rows for comp, rows in df_tasks.groupby("Component")
    }
    pm_records = [
        (comp, build_pm_content(comp, pm_grouped.get(comp)))
        for comp in all_components
    ]

    # ── Build WP records (only components that have WP steps) ─────────────
    wp_records = []
    if not df_wp.empty:
        wp_grouped = {
            comp: rows for comp, rows in df_wp.groupby("COMPONENT")
        }
        wp_records = [
            (comp, build_wp_content(comp, rows))
            for comp, rows in wp_grouped.items()
        ]

    log.info(f"PM records to index:  {len(pm_records)}")
    log.info(f"WP records to index:  {len(wp_records)}")

    if dry_run:
        log.info("=== DRY RUN — nothing will be written ===")
        conn = None
    else:
        conn = get_db_connection()

    # ── Step 1: Sync equipment_type_defaults ──────────────────────────────
    sync_equipment_type_defaults(conn, all_components, dry_run)

    # ── Step 2: Index PM knowledge ────────────────────────────────────────
    index_records(conn, pm_records, SOURCE_TYPE_PM, "PM knowledge", dry_run)

    # ── Step 3: Index working principle knowledge ─────────────────────────
    if wp_records:
        index_records(conn, wp_records, SOURCE_TYPE_WP, "Working Principle", dry_run)
    else:
        log.info("No working principle steps found. Skipping WP indexing.")

    if conn:
        conn.close()

    log.info("Indexer complete.")


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SquareMethods Knowledge Indexer")
    parser.add_argument("--file",    required=True, help="Path to the Excel import file")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing to DB")
    args = parser.parse_args()

    run_indexer(filepath=args.file, dry_run=args.dry_run)