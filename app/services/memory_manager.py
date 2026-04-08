from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.models import (
    ImageAlias,
    ImageAsset,
    ImageUnderstanding,
    DocumentAsset,
    DocumentUnderstanding,
    MemoryItem,
    ResolutionLog,
    Turn,
    WorkingMemory,
)
from app.services.resolvers import detect_reference_expressions
from app.utils import compact_text


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

    def serialize_working_memory(self, wm: WorkingMemory) -> dict[str, Any]:
        return {
            'user_goal': wm.user_goal or '',
            'current_task': wm.current_task or '',
            'current_focus': wm.current_focus or {},
            'active_image_ids': [str(x) for x in (wm.active_image_ids or [])],
            'constraints': list(wm.constraints or []),
            'decisions': list(wm.decisions or []),
            'unresolved_questions': list(wm.unresolved_questions or []),
            'summary_buffer': wm.summary_buffer or '',
        }

    def normalize_working_memory(self, value: dict[str, Any]) -> dict[str, Any]:
        def dedupe(items: list[str], limit: int) -> list[str]:
            out: list[str] = []
            seen = set()
            for item in items:
                norm = compact_text((item or '').strip(), 280)
                if not norm:
                    continue
                key = norm.casefold()
                if key in seen:
                    continue
                seen.add(key)
                out.append(norm)
                if len(out) >= limit:
                    break
            return out

        current_focus = value.get('current_focus') or {}
        if not isinstance(current_focus, dict):
            current_focus = {'value': str(current_focus)}

        active_image_ids = []
        for item in value.get('active_image_ids', []) or []:
            text = str(item).strip()
            if text and text not in active_image_ids:
                active_image_ids.append(text)
            if len(active_image_ids) >= 4:
                break

        return {
            'user_goal': compact_text(value.get('user_goal', ''), 280),
            'current_task': compact_text(value.get('current_task', ''), 280),
            'current_focus': current_focus,
            'active_image_ids': active_image_ids,
            'constraints': dedupe(list(value.get('constraints', []) or []), 8),
            'decisions': dedupe(list(value.get('decisions', []) or []), 12),
            'unresolved_questions': dedupe(list(value.get('unresolved_questions', []) or []), 8),
            'summary_buffer': compact_text(value.get('summary_buffer', ''), 480),
        }

    def apply_working_memory(self, db: Session, conversation_id: UUID, value: dict[str, Any]) -> WorkingMemory:
        wm = self.get_or_create_working_memory(db, conversation_id)
        normalized = self.normalize_working_memory(value)
        wm.user_goal = normalized['user_goal']
        wm.current_task = normalized['current_task']
        wm.current_focus = normalized['current_focus']
        parsed_active_ids = []
        for item in normalized['active_image_ids']:
            try:
                parsed_active_ids.append(UUID(item))
            except Exception:
                continue
        wm.active_image_ids = parsed_active_ids
        wm.constraints = normalized['constraints']
        wm.decisions = normalized['decisions']
        wm.unresolved_questions = normalized['unresolved_questions']
        wm.summary_buffer = normalized['summary_buffer']
        db.flush()
        return wm

    def build_fast_working_memory(
        self,
        previous_memory: dict[str, Any],
        user_text: str,
        current_image_ids: list[str],
        current_document_ids: list[str],
        file_placeholders: list[dict[str, Any]],
    ) -> dict[str, Any]:
        lower = (user_text or '').lower()
        constraints = list(previous_memory.get('constraints', []) or [])
        if 'không có các kí tự đặc biệt' in lower or 'không có ký tự đặc biệt' in lower:
            constraints.append('Không dùng các ký tự đặc biệt mà user đã cấm')
        if 'không có dấu *' in lower or 'không có các dấu *' in lower or 'không có dấu sao' in lower:
            constraints.append('Không dùng dấu sao trong câu trả lời')
        if 'dài hơn' in lower or 'chi tiết hơn' in lower:
            constraints.append('Ưu tiên trả lời dài và chi tiết')
        constraints.append('Ngôn ngữ phản hồi là tiếng Việt')

        unresolved = list(previous_memory.get('unresolved_questions', []) or [])
        if user_text.strip():
            unresolved.append(compact_text(user_text, 240))

        focus = {
            'focus_type': 'image_or_file' if (current_image_ids or current_document_ids) else 'text',
            'primary_image_ids': current_image_ids[:2],
            'primary_document_ids': current_document_ids[:2],
            'new_file_count': len(current_image_ids) + len(current_document_ids),
            'reference_expressions': detect_reference_expressions(user_text),
            'current_uploads': file_placeholders,
        }

        combined_summary = ' | '.join(
            part for part in [previous_memory.get('summary_buffer', ''), compact_text(user_text, 160)] if part
        )

        fast = {
            'user_goal': previous_memory.get('user_goal') or compact_text(user_text, 200),
            'current_task': compact_text(user_text or 'Phân tích yêu cầu hiện tại', 240),
            'current_focus': focus,
            'active_image_ids': current_image_ids or list(previous_memory.get('active_image_ids', []) or [])[:2],
            'constraints': constraints,
            'decisions': list(previous_memory.get('decisions', []) or [])[-10:],
            'unresolved_questions': unresolved,
            'summary_buffer': compact_text(combined_summary, 480),
        }
        return self.normalize_working_memory(fast)

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
            .outerjoin(ImageUnderstanding, ImageUnderstanding.image_id == ImageAsset.id)
            .where(ImageAsset.conversation_id == conversation_id)
            .order_by(desc(ImageAsset.created_at))
            .limit(20)
        ).all()

        documents = db.execute(
            select(DocumentAsset, DocumentUnderstanding)
            .outerjoin(DocumentUnderstanding, DocumentUnderstanding.document_id == DocumentAsset.id)
            .where(DocumentAsset.conversation_id == conversation_id)
            .order_by(desc(DocumentAsset.created_at))
            .limit(20)
        ).all()

        resolutions = db.execute(
            select(ResolutionLog)
            .where(ResolutionLog.conversation_id == conversation_id)
            .order_by(desc(ResolutionLog.created_at))
            .limit(20)
        ).scalars().all()

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
            'working_memory': self.serialize_working_memory(wm),
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
                    'short_caption': iu.short_caption if iu else None,
                    'ocr_text_compressed': iu.ocr_text_compressed if iu else None,
                    'tags': iu.tags if iu else [],
                    'processing_status': (image.metadata_json or {}).get('analysis_status', 'unknown'),
                    'created_at': image.created_at.isoformat() if image.created_at else None,
                }
                for image, iu in images
            ],
            'documents': [
                {
                    'document_id': str(doc.id),
                    'storage_uri': doc.storage_uri,
                    'file_name': doc.file_name,
                    'summary': du.summary if du else None,
                    'tags': du.tags if du else [],
                    'processing_status': (doc.metadata_json or {}).get('analysis_status', 'unknown'),
                    'created_at': doc.created_at.isoformat() if doc.created_at else None,
                }
                for doc, du in documents
            ],
            'resolution_logs': [
                {
                    'id': str(item.id),
                    'turn_id': str(item.turn_id),
                    'raw_expression': item.raw_expression,
                    'resolution_type': item.resolution_type,
                    'resolved_image_id': str(item.resolved_image_id) if item.resolved_image_id else None,
                    'confidence': item.confidence,
                    'resolver_output': item.resolver_output,
                    'created_at': item.created_at.isoformat() if item.created_at else None,
                }
                for item in resolutions
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

    def persist_document_memory(
        self,
        db: Session,
        conversation_id: UUID,
        document_id: UUID,
        content: str,
        embedding: list[float],
        tags: list[str] | None,
        event_time,
    ) -> MemoryItem:
        item = MemoryItem(
            conversation_id=conversation_id,
            memory_type='document_memory',
            source_document_id=document_id,
            content=content,
            embedding=embedding,
            event_time_start=event_time,
            event_time_end=event_time,
            importance_score=0.8,
            recency_score=0.8,
            metadata_json={'tags': tags or []},
        )
        db.add(item)
        db.flush()
        return item

    def add_aliases(self, db: Session, image_id: UUID, turn_id: UUID, aliases: list[str]) -> None:
        seen = set()
        for alias in aliases:
            alias_norm = compact_text(alias.strip(), 180) if alias else ''
            if not alias_norm or alias_norm.casefold() in seen:
                continue
            seen.add(alias_norm.casefold())
            db.add(
                ImageAlias(
                    image_id=image_id,
                    alias_text=alias_norm,
                    alias_type='derived',
                    confidence=0.8,
                    first_seen_turn_id=turn_id,
                )
            )
