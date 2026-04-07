from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Any, Iterator
from uuid import UUID

from sqlalchemy import desc, func, select

from app.config import get_settings
from app.db import SessionLocal
from app.models import Conversation, ImageAsset, ImageUnderstanding, ResolutionLog, Turn, TurnImage
from app.services.gemini_service import GeminiService
from app.services.memory_manager import MemoryManager
from app.services.resolvers import ResolutionResult, detect_reference_expressions, resolve_reference
from app.services.retrieval import RetrievalService
from app.utils import compact_text, guess_mime_type, needs_visual_rehydration, save_upload_bytes, sha256_of_file


class ChatService:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.gemini = GeminiService()
        self.memory = MemoryManager()
        self.retrieval = RetrievalService()
        self.executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix='memory-bg')

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

    def create_conversation(self, title: str | None = None) -> Conversation:
        with SessionLocal() as db:
            convo = Conversation(title=title or 'Multimodal Context Demo', timezone=self.settings.timezone)
            db.add(convo)
            db.commit()
            db.refresh(convo)
            return convo

    def list_conversations(self) -> list[dict[str, Any]]:
        with SessionLocal() as db:
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

    def get_conversation_detail(self, conversation_id: UUID) -> dict[str, Any]:
        with SessionLocal() as db:
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
                        'processing_status': (image.metadata_json or {}).get('analysis_status', 'unknown'),
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

    def memory_snapshot(self, conversation_id: UUID) -> dict[str, Any]:
        with SessionLocal() as db:
            return self.memory.snapshot(db, conversation_id)

    def process_chat(self, conversation_id: UUID, user_text: str | None, uploads: list[dict[str, Any]]) -> dict[str, Any]:
        started = perf_counter()
        prepared = self._prepare_chat_request(conversation_id, user_text, uploads)
        answer = self.gemini.answer(prompt=prepared['prompt'], image_parts=prepared['image_parts'])
        latency_ms = int((perf_counter() - started) * 1000)
        return self._persist_answer_and_schedule(prepared, answer, latency_ms)

    def process_chat_stream(self, conversation_id: UUID, user_text: str | None, uploads: list[dict[str, Any]]) -> Iterator[str]:
        started = perf_counter()
        try:
            prepared = self._prepare_chat_request(conversation_id, user_text, uploads)
        except ValueError as exc:
            yield self._sse('error', {'message': str(exc)})
            return
        except Exception as exc:
            yield self._sse('error', {'message': f'Internal error: {exc}'})
            return

        yield self._sse('meta', {
            'conversation_id': str(prepared['conversation_id']),
            'user_turn_id': str(prepared['user_turn_id']),
            'processing_mode': prepared['processing_mode'],
            'background_enrichment_started': True,
        })

        chunks: list[str] = []
        try:
            for text in self.gemini.stream_answer(prompt=prepared['prompt'], image_parts=prepared['image_parts']):
                if not text:
                    continue
                chunks.append(text)
                yield self._sse('token', {'text': text})
            answer = ''.join(chunks).strip()
            latency_ms = int((perf_counter() - started) * 1000)
            result = self._persist_answer_and_schedule(prepared, answer, latency_ms)
            yield self._sse('done', self._json_safe(result))
        except Exception as exc:
            yield self._sse('error', {'message': f'Internal error: {exc}'})

    def _sse(self, event: str, data: dict[str, Any]) -> str:
        return f"event: {event}\ndata: {json.dumps(self._json_safe(data), ensure_ascii=False)}\n\n"

    def _json_safe(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {k: self._json_safe(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._json_safe(v) for v in value]
        if isinstance(value, UUID):
            return str(value)
        if isinstance(value, datetime):
            return value.isoformat()
        return value

    def _prepare_chat_request(self, conversation_id: UUID, user_text: str | None, uploads: list[dict[str, Any]]) -> dict[str, Any]:
        user_text = (user_text or '').strip()
        with SessionLocal() as db:
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
                text_content=user_text,
                response_summary='',
                metadata_json={'has_images': bool(uploads)},
            )
            db.add(user_turn)
            db.flush()

            current_image_ids: list[str] = []
            current_image_parts = []
            image_jobs: list[dict[str, Any]] = []
            image_placeholders: list[dict[str, Any]] = []

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
                    image_type='pending',
                    metadata_json={
                        'analysis_status': 'pending',
                        'original_filename': upload['filename'],
                    },
                )
                db.add(image)
                db.flush()
                db.add(TurnImage(turn_id=user_turn.id, image_id=image.id, position=idx))

                current_image_id = str(image.id)
                current_image_ids.append(current_image_id)
                placeholder = {
                    'image_id': current_image_id,
                    'filename': upload['filename'],
                    'image_type': 'pending',
                    'status': 'attached_and_answerable_now',
                }
                image_placeholders.append(placeholder)
                image_jobs.append(
                    {
                        'image_id': current_image_id,
                        'file_path': file_path,
                        'mime_type': mime_type,
                    }
                )

            working_memory = self.memory.get_or_create_working_memory(db, conversation_id)
            previous_wm = self.memory.serialize_working_memory(working_memory)
            fast_wm = self.memory.build_fast_working_memory(previous_wm, user_text, current_image_ids, image_placeholders)
            self.memory.apply_working_memory(db, conversation_id, fast_wm)

            resolved_refs = resolve_reference(
                db=db,
                conversation_id=str(conversation_id),
                user_text=user_text,
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

            temporal_range = None
            for ref in resolved_refs:
                if ref.resolution_type == 'temporal_image' and 'start_time' in ref.payload:
                    temporal_range = {
                        'start_time': datetime.fromisoformat(ref.payload['start_time']),
                        'end_time': datetime.fromisoformat(ref.payload['end_time']),
                    }
                    break

            prefer_fast = self._should_prefer_fast_path(user_text, uploads, resolved_refs)
            query_embedding = None
            if user_text and not prefer_fast:
                query_embedding = self.gemini.embed_text(user_text)

            resolved_image_ids = [r.resolved_image_id for r in resolved_refs if r.resolved_image_id]
            retrieved = self.retrieval.retrieve(
                db=db,
                conversation_id=str(conversation_id),
                query_text=user_text,
                query_embedding=query_embedding,
                temporal_range=temporal_range,
                resolved_image_ids=resolved_image_ids,
                prefer_fast=prefer_fast,
            )

            recent_turns = self.memory.recent_turns(db, conversation_id, limit=self.settings.max_recent_turns)
            rehydrated_parts = self._build_rehydrated_image_parts(db, user_text, resolved_refs)
            prompt = self._build_prompt(
                timezone=conversation.timezone,
                user_text=user_text,
                recent_turns=recent_turns,
                working_memory=fast_wm,
                image_summaries=image_placeholders,
                retrieved=retrieved,
                resolved_refs=resolved_refs,
                has_current_images=bool(current_image_parts),
                prefer_fast=prefer_fast,
            )

            conversation.updated_at = datetime.utcnow()
            db.commit()

            processing_mode = 'fast_image_parallel' if uploads else ('fast_text' if prefer_fast else 'standard_text')
            return {
                'conversation_id': conversation_id,
                'conversation_timezone': conversation.timezone,
                'user_turn_id': user_turn.id,
                'user_text': user_text,
                'prompt': prompt,
                'image_parts': current_image_parts + rehydrated_parts,
                'image_jobs': image_jobs,
                'resolved_references': self._serialize_resolution_results(resolved_refs),
                'retrieved_items': retrieved,
                'working_memory': fast_wm,
                'processing_mode': processing_mode,
                'max_turn': int(max_turn),
            }

    def _persist_answer_and_schedule(self, prepared: dict[str, Any], answer: str, latency_ms: int) -> dict[str, Any]:
        with SessionLocal() as db:
            conversation = db.get(Conversation, prepared['conversation_id'])
            if not conversation:
                raise ValueError('Conversation not found')

            assistant_turn = Turn(
                conversation_id=prepared['conversation_id'],
                turn_index=prepared['max_turn'] + 2,
                role='assistant',
                text_content=answer,
                response_summary=compact_text(answer, 400),
                metadata_json={
                    'latency_ms': latency_ms,
                    'processing_mode': prepared['processing_mode'],
                    'background_enrichment_pending': True,
                    'resolved_references': prepared['resolved_references'],
                    'retrieved_items': prepared['retrieved_items'][:4],
                    'reference_expressions': detect_reference_expressions(prepared['user_text']),
                    'streaming_enabled': True,
                },
            )
            db.add(assistant_turn)

            if (not conversation.title) or conversation.title == 'Multimodal Context Demo':
                source_text = prepared['user_text'].strip()
                if source_text:
                    conversation.title = compact_text(source_text, 60)
            conversation.updated_at = datetime.utcnow()
            db.flush()

            assistant_turn_id = assistant_turn.id
            db.commit()

        self._start_background_finalize(
            conversation_id=prepared['conversation_id'],
            user_turn_id=prepared['user_turn_id'],
            assistant_turn_id=assistant_turn_id,
            user_text=prepared['user_text'],
            answer=answer,
            image_jobs=prepared['image_jobs'],
            previous_wm=prepared['working_memory'],
            resolved_refs=prepared['resolved_references'],
        )

        return {
            'conversation_id': prepared['conversation_id'],
            'user_turn_id': prepared['user_turn_id'],
            'assistant_turn_id': assistant_turn_id,
            'answer': answer,
            'resolved_references': prepared['resolved_references'],
            'retrieved_items': prepared['retrieved_items'],
            'working_memory': prepared['working_memory'],
            'latency_ms': latency_ms,
            'processing_mode': prepared['processing_mode'],
            'background_enrichment_started': True,
        }

    def _should_prefer_fast_path(self, user_text: str, uploads: list[dict[str, Any]], resolved_refs: list[ResolutionResult]) -> bool:
        lower = (user_text or '').lower()
        if not user_text and uploads:
            return True
        if uploads and not resolved_refs:
            quick_terms = ['tóm tắt', 'mô tả', 'ocr', 'đọc chữ', 'ảnh này', 'bức ảnh này', 'trong ảnh']
            if any(term in lower for term in quick_terms):
                return True
        if not uploads and not resolved_refs and len(lower) < 80:
            return True
        return False

    def _build_rehydrated_image_parts(self, db, user_text: str, resolved_refs: list[ResolutionResult]):
        if not needs_visual_rehydration(user_text or ''):
            return []
        parts = []
        seen_paths = set()
        for ref in resolved_refs:
            if not ref.resolved_image_id:
                continue
            image = db.get(ImageAsset, UUID(ref.resolved_image_id))
            if not image or image.storage_uri in seen_paths:
                continue
            seen_paths.add(image.storage_uri)
            parts.append(self.gemini.image_part_from_file(image.storage_uri, image.mime_type or 'image/png'))
        return parts

    def _serialize_resolution_results(self, resolved_refs: list[ResolutionResult]) -> list[dict[str, Any]]:
        return [
            {
                'expression': r.expression,
                'resolution_type': r.resolution_type,
                'resolved_image_id': r.resolved_image_id,
                'confidence': r.confidence,
                'payload': r.payload,
            }
            for r in resolved_refs
        ]

    def _start_background_finalize(self, **payload) -> None:
        self.executor.submit(self._background_finalize, payload)

    def _background_finalize(self, payload: dict[str, Any]) -> None:
        assistant_turn_id: UUID = payload['assistant_turn_id']
        try:
            with SessionLocal() as db:
                conversation_id: UUID = payload['conversation_id']
                image_summaries: list[dict[str, Any]] = []

                for job in payload.get('image_jobs', []):
                    image = db.get(ImageAsset, UUID(job['image_id']))
                    if not image:
                        continue
                    understanding = db.execute(
                        select(ImageUnderstanding).where(ImageUnderstanding.image_id == image.id)
                    ).scalar_one_or_none()
                    if understanding:
                        image_summaries.append(
                            {
                                'image_id': str(image.id),
                                'short_caption': understanding.short_caption,
                                'detailed_caption': understanding.detailed_caption,
                                'ocr_text_compressed': understanding.ocr_text_compressed,
                                'tags': understanding.tags or [],
                                'image_type': image.image_type,
                            }
                        )
                        continue

                    analysis = self.gemini.analyze_image(job['file_path'], job['mime_type'])
                    image.image_type = analysis.get('image_type') or 'other'
                    image.metadata_json = {
                        **(image.metadata_json or {}),
                        'analysis_status': 'ready',
                    }
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

                    self.memory.add_aliases(
                        db,
                        image.id,
                        payload['user_turn_id'],
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
                    image_summaries.append(
                        {
                            'image_id': str(image.id),
                            'short_caption': analysis.get('short_caption'),
                            'detailed_caption': analysis.get('detailed_caption'),
                            'ocr_text_compressed': analysis.get('ocr_text_compressed'),
                            'tags': analysis.get('tags') or [],
                            'image_type': analysis.get('image_type'),
                        }
                    )

                wm_row = self.memory.get_or_create_working_memory(db, conversation_id)
                previous_wm = self.memory.serialize_working_memory(wm_row)
                updated_wm = self.gemini.update_working_memory(
                    previous_memory=previous_wm,
                    user_text=payload['user_text'],
                    assistant_answer=payload['answer'],
                    image_summaries=image_summaries,
                    resolved_references=payload.get('resolved_refs', []),
                )
                self.memory.apply_working_memory(db, conversation_id, updated_wm)

                summary = self.gemini.summarize_turn(payload['user_text'], payload['answer'])
                self.memory.persist_turn_memory(
                    db=db,
                    conversation_id=conversation_id,
                    turn_id=payload['user_turn_id'],
                    summary=summary,
                    embedding=self.gemini.embed_text(summary or (payload['user_text'] or 'empty turn')),
                )

                assistant_turn = db.get(Turn, assistant_turn_id)
                if assistant_turn:
                    meta = dict(assistant_turn.metadata_json or {})
                    meta['background_enrichment_pending'] = False
                    meta['background_enrichment_completed'] = True
                    meta['background_enrichment_completed_at'] = datetime.utcnow().isoformat()
                    meta['image_enrichment_count'] = len(image_summaries)
                    assistant_turn.metadata_json = meta
                db.commit()
        except Exception as exc:
            with SessionLocal() as db:
                assistant_turn = db.get(Turn, assistant_turn_id)
                if assistant_turn:
                    meta = dict(assistant_turn.metadata_json or {})
                    meta['background_enrichment_pending'] = False
                    meta['background_enrichment_error'] = compact_text(str(exc), 200)
                    assistant_turn.metadata_json = meta
                    db.commit()

    def _build_prompt(
        self,
        timezone: str,
        user_text: str,
        recent_turns: list[Turn],
        working_memory: dict[str, Any],
        image_summaries: list[dict[str, Any]],
        retrieved: list[dict[str, Any]],
        resolved_refs: list[ResolutionResult],
        has_current_images: bool,
        prefer_fast: bool,
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
        resolved_serialized = self._serialize_resolution_results(resolved_refs)
        mode_note = (
            'Current uploaded images are attached directly to this request. Use the raw images immediately for description, OCR and reasoning. '
            'Their database enrichment may still be running in parallel, so do not wait for stored OCR or caption if the raw image is enough.'
            if has_current_images else
            'No new image is attached in this turn. Use recent memory, resolved references and retrieved context.'
        )
        speed_note = 'Prefer concise context usage and avoid relying on long retrieved history unless it is clearly relevant.' if prefer_fast else 'Use the available retrieved context when helpful.'
        return (
            'Bạn là assistant cho hệ thống quản lý cửa sổ ngữ cảnh đa phương thức. '
            'Hãy trả lời dựa trên recent memory, working memory, retrieval và ảnh đính kèm. '
            'Nếu tham chiếu ảnh đã được resolve thì ưu tiên dùng kết quả đó. '
            'Nếu chỉ cần nhận diện nội dung, ưu tiên caption hoặc OCR; nếu có ảnh trực quan kèm theo thì có thể dùng ảnh để so sánh bố cục. '
            'Trả lời bằng tiếng Việt, rõ ràng, thực dụng và bám sát dữ liệu đang có.\n\n'
            f'Conversation timezone: {timezone}\n\n'
            f'Mode note: {mode_note}\n'
            f'Speed note: {speed_note}\n\n'
            f'Working memory:\n{json.dumps(working_memory, ensure_ascii=False, indent=2)}\n\n'
            f'Recent turns:\n{json.dumps(recent_serialized, ensure_ascii=False, indent=2)}\n\n'
            f'Current image context:\n{json.dumps(image_summaries, ensure_ascii=False, indent=2)}\n\n'
            f'Resolved references:\n{json.dumps(resolved_serialized, ensure_ascii=False, indent=2)}\n\n'
            f'Retrieved context:\n{json.dumps(retrieved, ensure_ascii=False, indent=2)}\n\n'
            f'User question hiện tại: {user_text}'
        )
