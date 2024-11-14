import os
from fastapi import FastAPI, File, Form, UploadFile, Depends, HTTPException, status, Request, BackgroundTasks, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.security import OAuth2AuthorizationCodeBearer
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from chatbot import Chatbot
from database import create_user, get_user_by_email, insert_chat_message, get_chat_history
from database import insert_video_analysis, get_video_analysis_history, check_user_exists
from dotenv import load_dotenv
import uvicorn
from supabase.client import create_client, Client
import uuid
import logging
from typing import List, Dict, Optional, Any
from datetime import datetime, timedelta
import time
import jwt
from fastapi.responses import Response
from redis_storage import RedisFileStorage
from redis_manager import RedisManager, TaskType, TaskPriority
import asyncio
import secrets
import httpx
from session_config import (
    SESSION_LIFETIME,
    SESSION_REFRESH_THRESHOLD,
    COOKIE_SECURE,
    COOKIE_HTTPONLY,
    COOKIE_SAMESITE,
    SESSION_CLEANUP_INTERVAL
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()

redis_url = os.getenv('REDIS_URL')
if not redis_url:
    raise ValueError("REDIS_URL environment variable is not set")

redis_storage = RedisFileStorage(redis_url)
redis_manager = RedisManager(redis_url)

supabase_url = os.environ.get("SUPABASE_URL")
supabase_key = os.environ.get("SUPABASE_ANON_KEY")

if not supabase_url or not supabase_key:
    raise ValueError("SUPABASE_URL or SUPABASE_ANON_KEY is missing from environment variables")

supabase: Client = create_client(supabase_url, supabase_key)

app = FastAPI(
    title="Video Analysis Chatbot",
    description="A FastAPI application for video analysis with chatbot capabilities",
    version="1.0.0",
    docs_url="/api/docs",
    redoc_url="/api/redoc"
)

# Setup session cleanup background task
@app.on_event("startup")
async def startup_event():
    app.state.start_time = time.time()
    app.state.request_count = 0
    
    async def cleanup_sessions():
        while True:
            await redis_manager.cleanup_expired_sessions()
            await asyncio.sleep(SESSION_CLEANUP_INTERVAL)
    
    asyncio.create_task(cleanup_sessions())

# Configure CORS with specific origin and allow WebSocket
origins = [
    "http://localhost:5173",
    "http://0.0.0.0:5173",
    "http://172.31.196.19:5173",
    "https://172.31.196.19:5173",
    "ws://localhost:5173",
    "ws://0.0.0.0:5173",
    "ws://172.31.196.19:5173",
    "wss://172.31.196.19:5173",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=["*"]
)

# Mount static files and templates
app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/assets", StaticFiles(directory="static/react/assets"), name="assets")
templates = Jinja2Templates(directory="templates")
chatbot = Chatbot()

# WebSocket connection manager
class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}

    async def connect(self, websocket: WebSocket, client_id: str):
        await websocket.accept()
        self.active_connections[client_id] = websocket

    def disconnect(self, client_id: str):
        if client_id in self.active_connections:
            del self.active_connections[client_id]

    async def send_message(self, message: Dict, client_id: str):
        if client_id in self.active_connections:
            await self.active_connections[client_id].send_json(message)

manager = ConnectionManager()

@app.websocket("/ws/{client_id}")
async def websocket_endpoint(websocket: WebSocket, client_id: str):
    try:
        await manager.connect(websocket, client_id)
        
        while True:
            data = await websocket.receive_text()
            # Process the received message
            response = await chatbot.send_message(data)
            
            # Send response back to the client
            await manager.send_message(
                {
                    "type": "message",
                    "content": response,
                    "client_id": client_id
                },
                client_id
            )
    except WebSocketDisconnect:
        manager.disconnect(client_id)
    except Exception as e:
        logger.error(f"WebSocket error: {str(e)}")
        if client_id in manager.active_connections:
            await manager.send_message(
                {
                    "type": "error",
                    "content": "An error occurred processing your message",
                    "client_id": client_id
                },
                client_id
            )

