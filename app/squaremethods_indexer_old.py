"""
SquareMethods Knowledge Indexer
================================
Indexes one embedding per component type into knowledge_embeddings.

All 228 components from the dropdown are indexed regardless of whether
they have tasks in the PM TASKS sheet. Components with no tasks get a
minimal record so Claude can still generate a reasonable job aid from
its general maintenance knowledge.

Records are stored with:
  - source_type = 'squaremethods_import'
  - company_id  = SHARED_COMPANY_ID (zero UUID)
  - source_id   = unique UUID per component type

Usage:
    PYTHONPATH=/workspaces/squaremethods_RAG python app/squaremethods_indexer.py --file app/squaremethods_import.xlsx --dry-run
    PYTHONPATH=/workspaces/squaremethods_RAG python app/squaremethods_indexer.py --file app/squaremethods_import.xlsx
"""

import uuid
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

SHEET_TASKS       = "PM TASKS"
SHEET_DROPDOWN    = "Dropdown options"
SOURCE_TYPE       = "squaremethods_import"
BATCH_SIZE        = 50
SHARED_COMPANY_ID = "00000000-0000-0000-0000-000000000000"


# ── Data loading ──────────────────────────────────────────────────────────────

def load_all_components(filepath: str) -> list:
    """Load all 228 component types from the dropdown sheet."""
    df = pd.read_excel(filepath, sheet_name=SHEET_DROPDOWN)
    components = sorted(df["COMPONENT"].dropna().str.strip().unique().tolist())
    log.info(f"Loaded {len(components)} component types from dropdown.")
    return components


def load_tasks(filepath: str, valid_components: set) -> pd.DataFrame:
    """Load task records for components that have them."""
    df = pd.read_excel(filepath, sheet_name=SHEET_TASKS)
    df.columns = [str(c).strip() for c in df.columns]
    df["Component"] = df["Component"].astype(str).str.strip()
    df = df[df["Component"].isin(valid_components)].copy()
    df = df[df["TASK"].notna() & (df["TASK"].astype(str).str.strip() != "")].copy()
    log.info(f"Loaded {len(df)} task records across {df['Component'].nunique()} component types.")
    return df


# ── Content builder ───────────────────────────────────────────────────────────

def build_component_content(component_type: str, rows: pd.DataFrame = None) -> str:
    """
    Build a rich content string for a component type.
    If rows is None or empty, build a minimal placeholder record
    so Claude can still generate a job aid from general knowledge.
    """
    lines = [f"Component: {component_type}"]

    if rows is None or rows.empty:
        lines.append("Failure Modes: Not yet documented")
        lines.append("Tasks: Not yet documented")
        return "\n".join(lines)

    # Collect all unique failure modes across all tasks
    all_failure_modes = set()
    for col in ["FAILURE MODE 1", "FAILURE MODE 2", "FAILURE MODE 3"]:
        if col in rows.columns:
            fms = rows[col].dropna().str.strip().tolist()
            all_failure_modes.update([f for f in fms if f])

    if all_failure_modes:
        lines.append(f"Failure Modes: {', '.join(sorted(all_failure_modes))}")
    else:
        lines.append("Failure Modes: Not yet documented")

    # Add each unique task with its details
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

        lines.append(task_line)

    return "\n".join(lines)


# ── DB helpers ────────────────────────────────────────────────────────────────

def clear_existing(conn):
    with conn.cursor() as cur:
        cur.execute("""
            DELETE FROM knowledge_embeddings
            WHERE source_type = %s
            AND company_id = %s::uuid
        """, (SOURCE_TYPE, SHARED_COMPANY_ID))
    conn.commit()
    log.info("Cleared existing squaremethods_import records.")


def flush_batch(conn, batch: list):
    sql = """
        INSERT INTO knowledge_embeddings
            (id, company_id, source_type, source_id, content, embedding,
             created_at, updated_at)
        VALUES %s
    """
    psycopg2.extras.execute_values(conn.cursor(), sql, batch)
    conn.commit()


# ── Main ──────────────────────────────────────────────────────────────────────

def run_indexer(filepath: str, dry_run: bool = False):
    log.info("Starting SquareMethods knowledge indexer.")

    all_components = load_all_components(filepath)
    valid_set      = set(all_components)
    df_tasks       = load_tasks(filepath, valid_set)

    # Group tasks by component for quick lookup
    grouped = {
        component: rows
        for component, rows in df_tasks.groupby("Component")
    }

    components_with_tasks    = len(grouped)
    components_without_tasks = len(all_components) - components_with_tasks
    log.info(f"Components with tasks: {components_with_tasks} | Without tasks: {components_without_tasks}")

    if dry_run:
        log.info("Dry run mode. Sample content strings:")
        samples = all_components[:2] + [c for c in all_components if c not in grouped][:1]
        for component_type in samples:
            rows    = grouped.get(component_type)
            content = build_component_content(component_type, rows)
            print(f"\n{'='*60}")
            print(content)
        log.info(f"Total records that would be indexed: {len(all_components)}")
        return

    conn = get_db_connection()
    clear_existing(conn)

    batch   = []
    indexed = 0

    for component_type in all_components:
        rows      = grouped.get(component_type)
        content   = build_component_content(component_type, rows)
        embedding = get_embedding(content)
        emb_str   = "[" + ",".join(map(str, embedding)) + "]"
        record_id = str(uuid.uuid4())

        batch.append((
            record_id,
            SHARED_COMPANY_ID,
            SOURCE_TYPE,
            record_id,
            content,
            emb_str,
            "NOW()",
            "NOW()",
        ))

        if len(batch) >= BATCH_SIZE:
            flush_batch(conn, batch)
            indexed += len(batch)
            batch    = []
            log.info(f"Indexed {indexed} / {len(all_components)} component types...")

    if batch:
        flush_batch(conn, batch)
        indexed += len(batch)

    conn.close()
    log.info(f"Indexing complete. {indexed} component types inserted into knowledge_embeddings.")


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SquareMethods Knowledge Indexer")
    parser.add_argument("--file",    required=True, help="Path to the CMMS import Excel file")
    parser.add_argument("--dry-run", action="store_true", help="Preview records without writing to DB")
    args = parser.parse_args()

    run_indexer(filepath=args.file, dry_run=args.dry_run)