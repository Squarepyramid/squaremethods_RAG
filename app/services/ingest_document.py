"""
SquareMethods - Document Ingestion Service
==========================================
Parses uploaded equipment manuals (PDF or Word) and stores
chunked embeddings in knowledge_embeddings. Also (see IMAGE_EXTRACTION_ENABLED
below) can extract embedded images from PDF manuals and store them in S3 +
equipment_manual_images, so PM strategy generation can reference the
manual's OWN component images instead of a generic stock photo pulled
from the open web.

*** IMAGE EXTRACTION IS CURRENTLY DISABLED (IMAGE_EXTRACTION_ENABLED = False) ***
Per-image S3 uploads were adding meaningful latency to ingestion,
especially on manuals with many embedded drawings. Turned off for now so
ingestion stays fast; all the image extraction/storage code below is
intact and unused, not deleted -- flip the flag back to True to resume.
See that constant's comment for what changes when you do. Nothing about
re-enabling it requires re-ingesting already-processed documents' TEXT
chunks; only documents ingested while the flag is off will be missing
images until they're re-ingested.

*** OCR FALLBACK FOR SCANNED PDFS (NEW) ***
A meaningful fraction of manufacturer-supplied manuals are flat scans --
every page is a raster image with no text layer at all (pypdf's
extract_text() returns "" for every page). Previously that made
extract_text() raise "No text could be extracted from the document." and
the whole ingest job would fail, even though the pages are perfectly
readable to a human. See OCR_ENABLED below: each page's native text is
now checked, and any page that comes back empty/near-empty is rendered
to an image and OCR'd with Tesseract, page-by-page, so manuals that mix
real text pages with scanned drawing pages only pay OCR's cost on the
pages that actually need it.

DEPLOYMENT REQUIREMENT for OCR: the `tesseract` binary + English language
data must be present in the runtime, plus the `pymupdf` and `pytesseract`
Python packages (add both to requirements.txt). On Lambda this means
attaching a layer that bundles the tesseract binary (e.g.
https://github.com/bweigel/aws-lambda-tesseract-layer, or a custom layer
built for your runtime/arch) -- `pip install pytesseract` alone is NOT
enough, it just calls out to a `tesseract` executable that has to already
be on PATH. Until that layer is attached, OCR is skipped automatically
and safely (see _ocr_available()): scanned pages come back empty and a
document that is ENTIRELY scanned will still raise "No text could be
extracted", same as before, with a log line explaining OCR wasn't
available. No code change is needed to turn OCR on once the layer is
in place -- OCR_ENABLED just needs the binary to actually be there.

Key design decisions:
  - Each chunk gets a stable UUID derived from (doc_uuid, chunk_index)
    via uuid5, so re-ingesting the same file produces the same chunk UUIDs
  - doc_uuid is embedded in the content prefix so clear_document_chunks
    can delete all chunks for a specific document without a separate column
  - Deleting a document removes only its chunks, not other documents
  - Deleting a node removes all chunks for that node
  - Ingest runs as an async background job via SQS (see run_ingest_job)
  - Embeddings are generated concurrently in bounded batches to handle
    large documents (50+ pages) within the Lambda timeout window
  - NEW: PDF text extraction falls back to OCR (Tesseract) on a
    per-page basis for pages with no usable native text layer, so
    scanned/flatbed-scanned manuals ingest instead of failing outright.
    OCR pages are also run with bounded concurrency (tesseract runs as
    a subprocess, so threads genuinely parallelize it) to keep a
    fully-scanned 50-100 page manual within the Lambda timeout window.
  - Extracted manual images are associated with their PAGE's text as
    "context_text" (not a precise per-figure caption) -- OEM manuals
    rarely have clean one-image-per-component layouts; a parts-list
    page usually has one exploded-view image covering many components,
    with a table of callouts below it. Page-level association is a
    reasonable middle ground: coarser than a true caption match, but
    still specific to the actual equipment, not a generic web photo.
    That page text now goes through the same native-or-OCR extraction
    as the main text pass, so scanned pages get real context_text too
    once IMAGE_EXTRACTION_ENABLED is turned back on.
  - Extracted images are size-filtered (MIN_IMAGE_BYTES) as a first-pass
    heuristic to skip small logos/watermarks/icons -- not perfect, but
    avoids flooding storage with dozens of duplicate section-header
    logos that OEM manuals commonly repeat on every divider page.
  - S3 stores images privately; only the S3 KEY is saved in the DB, not
    a permanent public URL. A presigned URL is generated on demand
    whenever an image is actually referenced (e.g. during PM strategy
    generation), since these manuals may be confidential OEM documents
    and shouldn't be permanently public on the open internet.
  - NEW: each text chunk is additionally scanned (regex, no model call)
    for OEM-style parts-list rows ("Lv Item Component-no Description
    Qty Un", e.g. "2 0001 341047 02 RAIL-TOP-RH 1.000 EA") and any
    (item_no, part_no) pairs found are captured in the chunk's content
    prefix as "bom_items:...". This costs nothing at ingest time and
    is a no-op (empty field) for manuals that don't use this table
    format. It isn't consumed by anything yet -- it's stored so a
    future item-number-precise image-linking feature doesn't require
    re-ingesting every manual to backfill it. See extract_bom_items()
    docstring for the matching caveat. Note this regex is matched
    against extracted text, so on OCR'd pages its hit rate depends on
    OCR quality -- garbled OCR output on a low-quality scan simply
    won't match the row shape, which is a safe no-op, not an error.

REQUIRES A DB MIGRATION before this will work -- see the
equipment_manual_images table DDL in the module docstring below the
imports. This has NOT been run automatically; it must be applied to
your database before deploying this file.

-- Migration required:
-- CREATE TABLE equipment_manual_images (
--     id UUID PRIMARY KEY,
--     company_id UUID NOT NULL REFERENCES companies(id),
--     equipment_id UUID NOT NULL REFERENCES equipment(id) ON DELETE CASCADE,
--     doc_id UUID NOT NULL,
--     s3_key TEXT NOT NULL,
--     page_number INT,
--     context_text TEXT,
--     created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
--     updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
-- );
-- CREATE INDEX equipment_manual_images_equipment_id ON equipment_manual_images(equipment_id);
-- CREATE INDEX equipment_manual_images_company_id ON equipment_manual_images(company_id);
"""

