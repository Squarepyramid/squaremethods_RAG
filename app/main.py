from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from mangum import Mangum
from pydantic import BaseModel
from typing import List
import os

# Import services (fallback if missing)
try:
    from app.services.openai_client import ask_openai
    from app.utils.parameters import get_param
except ImportError:
    def ask_openai(prompt):
        return f"Simulated OpenAI response for: {prompt}"
    def get_param(name):
        return None

root_path = os.getenv("ROOT_PATH", "")

app = FastAPI(
    title="SquareMethods RAG API",
    version="1.0.0",
    description="Chat API with RAG powered by OpenAI + OpenSearch",
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

# ✅ Models
class ChatMessage(BaseModel):
    role: str    # 'user' or 'assistant'
    content: str

class ChatRequest(BaseModel):
    query: str
    equipment_path: str
    company_id: str = None
    history: List[ChatMessage] = []

class ChatResponse(BaseModel):
    answer: str

OPENAPI_SCHEMA = {
    "openapi": "3.0.2",
    "info": {
        "title": "SquareMethods RAG API",
        "version": "1.0.0",
        "description": "Chat API with RAG powered by OpenAI + OpenSearch"
    },
    "servers": [
        {"url": f"https://pv1wat9161.execute-api.us-east-1.amazonaws.com{root_path}"}
    ],
    "paths": {
        "/health": {
            "get": {
                "tags": ["Health"],
                "summary": "Health Check",
                "responses": {
                    "200": {
                        "description": "Successful Response",
                        "content": {"application/json": {"schema": {"type": "object"}}}
                    }
                }
            }
        },
        "/chat": {
            "post": {
                "tags": ["Chat"],
                "summary": "AI Chat with history",
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "query":          {"type": "string"},
                                    "equipment_path": {"type": "string"},
                                    "company_id":     {"type": "string"},
                                    "history": {
                                        "type": "array",
                                        "items": {
                                            "type": "object",
                                            "properties": {
                                                "role":    {"type": "string"},
                                                "content": {"type": "string"}
                                            }
                                        }
                                    }
                                },
                                "required": ["query", "equipment_path"]
                            },
                            "example": {
                                "query": "What is the lubrication interval?",
                                "equipment_path": "acme/plant-a/line-1/pump-001",
                                "company_id": "abc-123",
                                "history": [
                                    {"role": "user",      "content": "What are the safety precautions for this pump?"},
                                    {"role": "assistant", "content": "Always isolate the equipment before inspection and wear PPE."}
                                ]
                            }
                        }
                    }
                },
                "responses": {
                    "200": {
                        "description": "Successful Response",
                        "content": {"application/json": {"schema": {"type": "object", "properties": {"answer": {"type": "string"}}}}}
                    }
                }
            }
        }
    }
}

@app.get("/")
def root():
    return {
        "message": "SquareMethods RAG API",
        "version": "1.0.0",
        "documentation": {
            "swagger_ui":   app.root_path + "/docs",
            "redoc":        app.root_path + "/redoc",
            "openapi_json": app.root_path + "/openapi.json"
        },
        "endpoints": {
            "health": app.root_path + "/health",
            "chat":   app.root_path + "/chat"
        }
    }

@app.get("/openapi.json")
def openapi_json():
    return JSONResponse(content=OPENAPI_SCHEMA)

@app.get("/docs", response_class=HTMLResponse)
def swagger_ui():
    base_path_from_url = ""
    if app.root_path:
        base_path_from_url = app.root_path
    elif window_location_pathname := os.getenv("X_FORWARDED_PREFIX"):
        base_path_from_url = window_location_pathname

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
                url: "{app.root_path}/openapi.json",
                dom_id: '#swagger-ui',
                presets: [SwaggerUIBundle.presets.apis],
                layout: "BaseLayout",
                requestInterceptor: (req) => {{
                    if (req.url.startsWith('/') && !req.url.startsWith('{app.root_path}/')) {{
                        req.url = '{app.root_path}' + req.url;
                    }}
                    return req;
                }}
            }});
        </script>
    </body>
    </html>
    """)

@app.get("/redoc", response_class=HTMLResponse)
def redoc():
    return HTMLResponse(content=f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>SquareMethods API Documentation</title>
        <meta charset="utf-8"/>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <link href="https://fonts.googleapis.com/css?family=Montserrat:300,400,700|Roboto:300,400,700" rel="stylesheet">
        <style>body {{ margin: 0; padding: 0; }}</style>
    </head>
    <body>
        <div id="redoc-container"></div>
        <script src="https://cdn.jsdelivr.net/npm/redoc@2.0.0/bundles/redoc.standalone.js"></script>
        <script>
            Redoc.init("{app.root_path}/openapi.json", {{}}, document.getElementById('redoc-container'))
        </script>
    </body>
    </html>
    """)

@app.get("/health", tags=["Health"])
def health_check():
    return {
        "status": "ok",
        "environment": os.getenv("ENV", "lambda"),
        "version": "1.0.0",
        "root_path_active": app.root_path
    }

# ✅ Chat endpoint — now includes history
@app.post("/chat", response_model=ChatResponse, tags=["Chat"])
def chat(request: ChatRequest):
    try:
        # Build prompt with history context
        history_text = ""
        if request.history:
            recent = request.history[-10:]  # cap at 10 turns
            history_text = "\n".join(
                f"{msg.role.capitalize()}: {msg.content}"
                for msg in recent
            )
            history_text = f"\n\nConversation history:\n{history_text}\n"

        prompt = (
            f"Equipment path: {request.equipment_path}"
            f"{history_text}"
            f"\nUser query: {request.query}"
        )

        answer = ask_openai(prompt)
        return ChatResponse(answer=answer)

    except Exception as e:
        print(f"Error in chat endpoint: {e}")
        raise HTTPException(status_code=500, detail=str(e))

handler = Mangum(app, lifespan="off")