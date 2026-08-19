from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response
from mangum import Mangum
from pydantic import BaseModel
from typing import List, Optional
import os
import json
import boto3
import psycopg2.extras

from app.services.bedrock_client import ask_bedrock, call_claude, extract_text
from app.services.retrieval import build_context
from app.services.session import create_session, get_session, get_history, save_messages, maybe_summarize
from app.services import tools as chat_tools
from app.utils.db import get_db_connection
from app.services.generate_job_aid import generate as generate_job_aid_service
from app.services.ingest_document import (
    ingest as ingest_document_service,
    run_ingest_job,
)
from app.services.generate_pm_strategy import generate_with_filename as generate_pm_strategy_with_filename_service
from app.services.import_pm_strategy import ingest as import_pm_strategy_service
from app.services.import_pm_strategy import ingest as import_pm_strategy_service
from app.services.import_equipment_master_data import ingest as import_equipment_master_data_service
from app.services.manage_locations import move as move_location_service, insert_level as insert_location_level_service

root_path = os.getenv("ROOT_PATH", "")

ALLOWED_IMAGE_TYPES  = {"image/jpeg", "image/png", "image/webp", "image/gif"}
S3_BUCKET            = os.getenv("S3_BUCKET", "squaremethods")
AWS_REGION           = os.getenv("AWS_REGION", "ca-central-1")
SQS_JOBS_URL         = os.getenv("SQS_PM_STRATEGY_URL", "")

import logging
logging.getLogger().setLevel(logging.INFO)