import io
import os
import re
import uuid
import hashlib
import logging
import asyncio
import requests

import boto3
import psycopg2.extras

from app.utils.db import get_db_connection
from app.services.embeddings import get_embedding

log = logging.getLogger(__name__)

CHUNK_SIZE         = 500   # approximate words per chunk
CHUNK_OVERLAP      = 50    # words overlap between chunks
EMBED_CONCURRENCY  = 10    # max simultaneous embedding calls in flight

S3_BUCKET  = os.environ.get("S3_BUCKET", "squaremethods")
AWS_REGION = os.environ.get("AWS_REGION", "ca-central-1")

# Master switch for the PDF image-extraction + S3-upload step. Off for now
# to keep ingestion fast -- extracting every embedded image and uploading
# each one to S3 synchronously was the slow part of ingest, not the text
# chunking/embedding. When you're ready to bring images back:
#   1. Flip this to True.
#   2. That's it -- extract_images_from_pdf(), save_manual_images(), and
#      clear_document_images() are all still here and unchanged, just not
#      being called from _run_ingest() while this is False.
# If ingestion needs to stay fast even with images back on, the next step
# would be moving the S3 uploads to their own async/background step rather
# than inline in the main ingest job -- not needed until this flag is on.
IMAGE_EXTRACTION_ENABLED = False

# Heuristic filter for skipping small logos/icons/watermarks extracted
# from a PDF. Tune based on what your actual manuals contain -- OEM
# section-divider logos in the manuals we've tested were consistently
# well under this size, while real component photos/diagrams were not.
MIN_IMAGE_BYTES = 8000

_EXTENSION_CONTENT_TYPES = {
    ".png":  "image/png",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif":  "image/gif",
    ".bmp":  "image/bmp",
    ".tiff": "image/tiff",
    ".webp": "image/webp",
}


# ── File URL to stable UUID ───────────────────────────────────────────────────

def url_to_uuid(file_url: str) -> str:
    """
    Generate a stable UUID from the file URL.
    Same URL always produces the same UUID so we can reliably
    identify and delete chunks for a specific document.
    """
    return str(uuid.UUID(hashlib.md5(file_url.encode()).hexdigest()))


