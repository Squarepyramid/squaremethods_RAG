"""
SquareMethods Knowledge Indexer
================================
Reads the JDE/CMMS import file and indexes component-level PM knowledge
into the existing knowledge_embeddings table in RDS (pgvector).

Each record is serialized into a rich content string and embedded using
Amazon Titan Embed v2. Records are stored with source_type='squaremethods_import'
and no company_id, making them shared across all companies for job aid generation.

These records are completely separate from company-specific records
(source_type='job_aid', 'failure_mode') which are scoped by company_id
and used only for chat.

Usage:
    python squaremethods_indexer.py --file Import_File_A_for_JDE.xlsx
    python squaremethods_indexer.py --file Import_File_A_for_JDE.xlsx --dry-run

Requirements:
    pip install pandas openpyxl psycopg2-binary boto3
"""

import os
import re
import json
import uuid
import argparse
import logging

import pandas as pd
import boto3
import psycopg2
import psycopg2.extras
from app.utils.db import get_db_connection
from app.services.embeddings import get_embedding

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s"
)
log = logging.getLogger(__name__)

SHEET_TASKS    = "PM TASKS"
SHEET_DROPDOWN = "Dropdown options"
SOURCE_TYPE    = "squaremethods_import"
BATCH_SIZE     = 50

# Fixed UUID representing SquareMethods shared knowledge (not a real company).
# Used to satisfy the NOT NULL constraint on company_id while keeping
# shared records distinguishable from real company records.
SHARED_COMPANY_ID = "00000000-0000-0000-0000-000000000000"


# ── Data loading ──────────────────────────────────────────────────────────────

def load_valid_components(filepath: str) -> set:
    df = pd.read_excel(filepath, sheet_name=SHEET_DROPDOWN)
    return set(df["COMPONENT"].dropna().str.strip().tolist())


def load_tasks(filepath: str, valid_components: set) -> pd.DataFrame:
    df = pd.read_excel(filepath, sheet_name=SHEET_TASKS)
    df.columns = [str(c).strip() for c in df.columns]

    # Keep only records whose component matches the controlled vocabulary
    df["Component"] = df["Component"].astype(str).str.strip()
    df = df[df["Component"].isin(valid_components)].copy()

    # Drop rows with no task text
    df = df[df["TASK"].notna() & (df["TASK"].astype(str).str.strip() != "")].copy()

    log.info(f"Loaded {len(df)} task records with valid component types.")
    return df


def deduplicate(df: pd.DataFrame) -> pd.DataFrame:
    """
    For identical component + task + FM1 combinations,
    keep the row with the most detailed procedure text.
    """
    df["_proc_len"] = df["DETAILED PROCEDURE"].fillna("").astype(str).str.len()
    df_sorted = df.sort_values("_proc_len", ascending=False)
    deduped = df_sorted.drop_duplicates(
        subset=["Component", "TASK", "FAILURE MODE 1"],
        keep="first"
    ).drop(columns=["_proc_len"])
    log.info(f"After deduplication: {len(deduped)} records.")
    return deduped


def build_content(row: pd.Series) -> str:
    """
    Serialize a task row into a rich text string for embedding and retrieval.
    This content string is what Claude reads during job aid generation.
    """
    failure_modes = [
        str(row.get(col, "")).strip()
        for col in ["FAILURE MODE 1", "FAILURE MODE 2", "FAILURE MODE 3"]
        if pd.notna(row.get(col)) and str(row.get(col, "")).strip()
    ]
    fms_text  = ", ".join(failure_modes) if failure_modes else "General"
    proc_text = str(row.get("DETAILED PROCEDURE", "") or "").strip() or "Not specified"
    freq      = row.get("FREQUENCY")
    freq_text = str(int(freq)) + " days" if pd.notna(freq) else "Not specified"
    time_val  = row.get("TIME (HH)")
    time_text = str(round(float(time_val), 2)) + " hrs" if pd.notna(time_val) else "Not specified"

    return (
        f"Component: {str(row['Component']).strip()} | "
        f"Failure Modes: {fms_text} | "
        f"Task: {str(row['TASK']).strip()} | "
        f"PM Type: {str(row.get('PM TYPE', '') or '').strip() or 'Not specified'} | "
        f"Discipline: {str(row.get('DISCIPLINE', '') or '').strip() or 'Not specified'} | "
        f"Frequency: {freq_text} | "
        f"Estimated Time: {time_text} | "
        f"Procedure: {proc_text}"
    )


# ── DB helpers ────────────────────────────────────────────────────────────────

def clear_existing(conn):
    """Remove all previous squaremethods_import records for a clean re-index."""
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

    valid_components = load_valid_components(filepath)
    df               = load_tasks(filepath, valid_components)
    df               = deduplicate(df)

    log.info(f"Prepared {len(df)} records for indexing.")

    if dry_run:
        log.info("Dry run mode. Sample content strings:")
        for _, row in df.head(3).iterrows():
            print(build_content(row))
            print()
        return

    conn = get_db_connection()
    clear_existing(conn)

    batch   = []
    indexed = 0

    for _, row in df.iterrows():
        content    = build_content(row)
        embedding  = get_embedding(content)
        emb_str    = "[" + ",".join(map(str, embedding)) + "]"
        record_id  = str(uuid.uuid4())

        batch.append((
            record_id,          # id
            SHARED_COMPANY_ID,  # company_id - fixed UUID for shared knowledge
            SOURCE_TYPE,        # source_type
            record_id,          # source_id - unique per record
            content,            # content
            emb_str,            # embedding
            "NOW()",            # created_at
            "NOW()",            # updated_at
        ))

        if len(batch) >= BATCH_SIZE:
            flush_batch(conn, batch)
            indexed += len(batch)
            batch    = []
            log.info(f"Indexed {indexed} / {len(df)} records...")

    if batch:
        flush_batch(conn, batch)
        indexed += len(batch)

    conn.close()
    log.info(f"Indexing complete. {indexed} records inserted into knowledge_embeddings.")


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SquareMethods Knowledge Indexer")
    parser.add_argument("--file",    required=True, help="Path to the CMMS import Excel file")
    parser.add_argument("--dry-run", action="store_true", help="Preview records without writing to DB")
    args = parser.parse_args()

    run_indexer(filepath=args.file, dry_run=args.dry_run)