async def get_current_user(request: Request, return_none=False):
    try:
        session_id = request.cookies.get('session_id')
        if not session_id:
            if return_none:
                return None
            raise HTTPException(status_code=401, detail="Not authenticated")
        
        is_valid, session_data = redis_manager.validate_session(session_id)
        if not is_valid or not session_data:
            if return_none:
                return None
            raise HTTPException(status_code=401, detail="Invalid or expired session")

        if not isinstance(session_data, dict) or 'id' not in session_data:
            if return_none:
                return None
            raise HTTPException(status_code=401, detail="Invalid session data")

        # Check if session needs refresh
        current_time = time.time()
        last_refresh = session_data.get('last_refresh', 0)
        if current_time - last_refresh > SESSION_REFRESH_THRESHOLD:
            await redis_manager.refresh_session(session_id)

        return session_data
    except Exception as e:
        logger.error(f"Error in get_current_user: {str(e)}")
        if return_none:
            return None
        raise HTTPException(status_code=401, detail="Authentication error")

@app.post('/login')
async def login_post(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    response: Response = None
):
    try:
        if not redis_manager.check_rate_limit("login", request.client.host):
            raise HTTPException(
                status_code=429,
                detail="Too many login attempts. Please try again later."
            )

        auth_response = supabase.auth.sign_in_with_password({
            "email": email,
            "password": password
        })

        if not auth_response.user:
            logger.error("Login failed: No user in response")
            return JSONResponse(
                status_code=400,
                content={"success": False, "message": "Invalid credentials"}
            )

        user = await get_user_by_email(email)
        if not user:
            user = await create_user(email)

        session_id = secrets.token_urlsafe(32)
        session_data = {
            "id": str(user.get("id")),
            "email": email,
            "last_refresh": time.time()
        }

        if not redis_manager.set_session(session_id, session_data, SESSION_LIFETIME):
            raise HTTPException(
                status_code=500,
                detail="Failed to create session"
            )

        response = JSONResponse(content={"success": True, "message": "Login successful"})
        response.set_cookie(
            key="session_id",
            value=session_id,
            httponly=COOKIE_HTTPONLY,
            secure=COOKIE_SECURE,
            samesite=COOKIE_SAMESITE,
            max_age=SESSION_LIFETIME
        )
        return response

    except Exception as e:
        logger.error(f"Login error: {str(e)}")
        return JSONResponse(
            status_code=400,
            content={"success": False, "message": str(e)}
        )

@app.post('/logout')
async def logout(request: Request):
    session_id = request.cookies.get('session_id')
    if session_id:
        redis_manager.delete_session(session_id)
    
    response = JSONResponse(content={"success": True, "message": "Logout successful"})
    response.delete_cookie(
        key="session_id",
        secure=COOKIE_SECURE,
        httponly=COOKIE_HTTPONLY,
        samesite=COOKIE_SAMESITE
    )
    return response

@app.get("/auth_status")
async def auth_status(request: Request):
    try:
        session_id = request.cookies.get('session_id')
        if not session_id:
            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content={
                    "authenticated": False,
                    "message": "No session found"
                }
            )

        # Refresh session if it exists
        if await redis_manager.refresh_session(session_id):
            user = await get_current_user(request, return_none=True)
            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content={
                    "authenticated": user is not None,
                    "user": user if user else None,
                    "session_status": "active"
                }
            )
        else:
            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content={
                    "authenticated": False,
                    "message": "Session expired or invalid"
                }
            )
    except Exception as e:
        logger.error(f"Auth status error: {str(e)}")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "authenticated": False,
                "error": str(e),
                "session_status": "error"
            }
        )

@app.get("/", response_class=HTMLResponse)
async def serve_react_app(request: Request):
    return FileResponse("static/react/index.html")

