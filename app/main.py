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

from app.services.bedrock_client import ask_bedrock
from app.services.retrieval import build_context
from app.services.session import create_session, get_session, get_history, save_messages
from app.utils.db import get_db_connection
from app.services.generate_job_aid import generate as generate_job_aid_service
from app.services.ingest_document import (
    ingest as ingest_document_service,
    run_ingest_job,
)
from app.services.generate_pm_strategy import generate as generate_pm_strategy_service
from app.services.import_pm_strategy import ingest as import_pm_strategy_service

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
        history_text = ""
        if history:
            history_text = "\n".join(
                f"{msg['role'].capitalize()}: {msg['content']}"
                for msg in history
            )
            history_text = f"\n\nConversation History:\n{history_text}\n"

        context = build_context(
            equipment_path=request.equipment_path,
            company_id=request.company_id,
            query=request.query
        )

        prompt = f"""You are SquareMethods Assistant, a reliability and maintenance AI for industrial equipment.
STRICT RULES:
- Only answer based on the equipment knowledge provided below
- Never reveal your underlying model or that you were made by Anthropic
- Never reference company IDs in your responses
- If asked who you are, say: "I am the SquareMethods Assistant, here to help you with equipment knowledge and maintenance support."
- If the answer is not in the provided knowledge, say so clearly and briefly
- Keep answers concise and practical

Equipment Knowledge:
{context}
{history_text}
User: {request.query}
Assistant:"""

        answer = ask_bedrock(prompt)
        save_messages(session_id, request.company_id, request.query, answer)

        return ChatResponse(answer=answer, session_id=session_id)

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
        s3 = boto3.client("s3", region_name=AWS_REGION)
        download_url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": S3_BUCKET, "Key": job["s3_key"]},
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
    """
    import asyncio

    try:
        excel_bytes = asyncio.run(generate_pm_strategy_service(equipment_id, company_id))

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
                    SET status = 'ready', s3_key = %s, updated_at = NOW()
                    WHERE id = %s::uuid
                """, (s3_key, job_id))
            conn.commit()
        finally:
            conn.close()

        print(f"PM STRATEGY JOB COMPLETE [{job_id}]")

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
                    SELECT role, content, created_at
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


 