# ── OCR fallback (scanned / image-only PDFs) ──────────────────────────────────
#
# Some manufacturers submit manuals as flat scans -- every page is a raster
# image with no embedded text layer at all, so pypdf's extract_text()
# returns "" for every page. The fix is page-level, not whole-document:
# each page's native extract_text() result is checked against
# OCR_MIN_TEXT_CHARS, and only pages that come back empty/near-empty are
# rendered to an image (via PyMuPDF) and OCR'd (via pytesseract/tesseract).
# Manuals that mix real text pages with scanned drawing pages get native
# extraction on the pages that have it and OCR only on the pages that need
# it, instead of paying OCR's cost (and quality loss vs. a real text
# layer) on every page unconditionally.
#
# See the module docstring's "DEPLOYMENT REQUIREMENT" note for the
# tesseract binary/layer this needs on Lambda. _ocr_available() detects a
# missing binary at call time (not import time) so a deployment without
# the layer still imports this module fine and just skips OCR.
#
# OCR is also meaningfully slower than native extraction (roughly ~2
# seconds/page at OCR_DPI=300 on modest hardware, confirmed against a
# real 66-page fully-scanned manual during testing). For a fully-scanned
# manual that's real wall-clock time against the Lambda timeout, hence
# OCR_CONCURRENCY -- pytesseract shells out to the tesseract binary as a
# subprocess, so concurrent OCR calls from a thread pool genuinely run in
# parallel (unlike CPU-bound pure-Python work, which the GIL would
# otherwise serialize).

OCR_ENABLED        = True   # requires the tesseract binary in the runtime -- see module docstring
OCR_DPI             = 300   # render resolution for OCR; higher = better accuracy, slower
OCR_MIN_TEXT_CHARS  = 20    # native-extracted chars below this -> treat the page as scanned
OCR_CONCURRENCY     = 4     # max simultaneous tesseract subprocesses

_ocr_unavailable_logged = False  # log the "OCR unavailable" warning once per process, not once per page


def _ocr_available() -> bool:
    """
    True if OCR's dependencies (pymupdf + pytesseract + the tesseract
    binary itself) are actually importable/callable in this runtime.
    Checked lazily at call time, not at module import time, so a
    deployment that hasn't attached the tesseract Lambda layer yet still
    imports this module and ingests text-layer PDFs fine -- it just runs
    without the OCR fallback until the layer is added, instead of
    crashing the whole module on import.
    """
    global _ocr_unavailable_logged
    if not OCR_ENABLED:
        return False
    try:
        import fitz  # noqa: F401  (PyMuPDF)
        import pytesseract
        pytesseract.get_tesseract_version()
        return True
    except Exception as e:
        if not _ocr_unavailable_logged:
            log.warning(
                f"OCR fallback unavailable (pymupdf/pytesseract/tesseract not "
                f"usable in this runtime: {e}). Scanned/image-only PDF pages "
                f"will be skipped instead of OCR'd until the tesseract layer "
                f"is deployed -- see module docstring."
            )
            _ocr_unavailable_logged = True
        return False


def _ocr_page(fitz_doc, page_index: int) -> str:
    """OCR a single page: render it to an image via PyMuPDF, then run tesseract."""
    import pytesseract
    from PIL import Image

    page  = fitz_doc[page_index]
    pix   = page.get_pixmap(dpi=OCR_DPI)
    image = Image.open(io.BytesIO(pix.tobytes("png")))
    try:
        return pytesseract.image_to_string(image) or ""
    except Exception as e:
        log.warning(f"OCR failed for page {page_index + 1}: {e}")
        return ""


def _ocr_pages_concurrently(file_bytes: bytes, page_indices: list) -> dict:
    """
    OCR a specific set of 0-indexed pages, bounded by OCR_CONCURRENCY
    concurrent tesseract subprocess calls. Returns {page_index: text}.

    Only called with the pages that actually need it (see
    _extract_pdf_page_texts) -- pages with a usable native text layer
    never reach here, so a manual that's mostly real text with a few
    scanned drawing pages only OCRs those few pages.
    """
    import fitz
    from concurrent.futures import ThreadPoolExecutor, as_completed

    results  = {}
    fitz_doc = fitz.open(stream=file_bytes, filetype="pdf")
    try:
        with ThreadPoolExecutor(max_workers=OCR_CONCURRENCY) as executor:
            futures = {
                executor.submit(_ocr_page, fitz_doc, i): i
                for i in page_indices
            }
            for future in as_completed(futures):
                i = futures[future]
                try:
                    results[i] = future.result()
                except Exception as e:
                    log.warning(f"OCR task failed for page {i + 1}: {e}")
                    results[i] = ""
    finally:
        fitz_doc.close()
    return results


