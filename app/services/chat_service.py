from __future__ import annotations

import json
import mimetypes
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Any, Iterator
from uuid import UUID

from sqlalchemy import desc, func, select

from app.config import get_settings
from app.db import SessionLocal
from app.models import (
    Conversation,
    DocumentAsset,
    DocumentUnderstanding,
    ImageAsset,
    ImageUnderstanding,
    ResolutionLog,
    Turn,
    TurnDocument,
    TurnImage,
)
from app.services.gemini_service import GeminiService
from app.services.memory_manager import MemoryManager
from app.services.resolvers import (
    ResolutionResult,
    build_image_catalog,
    detect_reference_expressions,
    resolve_reference,
)
from app.services.retrieval import RetrievalService
from app.utils import (
    compact_text,
    guess_mime_type,
    is_image_edit_request,
    needs_visual_rehydration,
    save_upload_bytes,
    sha256_of_file,
    should_attach_resolved_images,
    wants_image_input_debug,
)


class ChatService:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.gemini = GeminiService()
        self.memory = MemoryManager()
        self.retrieval = RetrievalService()
        self.executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix='memory-bg')

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

    def _image_meta(self, image: ImageAsset | None) -> dict[str, Any]:
        return dict((image.metadata_json or {}) if image else {})

    def _serialize_image_for_debug(self, image: ImageAsset | None, reason: str, role: str = 'resolved') -> dict[str, Any]:
        if not image:
            return {}
        meta = self._image_meta(image)
        return {
            'image_id': str(image.id),
            'url': self._build_image_url(image.storage_uri),
            'mime_type': image.mime_type,
            'image_type': image.image_type,
            'source_kind': meta.get('source_kind', 'user_uploaded'),
            'source_turn_id': str(image.uploaded_by_turn_id) if image.uploaded_by_turn_id else None,
            'generation_action': meta.get('generation_action'),
            'edit_generation_index': meta.get('edit_generation_index'),
            'lineage_root_image_id': meta.get('lineage_root_image_id'),
            'source_image_ids': meta.get('source_image_ids', []),
            'reason': reason,
            'role': role,
        }

    def _image_display_label(self, image: ImageAsset, understanding: ImageUnderstanding | None = None) -> str:
        meta = self._image_meta(image)
        source_kind = meta.get('source_kind', 'user_uploaded')
        if source_kind == 'assistant_generated':
            edit_index = meta.get('edit_generation_index')
            action = meta.get('generation_action') or 'generate'
            if action == 'edit' and edit_index:
                return f'Ảnh sửa #{edit_index}'
            if action == 'edit':
                return 'Ảnh đã chỉnh sửa'
            return 'Ảnh tạo bởi Nano Banana 2'
        caption = (understanding.short_caption if understanding else None) or image.image_type or 'Ảnh người dùng'
        return caption

    def _load_image_debug_records(self, db, current_image_ids: list[str], resolved_refs: list[ResolutionResult]) -> list[dict[str, Any]]:
        debug_records: list[dict[str, Any]] = []
        seen: set[str] = set()

        for image_id in current_image_ids:
            try:
                image = db.get(ImageAsset, UUID(image_id))
            except Exception:
                image = None
            if not image or str(image.id) in seen:
                continue
            seen.add(str(image.id))
            record = self._serialize_image_for_debug(image, reason='current_upload', role='current')
            if record:
                debug_records.append(record)

        for ref in resolved_refs:
            if not ref.resolved_image_id:
                continue
            try:
                image = db.get(ImageAsset, UUID(ref.resolved_image_id))
            except Exception:
                image = None
            if not image or str(image.id) in seen:
                continue
            seen.add(str(image.id))
            record = self._serialize_image_for_debug(image, reason=ref.expression, role='resolved')
            if record:
                record['resolution_type'] = ref.resolution_type
                record['confidence'] = ref.confidence
                debug_records.append(record)

        return debug_records

    def _lineage_root_id_for_image(self, image: ImageAsset | None) -> str | None:
        if not image:
            return None
        meta = self._image_meta(image)
        return str(meta.get('lineage_root_image_id') or image.id)

    def _derive_lineage_root_id(self, db, source_image_ids: list[str]) -> str | None:
        if not source_image_ids:
            return None
        try:
            image = db.get(ImageAsset, UUID(source_image_ids[0]))
        except Exception:
            image = None
        return self._lineage_root_id_for_image(image)

    def _next_edit_generation_index(self, db, conversation_id: UUID, lineage_root_image_id: str | None) -> int:
        if not lineage_root_image_id:
            return 1
        rows = db.execute(
            select(ImageAsset)
            .where(ImageAsset.conversation_id == conversation_id)
            .order_by(ImageAsset.created_at)
        ).scalars().all()
        current = 0
        for image in rows:
            meta = self._image_meta(image)
            if meta.get('source_kind') != 'assistant_generated':
                continue
            if str(meta.get('lineage_root_image_id')) != str(lineage_root_image_id):
                continue
            try:
                current = max(current, int(meta.get('edit_generation_index') or 0))
            except Exception:
                continue
        return current + 1

    def _save_generated_image_bytes(self, conversation_id: UUID, content: bytes, mime_type: str, index: int = 1) -> str:
        extension = mimetypes.guess_extension(mime_type or 'image/png', strict=False) or '.png'
        filename = f'nano-banana-{index}{extension}'
        return save_upload_bytes(self.settings.upload_dir, str(conversation_id), filename, content)

    def _resolve_reference_source_image_ids(self, resolved_refs: list[ResolutionResult]) -> list[str]:
        out: list[str] = []
        for ref in resolved_refs:
            if not ref.resolved_image_id:
                continue
            if ref.resolved_image_id not in out:
                out.append(ref.resolved_image_id)
        return out

    def _build_image_generation_instruction(
        self,
        user_text: str,
        working_memory: dict[str, Any],
        resolved_refs: list[ResolutionResult],
        model_input_images: list[dict[str, Any]],
    ) -> str:
        resolved_serialized = self._serialize_resolution_results(resolved_refs)
        has_source_images = bool(model_input_images)
        action_hint = (
            'MODE: IMAGE EDITING. Preserve core elements and modify exactly per user request.'
            if has_source_images else
            'MODE: NEW IMAGE GENERATION. Render faithfully according to user description.'
        )
        return (
            f'{action_hint}\n\n'
            'After generating/editing, write a short paragraph (2-3 sentences) describing it.\n'
            'MANDATORY:\n'
            '- Tone: Natural, warm, friendly.\n'
            '- Focus: Main subjects, colors, atmosphere.\n'
            '- NO internal IDs, tech specs, or system concepts (memory, refs, etc.).\n\n'
            f'User: {user_text}\n\n'
            f'Context (do not mention):\n'
            f'Working memory: {json.dumps(working_memory, ensure_ascii=False)}\n'
            f'Resolved references: {json.dumps(resolved_serialized, ensure_ascii=False)}\n'
            f'Input images: {json.dumps(model_input_images, ensure_ascii=False)}'
        )

    def _build_reference_image_parts(self, db, current_file_parts: list[Any], resolved_refs: list[ResolutionResult], current_image_ids: list[str]) -> list[Any]:
        parts = list(current_file_parts)
        seen = set(current_image_ids)
        for ref in resolved_refs:
            if not ref.resolved_image_id or ref.resolved_image_id in seen:
                continue
            try:
                image = db.get(ImageAsset, UUID(ref.resolved_image_id))
            except Exception:
                image = None
            if not image or not image.storage_uri:
                continue
            seen.add(str(image.id))
            parts.append(self.gemini.file_part_from_path(image.storage_uri, image.mime_type or 'image/png'))
            if len(parts) >= self.settings.max_reference_images_per_generation:
                break
        return parts[: self.settings.max_reference_images_per_generation]

    def create_conversation(self, title: str | None = None) -> Conversation:
        with SessionLocal() as db:
            convo = Conversation(title=title or 'Multimodal Context Demo', timezone=self.settings.timezone)
            db.add(convo)
            db.commit()
            db.refresh(convo)
            return convo

    def delete_conversation(self, conversation_id: UUID) -> None:
        with SessionLocal() as db:
            convo = db.get(Conversation, conversation_id)
            if not convo:
                raise ValueError('Conversation not found')
            db.delete(convo)
            db.commit()

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

            document_links = db.execute(
                select(TurnDocument, DocumentAsset, DocumentUnderstanding)
                .join(DocumentAsset, TurnDocument.document_id == DocumentAsset.id)
                .outerjoin(DocumentUnderstanding, DocumentUnderstanding.document_id == DocumentAsset.id)
                .where(TurnDocument.turn_id.in_(turn_ids) if turn_ids else False)
                .order_by(TurnDocument.position)
            ).all() if turn_ids else []

            images_by_turn: dict[UUID, list[dict[str, Any]]] = {}
            for turn_image, image, understanding in image_links:
                meta = self._image_meta(image)
                images_by_turn.setdefault(turn_image.turn_id, []).append(
                    {
                        'image_id': str(image.id),
                        'url': self._build_image_url(image.storage_uri),
                        'mime_type': image.mime_type,
                        'image_type': image.image_type,
                        'short_caption': understanding.short_caption if understanding else None,
                        'ocr_text_compressed': understanding.ocr_text_compressed if understanding else None,
                        'processing_status': meta.get('analysis_status', 'unknown'),
                        'source_kind': meta.get('source_kind', 'user_uploaded'),
                        'generation_action': meta.get('generation_action'),
                        'edit_generation_index': meta.get('edit_generation_index'),
                        'source_image_ids': meta.get('source_image_ids', []),
                        'display_label': self._image_display_label(image, understanding),
                    }
                )

            documents_by_turn: dict[UUID, list[dict[str, Any]]] = {}
            for turn_doc, document, understanding in document_links:
                documents_by_turn.setdefault(turn_doc.turn_id, []).append(
                    {
                        'document_id': str(document.id),
                        'url': self._build_image_url(document.storage_uri),
                        'file_name': document.file_name,
                        'mime_type': document.mime_type,
                        'summary': understanding.summary if understanding else None,
                        'tags': understanding.tags if understanding else [],
                        'processing_status': (document.metadata_json or {}).get('analysis_status', 'unknown'),
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
                    'documents': documents_by_turn.get(t.id, []),
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

    def process_image_generation(self, conversation_id: UUID, user_text: str | None, uploads: list[dict[str, Any]]) -> dict[str, Any]:
        started = perf_counter()
        user_text = (user_text or '').strip()
        if not user_text and not uploads:
            raise ValueError('Cần nhập mô tả tạo/chỉnh ảnh hoặc tải ít nhất một ảnh tham chiếu')

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
                metadata_json={'has_images': bool(uploads), 'request_mode': 'image_generation'},
            )
            db.add(user_turn)
            db.flush()

            current_image_ids: list[str] = []
            current_file_parts: list[Any] = []
            image_jobs: list[dict[str, Any]] = []
            file_summaries: list[dict[str, Any]] = []

            for idx, upload in enumerate(uploads):
                mime_type = upload.get('mime_type') or guess_mime_type(upload['filename'])
                if not mime_type.startswith('image/'):
                    raise ValueError('Chế độ tạo/chỉnh ảnh chỉ nhận file ảnh làm tham chiếu')

                file_path = save_upload_bytes(self.settings.upload_dir, str(conversation_id), upload['filename'], upload['content'])
                current_file_parts.append(self.gemini.file_part_from_path(file_path, mime_type))

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
                        'source_kind': 'user_uploaded',
                    },
                )
                db.add(image)
                db.flush()
                db.add(TurnImage(turn_id=user_turn.id, image_id=image.id, position=idx))
                current_image_ids.append(str(image.id))
                file_summaries.append(
                    {
                        'image_id': str(image.id),
                        'filename': upload['filename'],
                        'image_type': 'pending',
                        'status': 'reference_image_ready',
                    }
                )
                image_jobs.append({'image_id': str(image.id), 'file_path': file_path, 'mime_type': mime_type})

            working_memory = self.memory.get_or_create_working_memory(db, conversation_id)
            previous_wm = self.memory.serialize_working_memory(working_memory)
            fast_wm = self.memory.build_fast_working_memory(previous_wm, user_text, current_image_ids, [], file_summaries)
            fast_wm['current_focus'] = {
                **(fast_wm.get('current_focus') or {}),
                'focus_type': 'image_generation',
                'mode': 'nano_banana_2',
                'reference_image_ids': current_image_ids[: self.settings.max_reference_images_per_generation],
            }
            self.memory.apply_working_memory(db, conversation_id, fast_wm)

            # Using the new LLM resolver
            resolved_refs = self._llm_resolve_references(
                db=db,
                conversation_id=conversation_id,
                user_text=user_text,
                current_image_ids=current_image_ids,
                recent_turns=[] # In gen mode, usually don't need much history for ref resolver
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

            debug_image_records = self._load_image_debug_records(db, current_image_ids, resolved_refs)
            reference_parts = self._build_reference_image_parts(db, current_file_parts, resolved_refs, current_image_ids)
            source_image_ids = []
            for image_id in current_image_ids + self._resolve_reference_source_image_ids(resolved_refs):
                if image_id not in source_image_ids:
                    source_image_ids.append(image_id)

            instruction = self._build_image_generation_instruction(
                user_text=user_text,
                working_memory=fast_wm,
                resolved_refs=resolved_refs,
                model_input_images=debug_image_records,
            )
            result = self.gemini.generate_or_edit_image(instruction=instruction, image_parts=reference_parts)
            generated_images = result.get('images') or []
            if not generated_images:
                raise ValueError('Model không trả về ảnh. Hãy thử mô tả lại yêu cầu cụ thể hơn.')

            action = 'edit' if source_image_ids else 'generate'
            fallback_answer = 'Mình đã chỉnh ảnh xong bằng Nano Banana 2.' if action == 'edit' else 'Mình đã tạo ảnh xong bằng Nano Banana 2.'
            answer_text = compact_text(result.get('text') or fallback_answer, 1200)

            assistant_turn = Turn(
                conversation_id=conversation_id,
                turn_index=int(max_turn) + 2,
                role='assistant',
                text_content=answer_text,
                response_summary=compact_text(answer_text, 400),
                metadata_json={
                    'latency_ms': 0,
                    'processing_mode': 'nano_banana_2_image',
                    'background_enrichment_pending': True,
                    'resolved_references': self._serialize_resolution_results(resolved_refs),
                    'retrieved_items': [],
                    'reference_expressions': detect_reference_expressions(user_text),
                    'streaming_enabled': False,
                    'model_input_images': debug_image_records,
                    'debug_link_input_images': True,
                    'image_request_mode': action,
                    'image_model': self.settings.image_generation_model,
                },
            )
            db.add(assistant_turn)
            db.flush()

            lineage_root_id = self._derive_lineage_root_id(db, source_image_ids)
            base_generation_index = self._next_edit_generation_index(db, conversation_id, lineage_root_id) if action == 'edit' else 1
            generated_jobs: list[dict[str, Any]] = []
            generated_image_ids: list[str] = []

            for idx, generated in enumerate(generated_images, start=1):
                output_mime_type = generated.get('mime_type') or 'image/png'
                file_path = self._save_generated_image_bytes(conversation_id, generated.get('bytes') or b'', output_mime_type, idx)
                image_asset = ImageAsset(
                    conversation_id=conversation_id,
                    uploaded_by_turn_id=assistant_turn.id,
                    storage_uri=file_path,
                    mime_type=output_mime_type,
                    checksum=sha256_of_file(file_path),
                    image_type='generated_pending',
                    metadata_json={
                        'analysis_status': 'pending',
                        'original_filename': Path(file_path).name,
                        'source_kind': 'assistant_generated',
                        'origin_role': 'assistant_generated',
                        'generation_action': action,
                        'source_image_ids': source_image_ids,
                        'generation_prompt': user_text,
                        'lineage_root_image_id': lineage_root_id,
                        'edit_generation_index': base_generation_index + idx - 1,
                        'model_name': self.settings.image_generation_model,
                    },
                )
                db.add(image_asset)
                db.flush()

                if not image_asset.metadata_json.get('lineage_root_image_id'):
                    image_asset.metadata_json = {
                        **image_asset.metadata_json,
                        'lineage_root_image_id': str(image_asset.id),
                    }

                db.add(TurnImage(turn_id=assistant_turn.id, image_id=image_asset.id, position=idx - 1))
                generated_jobs.append({'image_id': str(image_asset.id), 'file_path': file_path, 'mime_type': output_mime_type})
                generated_image_ids.append(str(image_asset.id))

            updated_wm = {
                **fast_wm,
                'current_task': compact_text(user_text or 'Tạo hoặc chỉnh ảnh', 240),
                'active_image_ids': generated_image_ids[:2] or source_image_ids[:2],
                'current_focus': {
                    'focus_type': 'image_generation',
                    'mode': 'nano_banana_2',
                    'primary_image_ids': generated_image_ids[:2],
                    'reference_image_ids': source_image_ids[: self.settings.max_reference_images_per_generation],
                    'lineage_root_image_id': lineage_root_id or (generated_image_ids[0] if generated_image_ids else None),
                    'generation_action': action,
                },
                'summary_buffer': compact_text(' | '.join(filter(None, [fast_wm.get('summary_buffer', ''), user_text, answer_text])), 480),
            }
            self.memory.apply_working_memory(db, conversation_id, updated_wm)

            if (not conversation.title) or conversation.title == 'Multimodal Context Demo':
                source_text = user_text.strip()
                if source_text:
                    conversation.title = compact_text(source_text, 60)
            conversation.updated_at = datetime.utcnow()

            # Lưu ID vào biến local TRƯỚC khi commit để tránh detached instance
            user_turn_id = user_turn.id
            assistant_turn_id = assistant_turn.id
            db.commit()

        latency_ms = int((perf_counter() - started) * 1000)
        self._start_background_finalize(
            conversation_id=conversation_id,
            user_turn_id=user_turn_id,
            assistant_turn_id=assistant_turn_id,
            user_text=user_text,
            answer=answer_text,
            image_jobs=image_jobs + generated_jobs,
            document_jobs=[],
            previous_wm=updated_wm,
            resolved_refs=self._serialize_resolution_results(resolved_refs),
        )

        with SessionLocal() as db:
            turn_to_update = db.get(Turn, assistant_turn_id)
            if turn_to_update:
                meta = dict(turn_to_update.metadata_json or {})
                meta['latency_ms'] = latency_ms
                turn_to_update.metadata_json = meta
                db.commit()

        return {
            'conversation_id': conversation_id,
            'user_turn_id': user_turn_id,
            'assistant_turn_id': assistant_turn_id,
            'answer': answer_text,
            'resolved_references': self._serialize_resolution_results(resolved_refs),
            'retrieved_items': [],
            'working_memory': updated_wm,
            'model_input_images': debug_image_records,
            'latency_ms': latency_ms,
            'processing_mode': 'nano_banana_2_image',
            'background_enrichment_started': True,
        }

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

    def _llm_resolve_references(
        self,
        db,
        conversation_id: UUID,
        user_text: str,
        current_image_ids: list[str],
        recent_turns: list[Turn],
    ) -> list[ResolutionResult]:
        """Resolve image references using LLM reasoning with a fallback to legacy regex."""
        try:
            catalog = build_image_catalog(db, str(conversation_id))
            if not catalog:
                return []
            
            # Prepare minimal history for the resolver
            history = [
                {"role": t.role, "text": t.text_content, "turn_index": t.turn_index}
                for t in recent_turns
            ]
            
            # Call LLM
            result = self.gemini.resolve_image_references(
                user_text=user_text,
                recent_history=history,
                image_catalog=catalog,
                current_image_ids=current_image_ids
            )
            
            selected_ids = result.get("selected_image_ids") or []
            reasoning = result.get("reasoning", "LLM resolved")
            
            resolved_results = []
            for img_id in selected_ids:
                resolved_results.append(
                    ResolutionResult(
                        expression="LLM semantic reference",
                        resolution_type="llm_resolved",
                        resolved_image_id=img_id,
                        confidence=0.95,
                        payload={"strategy": "llm_semantic_resolution", "reasoning": reasoning}
                    )
                )
            
            # If LLM didn't find anything but the user used reference phrases, 
            # we might want to let the legacy resolver try too, OR just trust the LLM.
            # User said "gọi llm từ đầu luôn", so we trust it. 
            # But if it's empty and user-uploaded text has reference patterns, legacy might be safer as a fallback.
            if not resolved_results and detect_reference_expressions(user_text):
                 return resolve_reference(
                    db=db,
                    conversation_id=str(conversation_id),
                    user_text=user_text,
                    current_image_ids=current_image_ids,
                    timezone_name=self.settings.timezone
                )

            return resolved_results
            
        except Exception as exc:
            print(f"[ChatService] LLM resolve error, falling back: {exc}")
            return resolve_reference(
                db=db,
                conversation_id=str(conversation_id),
                user_text=user_text,
                current_image_ids=current_image_ids,
                timezone_name=self.settings.timezone
            )

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
                metadata_json={'has_images': bool(uploads), 'requested_debug_image_inputs': wants_image_input_debug(user_text)},
            )
            db.add(user_turn)
            db.flush()

            current_image_ids: list[str] = []
            current_document_ids: list[str] = []
            current_file_parts = []
            image_jobs: list[dict[str, Any]] = []
            document_jobs: list[dict[str, Any]] = []
            file_summaries: list[dict[str, Any]] = []

            for idx, upload in enumerate(uploads):
                file_path = save_upload_bytes(self.settings.upload_dir, str(conversation_id), upload['filename'], upload['content'])
                mime_type = upload.get('mime_type') or guess_mime_type(upload['filename'])
                current_file_parts.append(self.gemini.file_part_from_path(file_path, mime_type))

                if mime_type.startswith('image/'):
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
                            'source_kind': 'user_uploaded',
                        },
                    )
                    db.add(image)
                    db.flush()
                    db.add(TurnImage(turn_id=user_turn.id, image_id=image.id, position=idx))

                    current_image_ids.append(str(image.id))
                    file_summaries.append({
                        'image_id': str(image.id),
                        'filename': upload['filename'],
                        'image_type': 'pending',
                        'status': 'attached_and_answerable_now',
                    })
                    image_jobs.append({'image_id': str(image.id), 'file_path': file_path, 'mime_type': mime_type})
                else:
                    document = DocumentAsset(
                        conversation_id=conversation_id,
                        uploaded_by_turn_id=user_turn.id,
                        storage_uri=file_path,
                        file_name=upload['filename'],
                        mime_type=mime_type,
                        checksum=sha256_of_file(file_path),
                        metadata_json={
                            'analysis_status': 'pending',
                            'original_filename': upload['filename'],
                            'source_kind': 'user_uploaded',
                        },
                    )
                    db.add(document)
                    db.flush()
                    db.add(TurnDocument(turn_id=user_turn.id, document_id=document.id, position=idx))

                    current_document_ids.append(str(document.id))
                    file_summaries.append({
                        'document_id': str(document.id),
                        'filename': upload['filename'],
                        'status': 'attached_and_answerable_now',
                    })
                    document_jobs.append({'document_id': str(document.id), 'file_path': file_path, 'mime_type': mime_type})

            working_memory = self.memory.get_or_create_working_memory(db, conversation_id)
            previous_wm = self.memory.serialize_working_memory(working_memory)
            fast_wm = self.memory.build_fast_working_memory(previous_wm, user_text, current_image_ids, current_document_ids, file_summaries)
            self.memory.apply_working_memory(db, conversation_id, fast_wm)

            recent_turns = self.memory.recent_turns(db, conversation_id, limit=self.settings.max_recent_turns)
            
            # Optimization: Pre-build catalog and history for parallel LLM resolution
            catalog = build_image_catalog(db, str(conversation_id))
            history = [
                {"role": t.role, "text": t.text_content, "turn_index": t.turn_index}
                for t in recent_turns
            ]

            # Start LLM resolution in background
            res_future = self.executor.submit(
                self.gemini.resolve_image_references,
                user_text=user_text,
                recent_history=history,
                image_catalog=catalog,
                current_image_ids=current_image_ids
            )

            # Do retrieval in parallel
            temporal_range = None # Logic for temporal range from legacy refs was here, we'll wait for LLM
            
            # Wait for LLM (with timeout or just await)
            try:
                llm_res = res_future.result(timeout=5)
                selected_ids = llm_res.get("selected_image_ids") or []
                reasoning = llm_res.get("reasoning", "LLM resolved")
                resolved_refs = [
                    ResolutionResult(
                        expression="LLM semantic reference",
                        resolution_type="llm_resolved",
                        resolved_image_id=iid,
                        confidence=0.95,
                        payload={"strategy": "llm_semantic_resolution", "reasoning": reasoning}
                    )
                    for iid in selected_ids
                ]
            except Exception as exc:
                print(f"[ChatService] Parallel LLM resolve failed, fallback to legacy: {exc}")
                resolved_refs = resolve_reference(
                    db=db,
                    conversation_id=str(conversation_id),
                    user_text=user_text,
                    current_image_ids=current_image_ids,
                    timezone_name=conversation.timezone,
                )

            # Log resolutions
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

            # Re-check temporal range if any
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
            debug_image_records = self._load_image_debug_records(db, current_image_ids, resolved_refs)
            rehydrated_parts = self._build_rehydrated_image_parts(db, user_text, resolved_refs)
            prompt = self._build_prompt(
                timezone=conversation.timezone,
                user_text=user_text,
                recent_turns=recent_turns,
                working_memory=fast_wm,
                file_summaries=file_summaries,
                retrieved=retrieved,
                resolved_refs=resolved_refs,
                has_current_files=bool(current_file_parts),
                prefer_fast=prefer_fast,
                model_input_images=debug_image_records,
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
                'image_parts': current_file_parts + rehydrated_parts,
                'model_input_images': debug_image_records,
                'debug_link_input_images': wants_image_input_debug(user_text) or is_image_edit_request(user_text),
                'image_jobs': image_jobs,
                'document_jobs': document_jobs,
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
                    'model_input_images': prepared.get('model_input_images', []),
                    'debug_link_input_images': prepared.get('debug_link_input_images', False),
                },
            )
            db.add(assistant_turn)
            db.flush()

            if prepared.get('debug_link_input_images'):
                for position, item in enumerate(prepared.get('model_input_images', [])):
                    image_id = item.get('image_id')
                    if not image_id:
                        continue
                    try:
                        db.add(TurnImage(turn_id=assistant_turn.id, image_id=UUID(image_id), position=position))
                    except Exception:
                        continue

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
            document_jobs=prepared['document_jobs'],
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
            'model_input_images': prepared.get('model_input_images', []),
            'latency_ms': latency_ms,
            'processing_mode': prepared['processing_mode'],
            'background_enrichment_started': True,
        }

    def _should_prefer_fast_path(self, user_text: str, uploads: list[dict[str, Any]], resolved_refs: list[ResolutionResult]) -> bool:
        lower = (user_text or '').lower()
        if not user_text and uploads:
            return True
        if is_image_edit_request(lower) or wants_image_input_debug(lower):
            return False
        if uploads and not resolved_refs:
            quick_terms = ['tóm tắt', 'mô tả', 'ocr', 'đọc chữ', 'ảnh này', 'bức ảnh này', 'trong ảnh']
            if any(term in lower for term in quick_terms):
                return True
        if not uploads and not resolved_refs and len(lower) < 80:
            return True
        return False

    def _build_rehydrated_image_parts(self, db, user_text: str, resolved_refs: list[ResolutionResult]):
        if not should_attach_resolved_images(user_text or '', bool(resolved_refs)):
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
            parts.append(self.gemini.file_part_from_path(image.storage_uri, image.mime_type or 'image/png'))
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

    def _analyze_image_job(self, job: dict[str, Any]) -> dict[str, Any] | None:
        """Analyze một ảnh, trả về summary dict hoặc None nếu đã có sẵn."""
        try:
            with SessionLocal() as db:
                image = db.get(ImageAsset, UUID(job['image_id']))
                if not image:
                    return None
                existing = db.execute(
                    select(ImageUnderstanding).where(ImageUnderstanding.image_id == image.id)
                ).scalar_one_or_none()
                if existing:
                    return {
                        'image_id': str(image.id),
                        'short_caption': existing.short_caption,
                        'detailed_caption': existing.detailed_caption,
                        'ocr_text_compressed': existing.ocr_text_compressed,
                        'tags': existing.tags or [],
                        'image_type': image.image_type,
                        '_already_done': True,
                    }
            # Phân tích bên ngoài session để tránh giữ connection lâu
            analysis = self.gemini.analyze_image(job['file_path'], job['mime_type'])
            with SessionLocal() as db:
                image = db.get(ImageAsset, UUID(job['image_id']))
                if not image:
                    return None
                # Kiểm tra lại sau khi analyze (có thể worker khác đã xử lý rồi)
                existing = db.execute(
                    select(ImageUnderstanding).where(ImageUnderstanding.image_id == image.id)
                ).scalar_one_or_none()
                if not existing:
                    image.image_type = analysis.get('image_type') or 'other'
                    image.metadata_json = {**(image.metadata_json or {}), 'analysis_status': 'ready'}
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
                    self.memory.add_aliases(
                        db, image.id, job.get('user_turn_id'),
                        [
                            analysis.get('short_caption', ''),
                            analysis.get('image_type', ''),
                            *(analysis.get('tags') or []),
                        ],
                    )
                    self.memory.persist_image_memory(
                        db=db,
                        conversation_id=job['conversation_id'],
                        image_id=image.id,
                        content=analysis.get('textual_memory', ''),
                        embedding=analysis.get('embedding'),
                        image_type=analysis.get('image_type'),
                        tags=analysis.get('tags') or [],
                        event_time=image.created_at,
                    )
                    db.commit()
                    return {
                        'image_id': str(image.id),
                        'short_caption': analysis.get('short_caption'),
                        'detailed_caption': analysis.get('detailed_caption'),
                        'ocr_text_compressed': analysis.get('ocr_text_compressed'),
                        'tags': analysis.get('tags') or [],
                        'image_type': analysis.get('image_type'),
                    }
        except Exception as exc:
            print(f'[bg] analyze_image_job error {job.get("image_id")}: {exc}')
            return None

    def _analyze_document_job(self, job: dict[str, Any]) -> dict[str, Any] | None:
        """Analyze một document, trả về summary dict hoặc None."""
        try:
            with SessionLocal() as db:
                document = db.get(DocumentAsset, UUID(job['document_id']))
                if not document:
                    return None
                existing = db.execute(
                    select(DocumentUnderstanding).where(DocumentUnderstanding.document_id == document.id)
                ).scalar_one_or_none()
                if existing:
                    return {
                        'document_id': str(document.id),
                        'summary': existing.summary,
                        'tags': existing.tags or [],
                        '_already_done': True,
                    }
            analysis = self.gemini.analyze_document(job['file_path'], job['mime_type'])
            with SessionLocal() as db:
                document = db.get(DocumentAsset, UUID(job['document_id']))
                if not document:
                    return None
                existing = db.execute(
                    select(DocumentUnderstanding).where(DocumentUnderstanding.document_id == document.id)
                ).scalar_one_or_none()
                if not existing:
                    document.metadata_json = {**(document.metadata_json or {}), 'analysis_status': 'ready'}
                    understanding = DocumentUnderstanding(
                        document_id=document.id,
                        summary=analysis.get('summary'),
                        extracted_text=analysis.get('extracted_text'),
                        tags=analysis.get('tags') or [],
                        entities=analysis.get('entities') or [],
                        embedding=analysis.get('embedding'),
                    )
                    db.add(understanding)
                    self.memory.persist_document_memory(
                        db=db,
                        conversation_id=job['conversation_id'],
                        document_id=document.id,
                        content=analysis.get('textual_memory', ''),
                        embedding=analysis.get('embedding'),
                        tags=analysis.get('tags') or [],
                        event_time=document.created_at,
                    )
                    db.commit()
                return {
                    'document_id': str(document.id),
                    'summary': analysis.get('summary'),
                    'tags': analysis.get('tags') or [],
                }
        except Exception as exc:
            print(f'[bg] analyze_document_job error {job.get("document_id")}: {exc}')
            return None

    def _background_finalize(self, payload: dict[str, Any]) -> None:
        assistant_turn_id: UUID = payload['assistant_turn_id']
        conversation_id: UUID = payload['conversation_id']
        MAX_IMAGES_PER_JOB = 4
        try:
            image_jobs = payload.get('image_jobs', [])[:MAX_IMAGES_PER_JOB]
            document_jobs = payload.get('document_jobs', [])

            for job in image_jobs:
                job['conversation_id'] = conversation_id
                job['user_turn_id'] = payload.get('user_turn_id')
            for job in document_jobs:
                job['conversation_id'] = conversation_id

            # Batch Analyze Images (Significant speedup)
            file_summaries: list[dict[str, Any]] = []
            if image_jobs:
                batched_analyses = self.gemini.batch_analyze_images(image_jobs)
                # Persist each analysis
                with SessionLocal() as db:
                    for analysis in batched_analyses:
                        image_id = UUID(analysis['image_id'])
                        image = db.get(ImageAsset, image_id)
                        if not image: continue
                        
                        # Already done check
                        if db.execute(select(ImageUnderstanding).where(ImageUnderstanding.image_id == image.id)).scalar_one_or_none():
                            file_summaries.append({'image_id': str(image.id), '_already_done': True})
                            continue

                        image.image_type = analysis.get('image_type') or 'other'
                        image.metadata_json = {**(image.metadata_json or {}), 'analysis_status': 'ready'}
                        understanding = ImageUnderstanding(
                            image_id=image.id,
                            short_caption=analysis.get('short_caption'),
                            ocr_text_compressed=analysis.get('ocr_text_compressed'),
                            tags=analysis.get('tags') or [],
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
                        self.memory.add_aliases(db, image.id, payload.get('user_turn_id'), [
                            analysis.get('short_caption', ''),
                            analysis.get('image_type', ''),
                            *(analysis.get('tags') or []),
                        ])
                        self.memory.persist_image_memory(
                            db=db, conversation_id=conversation_id, image_id=image.id,
                            content=analysis.get('textual_memory', ''), embedding=analysis.get('embedding'),
                            image_type=analysis.get('image_type'), tags=analysis.get('tags') or [],
                            event_time=image.created_at,
                        )
                        file_summaries.append(analysis)
                    db.commit()

            # Individual Analyze Documents
            for doc_job in document_jobs:
                res = self._analyze_document_job(doc_job)
                if res: file_summaries.append(res)

            # Cập nhật working memory và turn summary
            with SessionLocal() as db:
                wm_row = self.memory.get_or_create_working_memory(db, conversation_id)
                previous_wm = self.memory.serialize_working_memory(wm_row)
                updated_wm = self.gemini.update_working_memory(
                    previous_memory=previous_wm,
                    user_text=payload['user_text'],
                    assistant_answer=payload['answer'],
                    image_summaries=[s for s in file_summaries if not s.get('_already_done')],
                    resolved_references=payload.get('resolved_refs', []),
                )
                self.memory.apply_working_memory(db, conversation_id, updated_wm)

                summary = self.gemini.summarize_turn(payload['user_text'], payload['answer'])
                self.memory.persist_turn_memory(
                    db=db, conversation_id=conversation_id, turn_id=payload['user_turn_id'],
                    summary=summary, embedding=self.gemini.embed_text(summary or (payload['user_text'] or 'empty turn')),
                )

                assistant_turn = db.get(Turn, assistant_turn_id)
                if assistant_turn:
                    meta = dict(assistant_turn.metadata_json or {})
                    meta['background_enrichment_pending'] = False
                    meta['background_enrichment_completed'] = True
                    meta['background_enrichment_completed_at'] = datetime.utcnow().isoformat()
                    meta['file_enrichment_count'] = len(file_summaries)
                    assistant_turn.metadata_json = meta
                db.commit()

        except Exception as exc:
            print(f'[bg] _background_finalize error: {exc}')
            try:
                with SessionLocal() as db:
                    assistant_turn = db.get(Turn, assistant_turn_id)
                    if assistant_turn:
                        meta = dict(assistant_turn.metadata_json or {})
                        meta['background_enrichment_pending'] = False
                        meta['background_enrichment_error'] = compact_text(str(exc), 200)
                        assistant_turn.metadata_json = meta
                        db.commit()
            except Exception:
                pass

    def _build_prompt(
        self,
        timezone: str,
        user_text: str,
        recent_turns: list[Turn],
        working_memory: dict[str, Any],
        file_summaries: list[dict[str, Any]],
        retrieved: list[dict[str, Any]],
        resolved_refs: list[ResolutionResult],
        has_current_files: bool,
        prefer_fast: bool,
        model_input_images: list[dict[str, Any]],
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

        file_note = (
            'The user has attached a file or image in this message. '
            'Analyze it directly from the attached content — do not wait for stored data.'
            if has_current_files else
            'No new file is attached in this turn. Use memory, conversation history, and retrieved context.'
        )
        speed_note = (
            'Be concise and focused — answer the question directly without unnecessary elaboration.'
            if prefer_fast else
            'Use retrieved context to provide additional detail where relevant.'
        )

        return (
            'You are an intelligent AI assistant with deep understanding of multimodal context (text, images, documents).\n'
            'Your task is to respond to the user naturally, accurately, and helpfully.\n\n'

            'RESPONSE PRINCIPLES:\n'
            '- Respond in Vietnamese, with a friendly yet professional tone.\n'
            '- Stay focused on what the user is asking — do not ramble or explain things they did not ask about.\n'
            '- If an image is attached, observe it carefully and describe or analyze it specifically and vividly.\n'
            '- If the question refers to a previously mentioned image, use resolved references and memory to identify the correct image.\n'
            '- NEVER mention image IDs, UUIDs, internal file names, or any internal system technical details in your response.\n'
            '- Do NOT expose internal data structures (working memory, resolved references, retrieved items, model_input_images...) to the user.\n'
            '- If you are uncertain about information, acknowledge it rather than fabricating an answer.\n\n'

            f'Conversation timezone: {timezone}\n'
            f'File note: {file_note}\n'
            f'Speed note: {speed_note}\n\n'

            '--- INTERNAL CONTEXT (for reasoning only — do not quote or expose to the user) ---\n'
            f'Working memory:\n{json.dumps(working_memory, ensure_ascii=False, indent=2)}\n\n'
            f'Recent conversation history:\n{json.dumps(recent_serialized, ensure_ascii=False, indent=2)}\n\n'
            f'Currently attached files/images:\n{json.dumps(file_summaries, ensure_ascii=False, indent=2)}\n\n'
            f'Images resolved from context:\n{json.dumps(resolved_serialized, ensure_ascii=False, indent=2)}\n\n'
            f'Model input images:\n{json.dumps(model_input_images, ensure_ascii=False, indent=2)}\n\n'
            f'Retrieved context:\n{json.dumps(retrieved, ensure_ascii=False, indent=2)}\n\n'
            '--- END INTERNAL CONTEXT ---\n\n'

            f'User message: {user_text}'
        )
