"""
SquareMethods - Document Ingestion Service
==========================================
Parses uploaded equipment manuals (PDF or Word) and stores
chunked embeddings in knowledge_embeddings.

Key design decisions:
  - Each document gets a unique source_id derived from its file URL
    so multiple documents on the same node coexist independently
  - Deleting a document removes only its chunks, not other documents
  - Deleting a node removes all chunks for that node

Place this file at: app/services/ingest_document.py
"""

import io
import uuid
import hashlib
import logging
import requests

import psycopg2.extras

from app.utils.db import get_db_connection
from app.services.embeddings import get_embedding

log = logging.getLogger(__name__)

CHUNK_SIZE    = 500   # approximate words per chunk
CHUNK_OVERLAP = 50    # words overlap between chunks


# ── File URL to stable UUID ───────────────────────────────────────────────────

def url_to_uuid(file_url: str) -> str:
    """
    Generate a stable UUID from the file URL.
    Same URL always produces the same UUID so we can reliably
    identify and delete chunks for a specific document.
    """
    return str(uuid.UUID(hashlib.md5(file_url.encode()).hexdigest()))


# ── Text extraction ───────────────────────────────────────────────────────────

def extract_text_from_pdf(file_bytes: bytes) -> str:
    try:
        import pypdf
        reader = pypdf.PdfReader(io.BytesIO(file_bytes))
        pages  = [page.extract_text() for page in reader.pages if page.extract_text()]
        return "\n\n".join(p.strip() for p in pages)
    except Exception as e:
        log.error(f"PDF extraction error: {e}")
        raise


def extract_text_from_docx(file_bytes: bytes) -> str:
    try:
        import docx
        doc = docx.Document(io.BytesIO(file_bytes))
        return "\n\n".join(p.text.strip() for p in doc.paragraphs if p.text.strip())
    except Exception as e:
        log.error(f"DOCX extraction error: {e}")
        raise


def extract_text(file_bytes: bytes, filename: str) -> str:
    filename_lower = filename.lower()
    if filename_lower.endswith(".pdf"):
        return extract_text_from_pdf(file_bytes)
    elif filename_lower.endswith(".docx") or filename_lower.endswith(".doc"):
        return extract_text_from_docx(file_bytes)
    else:
        raise ValueError(f"Unsupported file type: {filename}")


# ── Chunking ──────────────────────────────────────────────────────────────────

def chunk_text(text: str) -> list:
    words  = text.split()
    chunks = []
    start  = 0
    while start < len(words):
        end   = min(start + CHUNK_SIZE, len(words))
        chunk = " ".join(words[start:end]).strip()
        if chunk:
            chunks.append(chunk)
        if end == len(words):
            break
        start = end - CHUNK_OVERLAP
    return chunks


# ── DB helpers ────────────────────────────────────────────────────────────────

def clear_document_chunks(conn, file_url: str, company_id: str):
    """
    Remove all chunks for a specific document identified by file URL.
    Called when a document is deleted or re-uploaded.
    Does not affect other documents on the same node.
    """
    doc_uuid = url_to_uuid(file_url)
    with conn.cursor() as cur:
        cur.execute("""
            DELETE FROM knowledge_embeddings
            WHERE source_id = %s::uuid
            AND company_id = %s::uuid
            AND source_type = 'manual'
        """, (doc_uuid, company_id))
        deleted = cur.rowcount
    conn.commit()
    log.info(f"Removed {deleted} chunks for document {file_url}")
    return deleted


def clear_node_chunks(conn, equipment_id: str, company_id: str):
    """
    Remove ALL manual chunks for a node (equipment or component).
    Called when a node is deleted. Cleans up all its documents at once.
    """
    with conn.cursor() as cur:
        cur.execute("""
            DELETE FROM knowledge_embeddings
            WHERE source_type = 'manual'
            AND company_id = %s::uuid
            AND content LIKE %s
        """, (company_id, f"%equipment_id:{equipment_id}%"))
        deleted = cur.rowcount
    conn.commit()
    log.info(f"Removed {deleted} chunks for node {equipment_id}")
    return deleted


def save_chunks(conn, chunks: list, file_url: str, equipment_id: str, company_id: str):
    """
    Embed and save all chunks. Each chunk stores the equipment_id
    in the content prefix so we can filter by node during retrieval.
    source_id is derived from the file URL so it is unique per document.
    """
    doc_uuid = url_to_uuid(file_url)
    rows     = []

    for i, chunk in enumerate(chunks):
        # Prefix chunk with equipment_id for node-scoped retrieval
        content   = f"equipment_id:{equipment_id} | {chunk}"
        embedding = get_embedding(content)
        emb_str   = "[" + ",".join(map(str, embedding)) + "]"
        record_id = str(uuid.uuid4())

        rows.append((
            record_id,
            company_id,
            "manual",
            doc_uuid,       # source_id = document UUID (stable per file URL)
            content,
            emb_str,
            "NOW()",
            "NOW()",
        ))

        if (i + 1) % 20 == 0:
            log.info(f"Embedded {i + 1} / {len(chunks)} chunks...")

    sql = """
        INSERT INTO knowledge_embeddings
            (id, company_id, source_type, source_id, content, embedding,
             created_at, updated_at)
        VALUES %s
    """
    psycopg2.extras.execute_values(conn.cursor(), sql, rows)
    conn.commit()
    log.info(f"Saved {len(rows)} chunks for document {file_url}")


# ── Main functions ────────────────────────────────────────────────────────────

def ingest(file_url: str, equipment_id: str, company_id: str) -> dict:
    """
    Ingest a document uploaded to an equipment or component node.
    Multiple documents on the same node are handled independently.
    Re-uploading the same file replaces only its chunks.
    """
    log.info(f"Ingesting document for equipment {equipment_id}: {file_url}")

    filename   = file_url.split("/")[-1].split("?")[0]
    response   = requests.get(file_url, timeout=30)
    response.raise_for_status()
    file_bytes = response.content
    log.info(f"Downloaded {len(file_bytes)} bytes from {filename}")

    text = extract_text(file_bytes, filename)
    if not text.strip():
        raise ValueError("No text could be extracted from the document.")
    log.info(f"Extracted {len(text.split())} words.")

    chunks = chunk_text(text)
    log.info(f"Created {len(chunks)} chunks.")

    conn = get_db_connection()
    try:
        # Remove previous chunks for this specific document only
        clear_document_chunks(conn, file_url, company_id)
        save_chunks(conn, chunks, file_url, equipment_id, company_id)
    finally:
        conn.close()

    return {
        "equipment_id": equipment_id,
        "company_id":   company_id,
        "source_type":  "manual",
        "filename":     filename,
        "words":        len(text.split()),
        "chunks":       len(chunks),
        "status":       "ingested"
    }


def delete_document(file_url: str, company_id: str) -> dict:
    """
    Remove all chunks for a specific document.
    Called by Benjamin when a document is deleted from a node.
    """
    conn = get_db_connection()
    try:
        deleted = clear_document_chunks(conn, file_url, company_id)
    finally:
        conn.close()
    return {"file_url": file_url, "chunks_deleted": deleted, "status": "deleted"}


def delete_node(equipment_id: str, company_id: str) -> dict:
    """
    Remove all manual chunks for a node.
    Called by Benjamin when an equipment or component node is deleted.
    """
    conn = get_db_connection()
    try:
        deleted = clear_node_chunks(conn, equipment_id, company_id)
    finally:
        conn.close()
    return {"equipment_id": equipment_id, "chunks_deleted": deleted, "status": "deleted"}