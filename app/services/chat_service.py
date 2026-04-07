from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Conversation, ImageAsset, ImageUnderstanding, ResolutionLog, Turn, TurnImage
from app.services.gemini_service import GeminiService
from app.services.memory_manager import MemoryManager
from app.services.resolvers import resolve_reference
from app.services.retrieval import RetrievalService
from app.utils import compact_text, guess_mime_type, needs_visual_rehydration, save_upload_bytes, sha256_of_file


class ChatService:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.gemini = GeminiService()
        self.memory = MemoryManager()
        self.retrieval = RetrievalService()

    def _build_image_url(self, storage_uri: str | None) -> str | None:
        if not storage_uri:
            return None

        upload_root = Path(self.settings.upload_dir).resolve()
        file_path = Path(storage_uri).resolve()

        try:
            relative_path = file_path.relative_to(upload_root)
            return f'/uploads/{relative_path.as_posix()}'
        except ValueError:
            normalized = storage_uri.replace('\\', '/').strip('/')
            if not normalized:
                return None
            parts = [part for part in normalized.split('/') if part]
            tail = '/'.join(parts[-2:]) if len(parts) >= 2 else parts[-1]
            return f'/uploads/{tail}'

    def create_conversation(self, db: Session, title: str | None = None) -> Conversation:
        convo = Conversation(title=title or 'Multimodal Context Demo', timezone=self.settings.timezone)
        db.add(convo)
        db.commit()
        db.refresh(convo)
        return convo

    def list_conversations(self, db: Session) -> list[dict[str, Any]]:
        convos = db.execute(
            select(Conversation)
            .order_by(desc(Conversation.updated_at), desc(Conversation.created_at))
        ).scalars().all()

        items: list[dict[str, Any]] = []
        for convo in convos:
            turn_count = db.execute(
                select(func.count(Turn.id)).where(Turn.conversation_id == convo.id)
            ).scalar_one()
            last_turn = db.execute(
                select(Turn)
                .where(Turn.conversation_id == convo.id)
                .order_by(desc(Turn.turn_index))
                .limit(1)
            ).scalar_one_or_none()
            items.append(
                {
                    'conversation_id': convo.id,
                    'title': convo.title,
                    'created_at': convo.created_at,
                    'updated_at': convo.updated_at,
                    'last_message': (last_turn.text_content if last_turn else None),
                    'turn_count': int(turn_count or 0),
                }
            )
        return items

    def get_conversation_detail(self, db: Session, conversation_id: UUID) -> dict[str, Any]:
        convo = db.get(Conversation, conversation_id)
        if not convo:
            raise ValueError('Conversation not found')

        turns = db.execute(
            select(Turn)
            .where(Turn.conversation_id == conversation_id)
            .order_by(Turn.turn_index)
        ).scalars().all()

        turn_ids = [t.id for t in turns]
        image_links = db.execute(
            select(TurnImage, ImageAsset, ImageUnderstanding)
            .join(ImageAsset, TurnImage.image_id == ImageAsset.id)
            .outerjoin(ImageUnderstanding, ImageUnderstanding.image_id == ImageAsset.id)
            .where(TurnImage.turn_id.in_(turn_ids) if turn_ids else False)
            .order_by(TurnImage.position)
        ).all() if turn_ids else []

        images_by_turn: dict[UUID, list[dict[str, Any]]] = {}
        for turn_image, image, understanding in image_links:
            images_by_turn.setdefault(turn_image.turn_id, []).append(
                {
                    'image_id': str(image.id),
                    'url': self._build_image_url(image.storage_uri),
                    'mime_type': image.mime_type,
                    'image_type': image.image_type,
                    'short_caption': understanding.short_caption if understanding else None,
                    'ocr_text_compressed': understanding.ocr_text_compressed if understanding else None,
                }
            )

        messages = [
            {
                'turn_id': t.id,
                'turn_index': t.turn_index,
                'role': t.role,
                'text': t.text_content,
                'summary': t.response_summary,
                'created_at': t.created_at,
                'images': images_by_turn.get(t.id, []),
                'metadata': t.metadata_json or {},
            }
            for t in turns
        ]

        return {
            'conversation_id': convo.id,
            'title': convo.title,
            'created_at': convo.created_at,
            'updated_at': convo.updated_at,
            'messages': messages,
        }

    def process_chat(
        self,
        db: Session,
        conversation_id: UUID,
        user_text: str | None,
        uploads: list[dict[str, Any]],
    ) -> dict[str, Any]:
        conversation = db.get(Conversation, conversation_id)
        if not conversation:
            raise ValueError('Conversation not found')

        max_turn = db.execute(
            select(func.coalesce(func.max(Turn.turn_index), 0)).where(Turn.conversation_id == conversation_id)
        ).scalar_one()
        user_turn = Turn(
            conversation_id=conversation_id,
            turn_index=int(max_turn) + 1,
            role='user',
            text_content=(user_text or '').strip(),
            response_summary=''
        )
        db.add(user_turn)
        db.flush()

        image_summaries: list[dict[str, Any]] = []
        current_image_ids: list[str] = []
        current_image_parts = []

        for idx, upload in enumerate(uploads):
            file_path = save_upload_bytes(self.settings.upload_dir, str(conversation_id), upload['filename'], upload['content'])
            mime_type = upload.get('mime_type') or guess_mime_type(upload['filename'])
            current_image_parts.append(self.gemini.image_part_from_file(file_path, mime_type))

            image = ImageAsset(
                conversation_id=conversation_id,
                uploaded_by_turn_id=user_turn.id,
                storage_uri=file_path,
                mime_type=mime_type,
                checksum=sha256_of_file(file_path),
                image_type='unknown',
            )
            db.add(image)
            db.flush()

            db.add(TurnImage(turn_id=user_turn.id, image_id=image.id, position=idx))

            analysis = self.gemini.analyze_image(file_path, mime_type)
            image.image_type = analysis.get('image_type') or 'other'
            understanding = ImageUnderstanding(
                image_id=image.id,
                short_caption=analysis.get('short_caption'),
                detailed_caption=analysis.get('detailed_caption'),
                ocr_text=analysis.get('ocr_text'),
                ocr_text_compressed=analysis.get('ocr_text_compressed'),
                tags=analysis.get('tags') or [],
                entities=analysis.get('entities') or [],
                visual_summary=analysis.get('visual_summary'),
                dehydrate_payload={
                    'image_id': str(image.id),
                    'short_caption': analysis.get('short_caption'),
                    'ocr_compact': analysis.get('ocr_text_compressed'),
                    'tags': analysis.get('tags') or [],
                    'image_type': analysis.get('image_type'),
                },
                embedding=analysis.get('embedding'),
            )
            db.add(understanding)
            db.flush()

            image_summary = {
                'image_id': str(image.id),
                'short_caption': analysis.get('short_caption'),
                'detailed_caption': analysis.get('detailed_caption'),
                'ocr_text_compressed': analysis.get('ocr_text_compressed'),
                'tags': analysis.get('tags') or [],
                'image_type': analysis.get('image_type'),
            }
            image_summaries.append(image_summary)
            current_image_ids.append(str(image.id))

            self.memory.add_aliases(
                db,
                image.id,
                user_turn.id,
                [
                    analysis.get('short_caption', ''),
                    analysis.get('image_type', ''),
                    *(analysis.get('tags') or []),
                ],
            )
            self.memory.persist_image_memory(
                db=db,
                conversation_id=conversation_id,
                image_id=image.id,
                content=analysis.get('textual_memory', ''),
                embedding=analysis.get('embedding'),
                image_type=analysis.get('image_type'),
                tags=analysis.get('tags') or [],
                event_time=image.created_at,
            )

        working_memory = self.memory.get_or_create_working_memory(db, conversation_id)
        previous_wm = {
            'user_goal': working_memory.user_goal or '',
            'current_task': working_memory.current_task or '',
            'current_focus': working_memory.current_focus or {},
            'active_image_ids': [str(x) for x in (working_memory.active_image_ids or [])],
            'constraints': working_memory.constraints or [],
            'decisions': working_memory.decisions or [],
            'unresolved_questions': working_memory.unresolved_questions or [],
            'summary_buffer': working_memory.summary_buffer or '',
        }
        new_wm = self.gemini.update_working_memory(previous_wm, user_turn.text_content or '', image_summaries)
        working_memory.user_goal = new_wm.get('user_goal', '')
        working_memory.current_task = new_wm.get('current_task', '')
        working_memory.current_focus = new_wm.get('current_focus', {})
        parsed_active_ids = []
        for x in new_wm.get('active_image_ids', []) or []:
            try:
                parsed_active_ids.append(UUID(x))
            except Exception:
                pass
        working_memory.active_image_ids = parsed_active_ids or [UUID(x) for x in current_image_ids]
        working_memory.constraints = new_wm.get('constraints', [])
        working_memory.decisions = new_wm.get('decisions', [])
        working_memory.unresolved_questions = new_wm.get('unresolved_questions', [])
        working_memory.summary_buffer = new_wm.get('summary_buffer', '')
        db.flush()

        resolved_refs = resolve_reference(
            db=db,
            conversation_id=str(conversation_id),
            user_text=user_turn.text_content or '',
            current_image_ids=current_image_ids,
            timezone_name=conversation.timezone,
        )
        for ref in resolved_refs:
            db.add(
                ResolutionLog(
                    conversation_id=conversation_id,
                    turn_id=user_turn.id,
                    raw_expression=ref.expression,
                    resolution_type=ref.resolution_type,
                    resolved_image_id=UUID(ref.resolved_image_id) if ref.resolved_image_id else None,
                    confidence=ref.confidence,
                    resolver_output=ref.payload,
                )
            )

        query_embedding = self.gemini.embed_text((user_turn.text_content or '').strip() or 'empty query')
        temporal_range = None
        for ref in resolved_refs:
            if ref.resolution_type == 'temporal_image' and 'start_time' in ref.payload:
                temporal_range = {
                    'start_time': datetime.fromisoformat(ref.payload['start_time']),
                    'end_time': datetime.fromisoformat(ref.payload['end_time']),
                }
                break
        retrieved = self.retrieval.retrieve(
            db=db,
            conversation_id=str(conversation_id),
            query_text=(user_turn.text_content or '').strip(),
            query_embedding=query_embedding,
            temporal_range=temporal_range,
        )

        recent_turns = self.memory.recent_turns(db, conversation_id, limit=self.settings.max_recent_turns)
        rehydrated_paths: list[tuple[str, str]] = []
        if needs_visual_rehydration(user_turn.text_content or ''):
            for ref in resolved_refs:
                if ref.resolved_image_id:
                    image = db.get(ImageAsset, UUID(ref.resolved_image_id))
                    if image and image.storage_uri not in [p[0] for p in rehydrated_paths]:
                        rehydrated_paths.append((image.storage_uri, image.mime_type or 'image/png'))

        prompt = self._build_prompt(
            conversation=conversation,
            user_text=user_turn.text_content or '',
            recent_turns=recent_turns,
            working_memory=new_wm,
            image_summaries=image_summaries,
            retrieved=retrieved,
            resolved_refs=resolved_refs,
        )
        answer = self.gemini.answer(
            prompt=prompt,
            image_parts=current_image_parts + [self.gemini.image_part_from_file(path, mime) for path, mime in rehydrated_paths],
        )

        assistant_turn = Turn(
            conversation_id=conversation_id,
            turn_index=int(max_turn) + 2,
            role='assistant',
            text_content=answer,
            response_summary=compact_text(answer, 400),
        )
        db.add(assistant_turn)

        if (not conversation.title) or conversation.title == 'Multimodal Context Demo':
            source_text = (user_turn.text_content or '').strip()
            if source_text:
                conversation.title = compact_text(source_text, 60)
        db.flush()

        summary = self.gemini.summarize_turn(user_turn.text_content or '', answer)
        self.memory.persist_turn_memory(
            db=db,
            conversation_id=conversation_id,
            turn_id=user_turn.id,
            summary=summary,
            embedding=self.gemini.embed_text(summary or (user_turn.text_content or '')),
        )

        db.commit()
        db.refresh(assistant_turn)

        return {
            'conversation_id': conversation_id,
            'user_turn_id': user_turn.id,
            'assistant_turn_id': assistant_turn.id,
            'answer': answer,
            'resolved_references': [
                {
                    'expression': r.expression,
                    'resolution_type': r.resolution_type,
                    'resolved_image_id': r.resolved_image_id,
                    'confidence': r.confidence,
                    'payload': r.payload,
                }
                for r in resolved_refs
            ],
            'retrieved_items': retrieved,
            'working_memory': new_wm,
        }

    def _build_prompt(
        self,
        conversation: Conversation,
        user_text: str,
        recent_turns: list[Turn],
        working_memory: dict[str, Any],
        image_summaries: list[dict[str, Any]],
        retrieved: list[dict[str, Any]],
        resolved_refs: list[Any],
    ) -> str:
        recent_serialized = [
            {
                'turn_index': turn.turn_index,
                'role': turn.role,
                'text': turn.text_content,
                'summary': turn.response_summary,
            }
            for turn in recent_turns
        ]
        resolved_serialized = [
            {
                'expression': r.expression,
                'resolution_type': r.resolution_type,
                'resolved_image_id': r.resolved_image_id,
                'confidence': r.confidence,
                'payload': r.payload,
            }
            for r in resolved_refs
        ]
        return (
            'Bạn là assistant cho hệ thống quản lý cửa sổ ngữ cảnh đa phương thức. '
            'Hãy trả lời dựa trên recent memory, working memory, retrieval và ảnh đính kèm. '
            'Nếu tham chiếu ảnh đã được resolve thì ưu tiên dùng kết quả đó. '
            'Nếu chỉ cần nhận diện nội dung, ưu tiên caption/OCR; nếu đang có ảnh trực quan kèm theo thì có thể dùng ảnh để so sánh bố cục. '
            'Trả lời bằng tiếng Việt, rõ ràng, thực dụng.\n\n'
            f'Conversation timezone: {conversation.timezone}\n\n'
            f'Working memory:\n{json.dumps(working_memory, ensure_ascii=False, indent=2)}\n\n'
            f'Recent turns:\n{json.dumps(recent_serialized, ensure_ascii=False, indent=2)}\n\n'
            f'Current image summaries:\n{json.dumps(image_summaries, ensure_ascii=False, indent=2)}\n\n'
            f'Resolved references:\n{json.dumps(resolved_serialized, ensure_ascii=False, indent=2)}\n\n'
            f'Retrieved context:\n{json.dumps(retrieved, ensure_ascii=False, indent=2)}\n\n'
            f'User question hiện tại: {user_text}'
        )