def _extract_pdf_page_texts(file_bytes: bytes) -> list:
    """
    Returns a list of per-page text, one entry per page, in page order.
    Native pypdf extraction is used first; any page whose native text
    comes back under OCR_MIN_TEXT_CHARS is OCR'd instead, if OCR is
    available (see _ocr_available()). Pages with neither a usable native
    text layer nor OCR available come back as "".

    Shared by extract_text_from_pdf() (joins the non-empty pages into the
    document's full text) and extract_images_from_pdf() (uses each page's
    text as that image's context_text), so both get OCR'd text for
    scanned pages instead of extract_images_from_pdf silently falling
    back to native-only extraction and getting blank context_text on
    pages extract_text_from_pdf had to OCR.
    """
    import pypdf
    reader = pypdf.PdfReader(io.BytesIO(file_bytes))

    native_texts = [(page.extract_text() or "").strip() for page in reader.pages]
    needs_ocr    = [i for i, t in enumerate(native_texts) if len(t) < OCR_MIN_TEXT_CHARS]

    if not needs_ocr:
        return native_texts

    if not _ocr_available():
        log.info(
            f"{len(needs_ocr)}/{len(native_texts)} page(s) have no usable "
            f"text layer and OCR is unavailable in this runtime; they will "
            f"be skipped."
        )
        return native_texts

    log.info(
        f"{len(needs_ocr)}/{len(native_texts)} page(s) have no usable text "
        f"layer -- OCR'ing them (concurrency={OCR_CONCURRENCY}, dpi={OCR_DPI})."
    )
    ocr_results = _ocr_pages_concurrently(file_bytes, needs_ocr)

    page_texts = list(native_texts)
    ocr_hits   = 0
    for i, text in ocr_results.items():
        text = text.strip()
        if text:
            ocr_hits += 1
        page_texts[i] = text
    log.info(f"OCR recovered text on {ocr_hits}/{len(needs_ocr)} scanned page(s).")
    return page_texts


# ── Text extraction ───────────────────────────────────────────────────────────

def extract_text_from_pdf(file_bytes: bytes) -> str:
    try:
        page_texts = _extract_pdf_page_texts(file_bytes)
        return "\n\n".join(t for t in page_texts if t)
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


# ── Image extraction (PDF only for now) ──────────────────────────────────────

def extract_images_from_pdf(file_bytes: bytes) -> list:
    """
    Extract embedded images from a PDF, paired with the extracted text
    of the page they appear on (used as coarse "context_text" for later
    keyword matching against a component name).

    Returns a list of dicts: {page_number, image_bytes, extension, context_text}
    Skips images smaller than MIN_IMAGE_BYTES (logos/icons/watermarks).

    Page text now comes from _extract_pdf_page_texts(), the same
    native-or-OCR extraction extract_text_from_pdf() uses, so a scanned
    page's images still get real context_text instead of "" -- note this
    means the PDF gets parsed twice (once here via pypdf for the image
    objects themselves, once inside _extract_pdf_page_texts for text/OCR).
    That redundancy is harmless correctness-wise and not worth optimizing
    away while this whole function is gated behind
    IMAGE_EXTRACTION_ENABLED = False; revisit if/when it's turned back on.

    NOTE: DOCX image extraction is not implemented yet -- extract_text_from_docx
    only pulls paragraph text. If DOCX manuals with embedded images become
    common, this needs a matching extract_images_from_docx() using
    doc.part.rels to walk embedded image parts.
    """
    import pypdf

    reader     = pypdf.PdfReader(io.BytesIO(file_bytes))
    page_texts = _extract_pdf_page_texts(file_bytes)
    images     = []

    for page_number, page in enumerate(reader.pages, start=1):
        page_text = page_texts[page_number - 1] if page_number - 1 < len(page_texts) else ""

        try:
            page_images = list(page.images)
        except Exception as e:
            log.warning(f"Failed to read images on page {page_number}: {e}")
            continue

        for img in page_images:
            try:
                image_bytes = img.data
            except Exception as e:
                log.warning(f"Failed to read image data on page {page_number}: {e}")
                continue

            if len(image_bytes) < MIN_IMAGE_BYTES:
                continue  # likely a logo/icon/watermark, not a component reference

            _, ext = os.path.splitext(getattr(img, "name", "") or "")
            ext = ext.lower() if ext else ".png"

            images.append({
                "page_number":  page_number,
                "image_bytes":  image_bytes,
                "extension":    ext,
                "context_text": page_text.strip(),
            })

    log.info(f"Extracted {len(images)} candidate images (>{MIN_IMAGE_BYTES} bytes) from PDF.")
    return images


