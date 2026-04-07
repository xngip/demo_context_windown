from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.models import ImageAlias, ImageAsset, ImageUnderstanding, MemoryItem, Turn, WorkingMemory


class MemoryManager:
    def get_or_create_working_memory(self, db: Session, conversation_id: UUID) -> WorkingMemory:
        wm = db.get(WorkingMemory, conversation_id)
        if wm:
            return wm
        wm = WorkingMemory(
            conversation_id=conversation_id,
            user_goal='',
            current_task='',
            current_focus={},
            active_image_ids=[],
            constraints=[],
            decisions=[],
            unresolved_questions=[],
            summary_buffer='',
        )
        db.add(wm)
        db.flush()
        return wm

    def recent_turns(self, db: Session, conversation_id: UUID, limit: int = 10) -> list[Turn]:
        stmt = (
            select(Turn)
            .where(Turn.conversation_id == conversation_id)
            .order_by(desc(Turn.turn_index))
            .limit(limit)
        )
        return list(reversed(db.execute(stmt).scalars().all()))

    def snapshot(self, db: Session, conversation_id: UUID) -> dict[str, Any]:
        wm = self.get_or_create_working_memory(db, conversation_id)
        recent = self.recent_turns(db, conversation_id, limit=10)
        memories = db.execute(
            select(MemoryItem)
            .where(MemoryItem.conversation_id == conversation_id)
            .order_by(desc(MemoryItem.created_at))
            .limit(20)
        ).scalars().all()
        images = db.execute(
            select(ImageAsset, ImageUnderstanding)
            .join(ImageUnderstanding, ImageUnderstanding.image_id == ImageAsset.id)
            .where(ImageAsset.conversation_id == conversation_id)
            .order_by(desc(ImageAsset.created_at))
            .limit(20)
        ).all()

        return {
            'recent_turns': [
                {
                    'id': str(t.id),
                    'turn_index': t.turn_index,
                    'role': t.role,
                    'text_content': t.text_content,
                    'response_summary': t.response_summary,
                    'created_at': t.created_at.isoformat() if t.created_at else None,
                }
                for t in recent
            ],
            'working_memory': {
                'user_goal': wm.user_goal,
                'current_task': wm.current_task,
                'current_focus': wm.current_focus,
                'active_image_ids': [str(x) for x in (wm.active_image_ids or [])],
                'constraints': wm.constraints,
                'decisions': wm.decisions,
                'unresolved_questions': wm.unresolved_questions,
                'summary_buffer': wm.summary_buffer,
            },
            'long_term_memory': [
                {
                    'id': str(m.id),
                    'memory_type': m.memory_type,
                    'content': m.content,
                    'metadata': m.metadata_json,
                    'created_at': m.created_at.isoformat() if m.created_at else None,
                }
                for m in memories
            ],
            'images': [
                {
                    'image_id': str(image.id),
                    'storage_uri': image.storage_uri,
                    'image_type': image.image_type,
                    'short_caption': iu.short_caption,
                    'ocr_text_compressed': iu.ocr_text_compressed,
                    'tags': iu.tags,
                    'created_at': image.created_at.isoformat() if image.created_at else None,
                }
                for image, iu in images
            ],
        }

    def persist_turn_memory(
        self,
        db: Session,
        conversation_id: UUID,
        turn_id: UUID,
        summary: str,
        embedding: list[float],
    ) -> MemoryItem:
        item = MemoryItem(
            conversation_id=conversation_id,
            memory_type='turn_summary',
            source_turn_id=turn_id,
            content=summary,
            embedding=embedding,
            importance_score=0.7,
            recency_score=0.9,
            metadata_json={'source': 'assistant_summary'},
        )
        db.add(item)
        db.flush()
        return item

    def persist_image_memory(
        self,
        db: Session,
        conversation_id: UUID,
        image_id: UUID,
        content: str,
        embedding: list[float],
        image_type: str | None,
        tags: list[str] | None,
        event_time,
    ) -> MemoryItem:
        item = MemoryItem(
            conversation_id=conversation_id,
            memory_type='image_memory',
            source_image_id=image_id,
            content=content,
            embedding=embedding,
            event_time_start=event_time,
            event_time_end=event_time,
            importance_score=0.8,
            recency_score=0.8,
            metadata_json={'image_type': image_type, 'tags': tags or []},
        )
        db.add(item)
        db.flush()
        return item

    def add_aliases(self, db: Session, image_id: UUID, turn_id: UUID, aliases: list[str]) -> None:
        seen = set()
        for alias in aliases:
            alias_norm = alias.strip()
            if not alias_norm or alias_norm in seen:
                continue
            seen.add(alias_norm)
            db.add(
                ImageAlias(
                    image_id=image_id,
                    alias_text=alias_norm,
                    alias_type='derived',
                    confidence=0.8,
                    first_seen_turn_id=turn_id,
                )
            )