app = FastAPI(
    title="SquareMethods RAG API",
    version="1.0.0",
    description="Chat API with RAG powered by Claude on Bedrock",
    docs_url=None,
    redoc_url=None,
    root_path=root_path
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Pydantic models ───────────────────────────────────────────────────────────

class ChatMessage(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    query: str
    equipment_path: str
    company_id: str
    user_id: str
    session_id: Optional[str] = None

class ChatResponse(BaseModel):
    answer: str
    session_id: str
    # New: structured citations so the frontend can render job aid links /
    # images instead of relying on the model getting markdown right in
    # prose. Both are additive/optional so existing consumers that only
    # read {answer, session_id} keep working unchanged.
    sources: Optional[dict] = None
    job_aids_created: Optional[List[dict]] = None

class SessionRequest(BaseModel):
    company_id: str
    user_id: str

class GenerateJobAidRequest(BaseModel):
    component_type: str
    equipment_id:   str
    company_id:     str
    created_by:     str

class IngestDocumentRequest(BaseModel):
    file_url:     str
    equipment_id: str
    company_id:   str

class DeleteDocumentRequest(BaseModel):
    file_url:   str
    company_id: str

class DeleteNodeRequest(BaseModel):
    equipment_id: str
    company_id:   str

class ProcedureOut(BaseModel):
    step:        int
    title:       Optional[str]
    instruction: str
    type:        str
    precautions: Optional[List[str]]

class JobAidOut(BaseModel):
    id:                 str
    title:              str
    instruction:        Optional[str]
    category:           Optional[str]
    estimated_duration: Optional[int]
    status:             str
    procedures:         List[ProcedureOut]


# ── Root and health ───────────────────────────────────────────────────────────

@app.get("/")
def root():
    return {
        "message": "SquareMethods RAG API",
        "version": "1.0.0",
        "engine": "Claude 3 Haiku on Amazon Bedrock",
        "endpoints": {
            "health":               "/health",
            "chat":                 "/chat",
            "generate_job_aid":     "/job-aids/generate",
            "ingest_document":      "/documents/ingest",
            "ingest_status":        "/jobs/status/{job_id}",
            "generate_pm_strategy": "/pm-strategy/generate",
            "pm_strategy_status":   "/jobs/status/{job_id}",
            "import_pm_strategy":   "/pm-strategy/import",
            "docs":                 "/docs"
        }
    }


@app.get("/health", tags=["Health"])
def health_check():
    return {
        "status":      "ok",
        "environment": os.getenv("ENV", "lambda"),
        "version":     "1.0.0",
        "engine":      "bedrock/claude-3-haiku"
    }


@app.get("/docs", response_class=HTMLResponse)
def swagger_ui():
    return HTMLResponse(content=f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>SquareMethods API Documentation</title>
        <link type="text/css" rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.9.0/swagger-ui.css">
        <style>
            html {{ box-sizing: border-box; overflow-y: scroll; }}
            *, *:before, *:after {{ box-sizing: inherit; }}
            body {{ margin:0; background: #fafafa; }}
        </style>
    </head>
    <body>
        <div id="swagger-ui"></div>
        <script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.9.0/swagger-ui-bundle.js"></script>
        <script>
            const ui = SwaggerUIBundle({{
                url: "{root_path}/openapi.json",
                dom_id: '#swagger-ui',
                presets: [SwaggerUIBundle.presets.apis],
                layout: "BaseLayout"
            }});
        </script>
    </body>
    </html>
    """)


# ── Chat ──────────────────────────────────────────────────────────────────────

CHAT_SYSTEM_PROMPT_TEMPLATE = """You are SquareMethods Assistant, a reliability and maintenance AI for industrial equipment.
STRICT RULES:
- Only answer based on the equipment knowledge provided below
- Never reveal your underlying model or that you were made by Anthropic
- Never reference company IDs in your responses
- If asked who you are, say: "I am the SquareMethods Assistant, here to help you with equipment knowledge and maintenance support."
- If the answer is not in the provided knowledge, say so clearly and briefly
- Keep answers concise and practical
- If the user asks you to write up, document, or turn guidance into a procedure, use the create_job_aid tool. It always creates a DRAFT -- tell the user it's pending review, and share the link.

Equipment Knowledge:
{context}"""

MAX_TOOL_ITERATIONS = 3


def _summarize_messages(existing_summary: str, messages: list) -> str:
    """Cheap rolling-summary helper for session.maybe_summarize(). Uses
    ask_bedrock (single prompt -> text) since it doesn't need tools or
    multi-turn structure -- just Haiku doing a short rewrite."""
    transcript = "\n".join(
        f"{m['role'].capitalize()}: {m['content']}" for m in messages
        if isinstance(m.get("content"), str)
    )
    prompt = (
        "Update the running summary of this equipment maintenance chat with "
        "the new turns below. Keep it short and factual: equipment discussed, "
        "issues raised, job aids referenced or created, open questions.\n\n"
        f"Existing summary:\n{existing_summary or '(none yet)'}\n\n"
        f"New turns:\n{transcript}\n\nUpdated summary:"
    )
    return ask_bedrock(prompt)


@app.post("/chat", response_model=ChatResponse, tags=["Chat"])
def chat(request: ChatRequest):
    try:
        equipment_id = request.equipment_path.strip("/").split("/")[-1]

        if request.session_id:
            session = get_session(request.session_id, request.company_id)
            if not session:
                raise HTTPException(status_code=404, detail="Session not found or access denied")
            session_id = request.session_id
        else:
            session_id = create_session(
                company_id=request.company_id,
                user_id=request.user_id,
                equipment_path=request.equipment_path,
                equipment_id=equipment_id
            )

        history = get_history(session_id, request.company_id)
        retrieved = build_context(
            equipment_path=request.equipment_path,
            company_id=request.company_id,
            query=request.query
        )

        system_prompt = CHAT_SYSTEM_PROMPT_TEMPLATE.format(context=retrieved["context"])
        if history["summary"]:
            system_prompt += f"\n\nEarlier conversation summary:\n{history['summary']}"

        # Real role-tagged turns instead of flattening history into the
        # prompt string. Only plain-text turns end up in chat_messages
        # (see save_messages below), so every stored message's content is
        # a plain string here -- safe to pass straight through.
        messages = [
            {"role": m["role"], "content": m["content"]}
            for m in history["messages"]
            if m["role"] in ("user", "assistant")
        ]
        messages.append({"role": "user", "content": request.query})

        tool_call_log = []
        response = call_claude(
            messages=messages, system=system_prompt,
            tools=chat_tools.ALL_TOOLS, max_tokens=1500,
        )

        iterations = 0
        while response.get("stop_reason") == "tool_use" and iterations < MAX_TOOL_ITERATIONS:
            iterations += 1
            tool_use_blocks = [b for b in response["content"] if b["type"] == "tool_use"]
            messages.append({"role": "assistant", "content": response["content"]})

            tool_result_blocks = []
            for block in tool_use_blocks:
                result = chat_tools.execute_tool(
                    block["name"], block["input"],
                    company_id=request.company_id,
                    equipment_id=equipment_id,
                    user_id=request.user_id,
                )
                tool_call_log.append({
                    "name": block["name"], "input": block["input"], "result": result,
                })
                tool_result_blocks.append({
                    "type": "tool_result",
                    "tool_use_id": block["id"],
                    "content": json.dumps(result),
                })
            messages.append({"role": "user", "content": tool_result_blocks})

            response = call_claude(
                messages=messages, system=system_prompt,
                tools=chat_tools.ALL_TOOLS, max_tokens=1500,
            )

        answer = extract_text(response)

        # Only the final user query + final assistant text get persisted
        # as conversation turns -- the tool_use/tool_result exchange above
        # is scoped to this one turn's reasoning, not replayed as history.
        # What tool ran (and its result) is preserved in metadata instead.
        save_messages(
            session_id, request.company_id, request.query, answer,
            sources=retrieved["sources"],
            tool_calls=tool_call_log or None,
        )

        try:
            maybe_summarize(session_id, request.company_id, _summarize_messages)
        except Exception as e:
            # Never let summarization block the actual chat response.
            print(f"SUMMARIZE ERROR: {str(e)}")

        job_aids_created = [
            tc["result"] for tc in tool_call_log
            if tc["name"] == "create_job_aid" and tc["result"].get("status") == "created_draft"
        ] or None

        return ChatResponse(
            answer=answer,
            session_id=session_id,
            sources=retrieved["sources"],
            job_aids_created=job_aids_created,
        )

    except HTTPException:
        raise
    except Exception as e:
        print(f"CHAT ERROR: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


# ── Job aid generation ────────────────────────────────────────────────────────

@app.post("/job-aids/generate", response_model=JobAidOut, status_code=201, tags=["Job Aid Generation"])
def generate_job_aid(request: GenerateJobAidRequest):
    try:
        result = generate_job_aid_service(
            component_type=request.component_type,
            equipment_id=request.equipment_id,
            company_id=request.company_id,
            created_by=request.created_by,
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        print(f"GENERATE JOB AID ERROR: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


# ── Documents ─────────────────────────────────────────────────────────────────




@app.post("/documents/ingest", status_code=202, tags=["Documents"])
def ingest_document(request: IngestDocumentRequest):
    """
    Kick off async document ingestion.

    Creates a background job and returns immediately with a job_id.
    Poll GET /documents/ingest/status?equipment_id={equipment_id}&company_id={company_id}
    to see status of all ingest jobs for that equipment node.

    Returns: { job_id: string, status: "pending" }
    """
    filename = request.file_url.split("/")[-1].split("?")[0]

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO pm_strategy_jobs
                    (job_type, equipment_id, company_id, status, payload)
                VALUES
                    ('document_ingest', %s::uuid, %s::uuid, 'pending', %s::jsonb)
                RETURNING id
            """, (
                request.equipment_id,
                request.company_id,
                psycopg2.extras.Json({
                    "file_url": request.file_url,
                    "filename": filename,
                }),
            ))
            job_id = str(cur.fetchone()["id"])
        conn.commit()
    except Exception as e:
        print(f"DOCUMENT INGEST JOB CREATE ERROR: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()

    sqs = boto3.client("sqs", region_name=AWS_REGION)
    sqs.send_message(
        QueueUrl    = SQS_JOBS_URL,
        MessageBody = json.dumps({
            "job_type":     "document_ingest",
            "job_id":       job_id,
            "file_url":     request.file_url,
            "equipment_id": request.equipment_id,
            "company_id":   request.company_id,
        })
    )

    return {"job_id": job_id, "status": "pending"}


@app.get("/documents/ingest/status", tags=["Documents"])
def list_ingest_jobs(equipment_id: str, company_id: str, limit: int = 20):
    """
    List recent document_ingest jobs for an equipment node, most recent first.

    Lets the frontend show ingestion status per document without needing
    to track individual job_ids client-side. The original file_url and
    filename are visible immediately (from payload), even before the job
    completes. Once ready, chunks/words are available from result.
    If a job's status is 'failed', the frontend can surface the error
    and let the user retrigger the ingest via POST /documents/ingest
    using the same file_url.

    Returns:
      {
        "equipment_id": "...",
        "company_id": "...",
        "jobs": [
          {
            "job_id": "...",
            "status": "ready" | "pending" | "failed",
            "file_url": "...",
            "filename": "...",
            "chunks": 142,
            "words": 38210,
            "error": "...",
            "created_at": "...",
            "updated_at": "..."
          },
          ...
        ]
      }
    """
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, status, payload, result, error, created_at, updated_at
                FROM pm_strategy_jobs
                WHERE job_type = 'document_ingest'
                AND equipment_id = %s::uuid
                AND company_id = %s::uuid
                ORDER BY created_at DESC
                LIMIT %s
            """, (equipment_id, company_id, limit))
            rows = cur.fetchall()
    finally:
        conn.close()

    jobs = []
    for row in rows:
        payload = row["payload"] or {}
        result  = row["result"] or {}
        jobs.append({
            "job_id":     str(row["id"]),
            "status":     row["status"],
            "file_url":   payload.get("file_url"),
            "filename":   result.get("filename") or payload.get("filename"),
            "chunks":     result.get("chunks"),
            "words":      result.get("words"),
            "error":      row["error"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
        })

    return {
        "equipment_id": equipment_id,
        "company_id":   company_id,
        "jobs":         jobs,
    }



@app.get("/jobs/status/{job_id}", tags=["Jobs"])
def job_status(job_id: str, company_id: str):
    """
    Generic status check for any async job (pm_strategy, document_ingest).

    Returns:
      { status: "pending" }
      { status: "ready", result: {...} }            -- document_ingest
      { status: "ready", download_url: "https://..." } -- pm_strategy
      { status: "failed", error: "..." }
    """
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT job_type, status, s3_key, result, error
                FROM pm_strategy_jobs
                WHERE id = %s::uuid
                AND company_id = %s::uuid
            """, (job_id, company_id))
            job = cur.fetchone()
    finally:
        conn.close()

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job["status"] == "failed":
        return {"status": "failed", "error": job["error"]}

    if job["status"] != "ready":
        return {"status": "pending"}

    # ready
    if job["job_type"] == "pm_strategy" and job["s3_key"]:
        # The S3 object key is an opaque, collision-safe internal id
        # (pm-strategy/{company_id}/{job_id}.xlsx) -- it was never meant
        # to be human-readable. The actual filename the user sees on
        # download comes from ResponseContentDisposition below, which
        # overrides it without needing to rename/copy the S3 object.
        # Real filename (e.g. "PM_Strategy_Compressor_1_A102_2026-08-05.xlsx")
        # is stored in `result` by run_pm_strategy_job() when the job
        # completes. Older jobs finished before that change won't have
        # it, hence the fallback.
        filename = (job["result"] or {}).get("filename") if job["result"] else None
        if not filename:
            filename = f"PM_Strategy_{job_id}.xlsx"

        s3 = boto3.client("s3", region_name=AWS_REGION)
        download_url = s3.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": S3_BUCKET,
                "Key": job["s3_key"],
                "ResponseContentDisposition": f'attachment; filename="{filename}"',
            },
            ExpiresIn=300
        )
        return {"status": "ready", "download_url": download_url}

    return {"status": "ready", "result": job["result"]}


@app.delete("/documents/delete", tags=["Documents"])
def delete_document(request: DeleteDocumentRequest):
    try:
        from app.services.ingest_document import delete_document as delete_doc_service
        result = delete_doc_service(
            file_url=request.file_url,
            company_id=request.company_id,
        )
        return result
    except Exception as e:
        print(f"DELETE DOCUMENT ERROR: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/nodes/delete", tags=["Documents"])
def delete_node(request: DeleteNodeRequest):
    try:
        from app.services.ingest_document import delete_node as delete_node_service
        result = delete_node_service(
            equipment_id=request.equipment_id,
            company_id=request.company_id,
        )
        return result
    except Exception as e:
        print(f"DELETE NODE ERROR: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


# ── PM Strategy ───────────────────────────────────────────────────────────────

def run_pm_strategy_job(job_id: str, equipment_id: str, company_id: str):
    """
    Called when Lambda is triggered by SQS for a pm_strategy job.
    Generates the PM strategy Excel and uploads it to S3.
    Updates pm_strategy_jobs when done.

    Uses generate_with_filename() (not generate()) so the reviewer-facing
    filename (e.g. "PM_Strategy_Compressor_1_A102_2026-08-05.xlsx") is
    computed here and stored in `result`, for job_status() to hand back
    as the download's Content-Disposition filename. The S3 key itself
    stays job_id-based -- that's just internal storage naming, not what
    the user ever sees.
    """
    import asyncio

    try:
        excel_bytes, filename = asyncio.run(
            generate_pm_strategy_with_filename_service(equipment_id, company_id)
        )

        s3_key = f"pm-strategy/{company_id}/{job_id}.xlsx"
        s3 = boto3.client("s3", region_name=AWS_REGION)
        s3.put_object(
            Bucket=S3_BUCKET,
            Key=s3_key,
            Body=excel_bytes,
            ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE pm_strategy_jobs
                    SET status = 'ready', s3_key = %s, result = %s::jsonb, updated_at = NOW()
                    WHERE id = %s::uuid
                """, (s3_key, psycopg2.extras.Json({"filename": filename}), job_id))
            conn.commit()
        finally:
            conn.close()

        print(f"PM STRATEGY JOB COMPLETE [{job_id}] filename={filename}")

    except Exception as e:
        print(f"PM STRATEGY JOB ERROR [{job_id}]: {str(e)}")
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


@app.post("/pm-strategy/generate", status_code=202, tags=["PM Strategy"])
async def generate_pm_strategy(
    equipment_id: str = Form(...),
    company_id:   str = Form(...),
):
    """
    Kick off async PM strategy generation.

    Creates a background job and returns immediately with a job_id.
    Poll GET /jobs/status/{job_id}?company_id={company_id}
    every 3 seconds until status is 'ready', then use the
    download_url to auto-download the Excel file.

    NOTE: Send as multipart/form-data, not JSON.
    Fields: equipment_id (string), company_id (string)

    Returns: { job_id: string, status: "pending" }
    """
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO pm_strategy_jobs (job_type, equipment_id, company_id, status)
                VALUES ('pm_strategy', %s::uuid, %s::uuid, 'pending')
                RETURNING id
            """, (equipment_id, company_id))
            job_id = str(cur.fetchone()["id"])
        conn.commit()
    except Exception as e:
        print(f"PM STRATEGY JOB CREATE ERROR: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()

    sqs = boto3.client("sqs", region_name=AWS_REGION)
    sqs.send_message(
        QueueUrl    = SQS_JOBS_URL,
        MessageBody = json.dumps({
            "job_type":     "pm_strategy",
            "job_id":       job_id,
            "equipment_id": equipment_id,
            "company_id":   company_id,
        })
    )

    return {"job_id": job_id, "status": "pending"}


@app.post("/pm-strategy/import", status_code=201, tags=["PM Strategy"])
async def import_pm_strategy(
    file:         UploadFile = File(...),
    equipment_id: str        = Form(...),
    company_id:   str        = Form(...),
    created_by:   str        = Form(...),
):
    """
    Import an edited PM strategy Excel file and create job aids.

    Parses the Excel file (output of /pm-strategy/generate after
    reviewer edits). Creates one job aid per PM type block that
    has data rows. Each job aid is linked to the equipment node.

    Steps with a URL in the Image column get image stored.
    Steps with a blank Image column get image = NULL.

    NOTE: Send as multipart/form-data, not JSON.
    Fields:
      file         - the edited .xlsx file (binary)
      equipment_id - UUID of the equipment node
      company_id   - UUID of the company
      created_by   - UUID of the user performing the import

    Returns:
      {
        "equipment_id": "...",
        "equipment_name": "...",
        "job_aids_created": 3,
        "job_aids": [
          { "job_aid_id": "...", "pm_code": "PM2", "pm_name": "Lubrication", "steps": 6 },
          ...
        ]
      }
    """
    if not file.filename.endswith(".xlsx"):
        raise HTTPException(
            status_code=400,
            detail="Only .xlsx files are accepted"
        )

    file_bytes = await file.read()

    try:
        result = import_pm_strategy_service(
            file_bytes   = file_bytes,
            equipment_id = equipment_id,
            company_id   = company_id,
            created_by   = created_by,
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        print(f"IMPORT PM STRATEGY ERROR: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))




# ── Equipment Master Data ─────────────────────────────────────────────────────

@app.post("/equipment/master-data/import", status_code=201, tags=["Equipment Master Data"])
async def import_equipment_master_data(
    file:       UploadFile = File(...),
    company_id: str        = Form(...),
    created_by: str        = Form(...),
):
    """
    Import an Equipment Master Data Excel file and create locations,
    equipment types, and equipment directly in the database.

    Runs synchronously, no SQS job queue -- parses the three tabs
    (Locations, Equipment Types, Equipment), resolves nested location
    hierarchies parent-first, resolves or creates equipment types
    (matched against the company's existing types, then against
    equipment_type_defaults), and inserts equipment with reference_code
    enforced unique per company. Commits as a single transaction; rolls
    back entirely if any row fails.

    NOTE: Send as multipart/form-data, not JSON.
    Fields:
      file       - the .xlsx file (binary), matching the Equipment
                   Master Data template (Locations, Equipment Types,
                   Equipment tabs)
      company_id - UUID of the company
      created_by - UUID of the user performing the import

   Returns:
      {
        "company_id": "...",
        "location_moves_applied": 1,
        "location_moves_skipped": 0,
        "location_moves": { "applied": [...], "skipped": [...] },
        "equipment_created": 8,
        "equipment_updated": 1,
        "equipment_skipped": 0,
        "equipment": {
          "created": [ { "equipment_id": "...", "name": "...", "reference_code": "...", "location_path": "..." }, ... ],
          "updated": [ ... same shape ... ],
          "skipped": [ { "row_number": 2, "name": "...", "reference_code": "...", "reason": "..." }, ... ]
        }
      }
    """
    if not file.filename.endswith(".xlsx"):
        raise HTTPException(
            status_code=400,
            detail="Only .xlsx files are accepted"
        )

    file_bytes = await file.read()

    try:
        result = import_equipment_master_data_service(
            file_bytes = file_bytes,
            company_id = company_id,
            created_by = created_by,
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        print(f"IMPORT EQUIPMENT MASTER DATA ERROR: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))






# ── Sessions ──────────────────────────────────────────────────────────────────

@app.get("/sessions", tags=["Session"])
def list_sessions(company_id: str, user_id: str, limit: int = 20):
    try:
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT s.id, s.equipment_path, s.title, s.created_at, s.updated_at,
                           COUNT(m.id) as message_count,
                           MAX(m.content) FILTER (WHERE m.role = 'user') as last_message
                    FROM chat_sessions s
                    LEFT JOIN chat_messages m ON m.session_id = s.id
                    WHERE s.company_id = %s::uuid
                    AND s.user_id = %s::uuid
                    AND s.deleted_at IS NULL
                    GROUP BY s.id
                    ORDER BY s.updated_at DESC
                    LIMIT %s
                """, (company_id, user_id, limit))
                sessions = cur.fetchall()
        finally:
            conn.close()
        return {"sessions": [dict(s) for s in sessions]}
    except Exception as e:
        print(f"LIST SESSIONS ERROR: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/sessions/{session_id}/messages", tags=["Session"])
def get_session_messages(session_id: str, company_id: str, user_id: str):
    try:
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id FROM chat_sessions
                    WHERE id = %s::uuid
                    AND company_id = %s::uuid
                    AND user_id = %s::uuid
                    AND deleted_at IS NULL
                """, (session_id, company_id, user_id))
                session = cur.fetchone()
                if not session:
                    raise HTTPException(status_code=404, detail="Session not found or access denied")
                cur.execute("""
                    SELECT role, content, metadata, created_at
                    FROM chat_messages
                    WHERE session_id = %s::uuid
                    AND company_id = %s::uuid
                    ORDER BY created_at ASC
                """, (session_id, company_id))
                messages = cur.fetchall()
        finally:
            conn.close()
        return {"session_id": session_id, "messages": [dict(m) for m in messages]}
    except HTTPException:
        raise
    except Exception as e:
        print(f"GET MESSAGES ERROR: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/session/{session_id}/messages", tags=["Session"])
def clear_session_messages(session_id: str, request: SessionRequest):
    try:
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id FROM chat_sessions
                    WHERE id = %s::uuid
                    AND company_id = %s::uuid
                    AND user_id = %s::uuid
                    AND deleted_at IS NULL
                """, (session_id, request.company_id, request.user_id))
                session = cur.fetchone()
                if not session:
                    raise HTTPException(status_code=404, detail="Session not found or access denied")
                cur.execute("""
                    DELETE FROM chat_messages
                    WHERE session_id = %s::uuid
                    AND company_id = %s::uuid
                """, (session_id, request.company_id))
                cur.execute("""
                    UPDATE chat_sessions
                    SET summary = NULL, summary_message_count = 0, updated_at = NOW()
                    WHERE id = %s::uuid
                """, (session_id,))
                conn.commit()
        finally:
            conn.close()
        return {"message": "Chat history cleared", "session_id": session_id}
    except HTTPException:
        raise
    except Exception as e:
        print(f"CLEAR MESSAGES ERROR: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/session/{session_id}", tags=["Session"])
def delete_session(session_id: str, request: SessionRequest):
    try:
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id FROM chat_sessions
                    WHERE id = %s::uuid
                    AND company_id = %s::uuid
                    AND user_id = %s::uuid
                    AND deleted_at IS NULL
                """, (session_id, request.company_id, request.user_id))
                session = cur.fetchone()
                if not session:
                    raise HTTPException(status_code=404, detail="Session not found or access denied")
                cur.execute("""
                    UPDATE chat_sessions SET deleted_at = NOW()
                    WHERE id = %s::uuid
                """, (session_id,))
                conn.commit()
        finally:
            conn.close()
        return {"message": "Session deleted", "session_id": session_id}
    except HTTPException:
        raise
    except Exception as e:
        print(f"DELETE SESSION ERROR: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


# ── Lambda handler ────────────────────────────────────────────────────────────

_mangum_handler = Mangum(app, lifespan="off")


def handler(event, context):
    import asyncio

    try:
        records = event.get("Records", [])
        if records and records[0].get("eventSource") == "aws:sqs":
            body     = json.loads(records[0]["body"])
            job_type = body.get("job_type")

            if job_type == "pm_strategy":
                run_pm_strategy_job(
                    job_id       = body["job_id"],
                    equipment_id = body["equipment_id"],
                    company_id   = body["company_id"],
                )
            elif job_type == "document_ingest":
                run_ingest_job(
                    job_id       = body["job_id"],
                    file_url     = body["file_url"],
                    equipment_id = body["equipment_id"],
                    company_id   = body["company_id"],
                )
            else:
                print(f"UNKNOWN JOB TYPE: {job_type}")

            return {"status": "done"}
    except (KeyError, json.JSONDecodeError) as e:
        print(f"SQS ROUTING ERROR: {e}")

    # Always ensure a usable event loop exists before handing off to Mangum.
    # Background jobs (asyncio.run) can leave the thread without one on
    # Python 3.10+, which raises instead of auto-creating a new loop.
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    return _mangum_handler(event, context)