# ── BOM item extraction (NEW) ──────────────────────────────────────────────────
#
# Many OEM electronic parts manuals (this one included) lay out components
# as fixed-format rows:
#     "2 0001 341047 02 RAIL-TOP-RH 1.000 EA"
#      Lv Item PartNo  Rev Description         Qty  Un
# The Item column (e.g. "0001") is a small integer that, in a meaningful
# fraction of these manuals, also matches the numbered balloon callouts on
# the facing exploded-view drawing -- but that's NOT being relied on here.
# All this does is opportunistically capture (item_no, part_no) pairs
# wherever this row shape appears in the extracted text, so they're not
# lost. It does nothing else: no image is linked, no balloon is detected,
# no assumption is made about what the item number means for any given
# manual. For manuals that don't use this table format, extract_bom_items
# simply returns an empty list -- safe to call unconditionally. On OCR'd
# (scanned) pages, matching also depends on OCR quality -- garbled OCR
# output simply won't match the row shape, which is a safe no-op.

_BOM_LINE_RE = re.compile(
    r"^\d+\s+(?P<item>\d{4})\s+(?P<part>[\w./\"-]+)\s+(?P<rest>.+)$"
)
_BOM_TRAILING_QTY_RE = re.compile(
    r"^(?P<desc>.*?)\s+(?P<qty>[\d.]+)\s+(?P<unit>EA|IN|FT)$"
)


def extract_bom_items(text: str) -> list:
    """
    Best-effort, regex-only extraction of (item_no, part_no, description)
    triples from OEM-style parts-list rows in already-extracted page text.
    No OCR, no model call -- just string matching over text we already have.

    Returns a list of dicts: {item_no, part_no, description}. Empty list
    if the text doesn't contain rows in this shape (the common case for
    most manuals) -- callers don't need to check the manual's format
    before calling this.
    """
    items = []
    for line in text.splitlines():
        line = line.strip()
        m = _BOM_LINE_RE.match(line)
        if not m:
            continue

        item_no = m.group("item")
        if item_no == "0000":
            continue  # assembly header / kit-reference row, not a numbered part

        part_no = m.group("part")
        rest    = m.group("rest").strip()

        m2 = _BOM_TRAILING_QTY_RE.match(rest)
        description = m2.group("desc").strip() if m2 else rest

        # Strip a leading 2-digit revision code before the real description,
        # e.g. "02 RAIL-TOP-RH" -> "RAIL-TOP-RH". Cosmetic only -- this
        # field isn't persisted anywhere yet (only item_no/part_no are
        # encoded into the chunk prefix), but keep it clean for whenever
        # it is.
        m3 = re.match(r"^\d{2}\s+(.*)$", description)
        if m3:
            description = m3.group(1)

        items.append({
            "item_no":     item_no,
            "part_no":     part_no,
            "description": description[:120],  # defensive cap
        })

    return items


def _encode_bom_items(bom_items: list) -> str:
    """
    Compact "item=part,item=part" encoding for embedding in the chunk's
    content prefix. Description is intentionally omitted here -- it's
    already present in the chunk's own text for embedding/full-text
    search, so repeating it in the prefix would just be redundant bytes.
    """
    return ",".join(f"{b['item_no']}={b['part_no']}" for b in bom_items)


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


# ── DB helpers: text chunks ───────────────────────────────────────────────────

def clear_document_chunks(conn, file_url: str, company_id: str):
    """
    Remove all chunks for a specific document identified by file URL.
    Matches on the doc_id prefix embedded in the content column.
    Called when a document is deleted or re-uploaded.
    Does not affect other documents on the same node.
    """
    doc_uuid = url_to_uuid(file_url)
    with conn.cursor() as cur:
        cur.execute("""
            DELETE FROM knowledge_embeddings
            WHERE company_id = %s::uuid
            AND source_type = 'manual'
            AND content LIKE %s
        """, (company_id, f"%doc_id:{doc_uuid}%"))
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


