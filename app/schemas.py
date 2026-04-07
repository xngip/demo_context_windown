from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class ConversationCreateResponse(BaseModel):
    conversation_id: UUID
    title: str | None
    created_at: datetime


class ConversationListItem(BaseModel):
    conversation_id: UUID
    title: str | None
    created_at: datetime
    updated_at: datetime | None = None
    last_message: str | None = None
    turn_count: int = 0


class ConversationListResponse(BaseModel):
    conversations: list[ConversationListItem] = Field(default_factory=list)


class ChatResponse(BaseModel):
    conversation_id: UUID
    user_turn_id: UUID
    assistant_turn_id: UUID
    answer: str
    resolved_references: list[dict[str, Any]] = Field(default_factory=list)
    retrieved_items: list[dict[str, Any]] = Field(default_factory=list)
    working_memory: dict[str, Any] = Field(default_factory=dict)
    latency_ms: int = 0
    processing_mode: str = 'standard'
    background_enrichment_started: bool = False


class ConversationMessage(BaseModel):
    turn_id: UUID
    turn_index: int
    role: str
    text: str | None = None
    summary: str | None = None
    created_at: datetime
    images: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ConversationDetailResponse(BaseModel):
    conversation_id: UUID
    title: str | None
    created_at: datetime
    updated_at: datetime | None = None
    messages: list[ConversationMessage] = Field(default_factory=list)


class MemorySnapshotResponse(BaseModel):
    conversation_id: UUID
    recent_turns: list[dict[str, Any]]
    working_memory: dict[str, Any]
    long_term_memory: list[dict[str, Any]]
    images: list[dict[str, Any]]
    resolution_logs: list[dict[str, Any]] = Field(default_factory=list)