@app.get('/login', response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@app.get("/chat_history")
async def get_chat_history_endpoint(request: Request):
    user = await get_current_user(request)
    if not user:
        return JSONResponse(content={"history": []})
    
    cache_key = f"chat_history:{user['id']}"
    cached_history = redis_manager.get_cache(cache_key)
    
    if cached_history:
        logger.info(f"Returning cached chat history for user {user['id']}")
        return JSONResponse(content={"history": cached_history})
        
    history = await get_chat_history(uuid.UUID(user['id']))
    redis_manager.set_cache(cache_key, history)
    return JSONResponse(content={"history": history})

@app.get("/video_analysis_history")
async def get_video_analysis_history_endpoint(request: Request):
    user = await get_current_user(request)
    if not user:
        return JSONResponse(content={"history": []})
    
    cache_key = f"video_history:{user['id']}"
    cached_history = redis_manager.get_cache(cache_key)
    
    if cached_history:
        logger.info(f"Returning cached video history for user {user['id']}")
        return JSONResponse(content={"history": cached_history})
        
    history = await get_video_analysis_history(uuid.UUID(user['id']))
    redis_manager.set_cache(cache_key, history)
    return JSONResponse(content={"history": history})

@app.get("/health")
async def health_check():
    health_status = {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "services": {
            "redis": await redis_manager.health_check(),
            "supabase": {
                "status": "unknown",
                "details": None
            }
        }
    }
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{supabase_url}/health",
                headers={"apikey": supabase_key},
                timeout=5.0
            )
            
            if response.status_code == 200:
                health_status["services"]["supabase"] = {
                    "status": "healthy",
                    "details": response.json()
                }
            else:
                health_status["services"]["supabase"] = {
                    "status": "degraded",
                    "details": {"status_code": response.status_code}
                }
                health_status["status"] = "degraded"
    except Exception as e:
        health_status["services"]["supabase"] = {
            "status": "unhealthy",
            "details": {"error": str(e)}
        }
        health_status["status"] = "unhealthy"
    
    return health_status

@app.get("/metrics")
async def metrics():
    try:
        redis_metrics = await redis_manager.get_metrics()
        
        metrics_data = {
            "timestamp": datetime.utcnow().isoformat(),
            "redis": redis_metrics,
            "app": {
                "uptime": time.time() - app.state.start_time if hasattr(app.state, "start_time") else 0,
                "requests_total": app.state.request_count if hasattr(app.state, "request_count") else 0,
            }
        }
        return metrics_data
    except Exception as e:
        logger.error(f"Error collecting metrics: {str(e)}")
        return {
            "error": "Failed to collect metrics",
            "detail": str(e),
            "timestamp": datetime.utcnow().isoformat()
        }

@app.post("/send_message")
async def send_message(
    request: Request,
    message: str = Form(...),
    videos: List[UploadFile] = File(None)
):
    user = await get_current_user(request)
    
    try:
        if videos:
            for video in videos:
                content = await video.read()
                file_id = str(uuid.uuid4())
                
                # Add video processing task to queue
                task_id = redis_manager.enqueue_task(
                    task_type=TaskType.VIDEO_PROCESSING,
                    payload={
                        "file_id": file_id,
                        "filename": video.filename,
                        "user_id": user["id"]
                    },
                    priority=TaskPriority.HIGH
                )
                
                if await redis_storage.store_file(file_id, content):
                    analysis_text, metadata = await chatbot.analyze_video(
                        file_id=file_id,
                        filename=video.filename
                    )
                    
                    # Add video analysis task to queue
                    analysis_task_id = redis_manager.enqueue_task(
                        task_type=TaskType.VIDEO_ANALYSIS,
                        payload={
                            "file_id": file_id,
                            "analysis": analysis_text,
                            "metadata": metadata,
                            "user_id": user["id"]
                        },
                        priority=TaskPriority.MEDIUM
                    )
                    
                    await insert_video_analysis(
                        user_id=uuid.UUID(user['id']),
                        upload_file_name=video.filename,
                        analysis=analysis_text,
                        video_duration=metadata.get('duration') if metadata else None,
                        video_format=metadata.get('format') if metadata else None
                    )
        
        response_text = await chatbot.send_message(message)
        
        await insert_chat_message(uuid.UUID(user['id']), message, 'user')
        await insert_chat_message(uuid.UUID(user['id']), response_text, 'bot')
        
        cache_key = f"chat_history:{user['id']}"
        redis_manager.invalidate_cache(cache_key)
        
        return JSONResponse(content={"response": response_text})
        
    except Exception as e:
        logger.error(f"Error processing message: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=3000, reload=True)