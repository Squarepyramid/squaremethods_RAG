from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response
from mangum import Mangum
from pydantic import BaseModel
from typing import List, Optional
import os

from app.services.bedrock_client import ask_bedrock
from app.services.retrieval import build_context
from app.services.session import create_session, get_session, get_history, save_messages
from app.utils.db import get_db_connection
from app.services.generate_job_aid import generate as generate_job_aid_service
from app.services.ingest_document import ingest as ingest_document_service
#from app.utils.s3 import upload_image as s3_upload_image
from app.services.generate_pm_strategy import generate as generate_pm_strategy_service
from app.services.import_pm_strategy import ingest as import_pm_strategy_service

root_path = os.getenv("ROOT_PATH", "")

ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}

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
            "health":              "/health",
            "chat":                "/chat",
            "generate_job_aid":    "/job-aids/generate",
            "generate_pm_strategy": "/pm-strategy/generate",
            "import_pm_strategy":  "/pm-strategy/import",
            "upload_image":        "/images/upload",
            "docs":                "/docs"
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

@app.post("/documents/ingest", status_code=201, tags=["Documents"])
def ingest_document(request: IngestDocumentRequest):
    try:
        result = ingest_document_service(
            file_url=request.file_url,
            equipment_id=request.equipment_id,
            company_id=request.company_id,
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        print(f"INGEST DOCUMENT ERROR: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


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


#@app.post("/images/upload", tags=["PM Strategy"])
#async def upload_image(
#    file: UploadFile = File(...),
#):

 
    """
    Upload a step image to S3 and return its public URL.

    Call this before filling the Image column in the PM strategy Excel.
    Paste the returned URL into the Image column for the relevant step row.

    NOTE: Send as multipart/form-data, not JSON.
    Field: file (binary)
    """
#    if file.content_type not in ALLOWED_IMAGE_TYPES:
  #      raise HTTPException(
  #          status_code=400,
   #         detail=f"Unsupported file type: {file.content_type}. Accepted: JPEG, PNG, WEBP, GIF"
    #    )

    #file_bytes = await file.read()

   # if len(file_bytes) > 10 * 1024 * 1024:
   #     raise HTTPException(
   #         status_code=400,
    #        detail=f"File exceeds 10 MB limit ({len(file_bytes) / 1024 / 1024:.1f} MB)"
   #     )

   # try:
   #     url = s3_upload_image(file_bytes, file.content_type, file.filename)
  #  except Exception as e:
  #      print(f"IMAGE UPLOAD ERROR: {str(e)}")
  #      raise HTTPException(status_code=500, detail="Image upload failed")

  #  return {"url": url, "filename": file.filename}



@app.post("/pm-strategy/generate", tags=["PM Strategy"])
async def generate_pm_strategy(
    equipment_id: str = Form(...),
    company_id:   str = Form(...),
):
    """
    Generate a PM strategy Excel file from the ingested equipment manual.

    Pulls all manual chunks for the equipment node, runs nine parallel
    Claude calls (one per PM type: PM1-PM9), and returns a structured
    Excel file for the reviewer to fill gaps and add image URLs.

    The equipment must have at least one document ingested via
    POST /documents/ingest before calling this endpoint.

    NOTE: Send as multipart/form-data, not JSON.
    Fields: equipment_id (string), company_id (string)

    Returns: .xlsx file download
    """
    try:
        excel_bytes = await generate_pm_strategy_service(equipment_id, company_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        print(f"GENERATE PM STRATEGY ERROR: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

    filename = f"pm_strategy_{equipment_id[:8]}.xlsx"

    return Response(
        content    = excel_bytes,
        media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers    = {"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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

    Steps with a URL in the Image column get image_url stored.
    Steps with a blank Image column get image_url = NULL.

    NOTE: Send as multipart/form-data, not JSON.
    Fields:
      file         - the edited .xlsx file (binary)
      equipment_id - UUID of the equipment node
      company_id   - UUID of the company
      created_by   - UUID of the user performing the import

    Returns:
      {
        "equipment_id": "...",
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


handler = Mangum(app, lifespan="off")