# ── DB + S3 helpers: manual images ────────────────────────────────────────────

def _s3_client():
    return boto3.client("s3", region_name=AWS_REGION)


def clear_document_images(conn, file_url: str, company_id: str):
    """
    Remove all manual images for a specific document: deletes both the
    S3 objects and the DB rows. Mirrors clear_document_chunks() so a
    re-uploaded document doesn't accumulate duplicate images over time.
    """
    doc_uuid = url_to_uuid(file_url)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT s3_key FROM equipment_manual_images
            WHERE company_id = %s::uuid AND doc_id = %s::uuid
        """, (company_id, doc_uuid))
        keys = [row["s3_key"] for row in cur.fetchall()]

    if keys:
        s3 = _s3_client()
        # S3 delete_objects takes up to 1000 keys per call
        for i in range(0, len(keys), 1000):
            batch = keys[i:i + 1000]
            s3.delete_objects(
                Bucket=S3_BUCKET,
                Delete={"Objects": [{"Key": k} for k in batch]},
            )

    with conn.cursor() as cur:
        cur.execute("""
            DELETE FROM equipment_manual_images
            WHERE company_id = %s::uuid AND doc_id = %s::uuid
        """, (company_id, doc_uuid))
        deleted = cur.rowcount
    conn.commit()
    log.info(f"Removed {deleted} manual images (and their S3 objects) for document {file_url}")
    return deleted


def clear_node_images(conn, equipment_id: str, company_id: str):
    """
    Remove ALL manual images for a node: S3 objects + DB rows.
    Mirrors clear_node_chunks(). Called when a node is deleted.
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT s3_key FROM equipment_manual_images
            WHERE company_id = %s::uuid AND equipment_id = %s::uuid
        """, (company_id, equipment_id))
        keys = [row["s3_key"] for row in cur.fetchall()]

    if keys:
        s3 = _s3_client()
        for i in range(0, len(keys), 1000):
            batch = keys[i:i + 1000]
            s3.delete_objects(
                Bucket=S3_BUCKET,
                Delete={"Objects": [{"Key": k} for k in batch]},
            )

    with conn.cursor() as cur:
        cur.execute("""
            DELETE FROM equipment_manual_images
            WHERE company_id = %s::uuid AND equipment_id = %s::uuid
        """, (company_id, equipment_id))
        deleted = cur.rowcount
    conn.commit()
    log.info(f"Removed {deleted} manual images (and their S3 objects) for node {equipment_id}")
    return deleted


def save_manual_images(conn, images: list, file_url: str, equipment_id: str, company_id: str):
    """
    Upload extracted images to S3 (privately -- key stored, not a
    public URL) and record one row per image in equipment_manual_images
    for later keyword lookup during PM strategy generation.
    """
    if not images:
        return

    doc_uuid = url_to_uuid(file_url)
    s3 = _s3_client()

    rows = []
    for idx, img in enumerate(images):
        ext = img["extension"] if img["extension"] in _EXTENSION_CONTENT_TYPES else ".png"
        content_type = _EXTENSION_CONTENT_TYPES.get(ext, "application/octet-stream")

        s3_key = f"manual-images/{company_id}/{equipment_id}/{doc_uuid}/{img['page_number']}_{idx}{ext}"

        try:
            s3.put_object(
                Bucket=S3_BUCKET,
                Key=s3_key,
                Body=img["image_bytes"],
                ContentType=content_type,
            )
        except Exception as e:
            log.warning(f"Failed to upload manual image {s3_key} to S3: {e}")
            continue

        rows.append((
            str(uuid.uuid4()),
            company_id,
            equipment_id,
            doc_uuid,
            s3_key,
            img["page_number"],
            img["context_text"][:5000],  # cap context text length defensively
        ))

    if not rows:
        return

    sql = """
        INSERT INTO equipment_manual_images
            (id, company_id, equipment_id, doc_id, s3_key, page_number,
             context_text, created_at, updated_at)
        VALUES %s
    """
    psycopg2.extras.execute_values(
        conn.cursor(), sql, rows,
        template="(%s, %s::uuid, %s::uuid, %s::uuid, %s, %s, %s, NOW(), NOW())",
        page_size=100,
    )
    conn.commit()
    log.info(f"Saved {len(rows)} manual images for document {file_url}")


# ── Concurrent embedding ───────────────────────────────────────────────────────

async def _embed_one(executor_loop, semaphore, index: int, content: str):
    """
    Embed a single chunk, bounded by a semaphore so we never have more
    than EMBED_CONCURRENCY calls in flight at once. Runs the blocking
    get_embedding() call in a thread executor so it doesn't block the loop.
    """
    async with semaphore:
        embedding = await executor_loop.run_in_executor(None, get_embedding, content)
        return index, embedding


async def embed_chunks_concurrently(contents: list) -> list:
    """
    Embed all chunk contents concurrently, bounded by EMBED_CONCURRENCY.
    Returns embeddings in the same order as the input contents list.
    """
    loop      = asyncio.get_event_loop()
    semaphore = asyncio.Semaphore(EMBED_CONCURRENCY)

    tasks = [
        _embed_one(loop, semaphore, i, content)
        for i, content in enumerate(contents)
    ]

    results = await asyncio.gather(*tasks)
    results.sort(key=lambda r: r[0])
    return [embedding for _, embedding in results]


def save_chunks(conn, chunks: list, file_url: str, equipment_id: str, company_id: str):
    """
    Embed and save all chunks. Each chunk gets:
      - a unique source_id derived from (doc_uuid, chunk_index) via uuid5
        so re-ingesting the same file produces the same chunk UUIDs
      - a content prefix with equipment_id, doc_id, and (NEW) any BOM
        item/part-number pairs found in that chunk's text, for node-scoped
        retrieval, document-scoped deletion, and future item-number-precise
        lookups

    The content prefix is always exactly three "key:value | " segments
    followed by the chunk text -- "bom_items" is included even when empty
    so the prefix has a fixed, predictable shape for parsing later
    (see fetch_all_manual_chunks in generate_pm_strategy.py).

    Embeddings are generated concurrently (bounded by EMBED_CONCURRENCY)
    instead of one at a time, so large documents (50-100+ pages) complete
    well within the Lambda timeout window.
    """
    doc_uuid = url_to_uuid(file_url)

    contents = []
    chunks_with_bom = 0
    for chunk in chunks:
        bom_items = extract_bom_items(chunk)
        if bom_items:
            chunks_with_bom += 1
        bom_field = _encode_bom_items(bom_items)
        contents.append(
            f"equipment_id:{equipment_id} | doc_id:{doc_uuid} | bom_items:{bom_field} | {chunk}"
        )

    if chunks_with_bom:
        log.info(f"Found parts-list rows in {chunks_with_bom}/{len(chunks)} chunks.")

    log.info(f"Embedding {len(contents)} chunks with concurrency={EMBED_CONCURRENCY}...")
    embeddings = asyncio.run(embed_chunks_concurrently(contents))
    log.info(f"Finished embedding {len(embeddings)} chunks.")

    rows = []
    for i, (content, embedding) in enumerate(zip(contents, embeddings)):
        emb_str   = "[" + ",".join(map(str, embedding)) + "]"
        record_id = str(uuid.uuid4())
        chunk_source_id = str(uuid.uuid5(uuid.UUID(doc_uuid), str(i)))

        rows.append((
            record_id,
            company_id,
            "manual",
            chunk_source_id,
            content,
            emb_str,
        ))

    sql = """
        INSERT INTO knowledge_embeddings
            (id, company_id, source_type, source_id, content, embedding,
             created_at, updated_at)
        VALUES %s
    """
    psycopg2.extras.execute_values(
        conn.cursor(), sql, rows,
        template="(%s, %s::uuid, %s, %s::uuid, %s, %s::vector, NOW(), NOW())",
        page_size=100,
    )
    conn.commit()
    log.info(f"Saved {len(rows)} chunks for document {file_url}")


# ── Synchronous core (used by both sync and async entry points) ───────────────

def _run_ingest(file_url: str, equipment_id: str, company_id: str) -> dict:
    """
    The actual ingest work. Shared by the synchronous ingest() function
    and the async run_ingest_job() background runner.
    """
    log.info(f"Ingesting document for equipment {equipment_id}: {file_url}")

    filename   = file_url.split("/")[-1].split("?")[0]
    response   = requests.get(file_url, timeout=30)
    response.raise_for_status()
    file_bytes = response.content
    log.info(f"Downloaded {len(file_bytes)} bytes from {filename}")

    text = extract_text(file_bytes, filename)
    if not text.strip():
        if filename.lower().endswith(".pdf") and not _ocr_available():
            raise ValueError(
                "No text could be extracted from the document. This PDF "
                "appears to be scanned (no embedded text layer) and OCR "
                "is not available in this runtime -- see OCR_ENABLED / "
                "_ocr_available() in the module docstring for the "
                "tesseract layer this needs on Lambda."
            )
        raise ValueError("No text could be extracted from the document.")
    log.info(f"Extracted {len(text.split())} words.")

    chunks = chunk_text(text)
    log.info(f"Created {len(chunks)} chunks.")

    # Image extraction is PDF-only, and gated behind IMAGE_EXTRACTION_ENABLED
    # (currently False -- see module docstring). Skipping this entirely
    # while disabled is the whole point: no per-page image pulls, no
    # per-image S3 uploads, so ingest is just download -> text -> chunk ->
    # embed. See extract_images_from_pdf's docstring for the DOCX gap
    # whenever this is turned back on.
    images = []
    if IMAGE_EXTRACTION_ENABLED and filename.lower().endswith(".pdf"):
        try:
            images = extract_images_from_pdf(file_bytes)
        except Exception as e:
            # Image extraction failing should never block text ingestion --
            # log and continue with an empty image set.
            log.error(f"Image extraction failed for {filename}, continuing without images: {e}")
            images = []

    conn = get_db_connection()
    try:
        clear_document_chunks(conn, file_url, company_id)
        save_chunks(conn, chunks, file_url, equipment_id, company_id)

        if IMAGE_EXTRACTION_ENABLED:
            clear_document_images(conn, file_url, company_id)
            save_manual_images(conn, images, file_url, equipment_id, company_id)
    finally:
        conn.close()

    return {
        "equipment_id": equipment_id,
        "company_id":   company_id,
        "source_type":  "manual",
        "filename":     filename,
        "words":        len(text.split()),
        "chunks":       len(chunks),
        "images":       len(images),
        "status":       "ingested"
    }


# ── Main functions ────────────────────────────────────────────────────────────

def ingest(file_url: str, equipment_id: str, company_id: str) -> dict:
    """
    Synchronous ingest, kept for any direct/internal callers.
    Most traffic should go through run_ingest_job via SQS instead.
    """
    return _run_ingest(file_url, equipment_id, company_id)


def run_ingest_job(job_id: str, file_url: str, equipment_id: str, company_id: str):
    """
    Called when Lambda is triggered by SQS for a document_ingest job.
    Runs the ingest, then updates the jobs table with the result.
    """
    try:
        result = _run_ingest(file_url, equipment_id, company_id)

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

        log.info(f"DOCUMENT INGEST JOB COMPLETE [{job_id}]")

    except Exception as e:
        log.error(f"DOCUMENT INGEST JOB ERROR [{job_id}]: {str(e)}")
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


def delete_document(file_url: str, company_id: str) -> dict:
    """
    Remove all chunks AND images for a specific document.
    Called by Benjamin when a document is deleted from a node.
    """
    conn = get_db_connection()
    try:
        deleted_chunks = clear_document_chunks(conn, file_url, company_id)
        deleted_images = clear_document_images(conn, file_url, company_id)
    finally:
        conn.close()
    return {
        "file_url":        file_url,
        "chunks_deleted":  deleted_chunks,
        "images_deleted":  deleted_images,
        "status":          "deleted",
    }


def delete_node(equipment_id: str, company_id: str) -> dict:
    """
    Remove all manual chunks AND images for a node.
    Called by Benjamin when an equipment or component node is deleted.
    """
    conn = get_db_connection()
    try:
        deleted_chunks = clear_node_chunks(conn, equipment_id, company_id)
        deleted_images = clear_node_images(conn, equipment_id, company_id)
    finally:
        conn.close()
    return {
        "equipment_id":    equipment_id,
        "chunks_deleted":  deleted_chunks,
        "images_deleted":  deleted_images,
        "status":          "deleted",
    }