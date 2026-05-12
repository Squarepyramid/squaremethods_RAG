from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from mangum import Mangum
from pydantic import BaseModel
from typing import List, Optional
import os

from app.services.bedrock_client import ask_bedrock
from app.services.retrieval import build_context
from app.services.session import create_session, get_session, get_history, save_messages

root_path = os.getenv("ROOT_PATH", "")

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

class ChatRequest(BaseModel):
    query: str
    equipment_path: str
    company_id: str
    user_id: str
    session_id: Optional[str] = None

class ChatResponse(BaseModel):
    answer: str
    session_id: str

@app.get("/")
def root():
    return {
        "message": "SquareMethods RAG API",
        "version": "1.0.0",
        "engine": "Claude 3 Haiku on Amazon Bedrock",
        "endpoints": {
            "health": "/health",
            "chat": "/chat",
            "docs": "/docs"
        }
    }

@app.get("/health", tags=["Health"])
def health_check():
    return {
        "status": "ok",
        "environment": os.getenv("ENV", "lambda"),
        "version": "1.0.0",
        "engine": "bedrock/claude-3-haiku"
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

@app.post("/chat", response_model=ChatResponse, tags=["Chat"])
def chat(request: ChatRequest):
    try:
        equipment_id = request.equipment_path.strip("/").split("/")[-1]

        # Resolve or create session
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

        # Fetch history from DB
        history = get_history(session_id, request.company_id)
        history_text = ""
        if history:
            history_text = "\n".join(
                f"{msg['role'].capitalize()}: {msg['content']}"
                for msg in history
            )
            history_text = f"\n\nConversation History:\n{history_text}\n"

        # Retrieve equipment context
        context = build_context(
            equipment_path=request.equipment_path,
            company_id=request.company_id,
            query=request.query
        )

        # Build prompt
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

        # Save messages to DB
        save_messages(session_id, request.company_id, request.query, answer)

        return ChatResponse(answer=answer, session_id=session_id)

    except HTTPException:
        raise
    except Exception as e:
        print(f"CHAT ERROR: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

handler = Mangum(app, lifespan="off")