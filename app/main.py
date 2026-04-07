from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, ORJSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import iterate_in_threadpool, run_in_threadpool

from app.config import get_settings
from app.db import init_db
from app.schemas import (
    ChatResponse,
    ConversationCreateResponse,
    ConversationDetailResponse,
    ConversationListResponse,
    MemorySnapshotResponse,
)
from app.services.chat_service import ChatService

settings = get_settings()
app = FastAPI(
    title='Multimodal Context Window Demo',
    version='2.1.0',
    default_response_class=ORJSONResponse,
)
service = ChatService()
BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / 'static'
UPLOAD_DIR = Path(settings.upload_dir).resolve()


@app.on_event('startup')
def on_startup() -> None:
    init_db()
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


app.mount('/static', StaticFiles(directory=str(STATIC_DIR)), name='static')
app.mount('/uploads', StaticFiles(directory=str(UPLOAD_DIR)), name='uploads')


@app.get('/')
def root() -> FileResponse:
    return FileResponse(STATIC_DIR / 'index.html')


@app.get('/health')
def healthcheck() -> dict:
    return {'ok': True}


@app.get('/conversations', response_model=ConversationListResponse)
def list_conversations():
    return ConversationListResponse(conversations=service.list_conversations())


@app.get('/conversations/{conversation_id}', response_model=ConversationDetailResponse)
def get_conversation(conversation_id: UUID):
    try:
        detail = service.get_conversation_detail(conversation_id)
        return ConversationDetailResponse(**detail)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post('/conversations', response_model=ConversationCreateResponse)
def create_conversation(
    title: Annotated[str | None, Form()] = None,
):
    convo = service.create_conversation(title=title)
    return ConversationCreateResponse(
        conversation_id=convo.id,
        title=convo.title,
        created_at=convo.created_at,
    )


@app.post('/conversations/{conversation_id}/chat', response_model=ChatResponse)
async def chat(
    conversation_id: UUID,
    text: Annotated[str | None, Form()] = None,
    images: Annotated[list[UploadFile] | None, File()] = None,
):
    uploads = []
    for image in images or []:
        uploads.append(
            {
                'filename': image.filename,
                'mime_type': image.content_type,
                'content': await image.read(),
            }
        )
    try:
        result = await run_in_threadpool(service.process_chat, conversation_id, text, uploads)
        return ChatResponse(**result)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f'Internal error: {exc}') from exc


@app.post('/conversations/{conversation_id}/chat/stream')
async def chat_stream(
    conversation_id: UUID,
    text: Annotated[str | None, Form()] = None,
    images: Annotated[list[UploadFile] | None, File()] = None,
):
    uploads = []
    for image in images or []:
        uploads.append(
            {
                'filename': image.filename,
                'mime_type': image.content_type,
                'content': await image.read(),
            }
        )
    stream = service.process_chat_stream(conversation_id, text, uploads)
    return StreamingResponse(
        iterate_in_threadpool(stream),
        media_type='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no',
        },
    )


@app.get('/conversations/{conversation_id}/memory', response_model=MemorySnapshotResponse)
def memory_snapshot(conversation_id: UUID):
    snapshot = service.memory_snapshot(conversation_id)
    return MemorySnapshotResponse(conversation_id=conversation_id, **snapshot)
