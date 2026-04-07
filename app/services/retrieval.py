from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import desc, or_, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import ImageAlias, ImageAsset, ImageUnderstanding, MemoryItem


class RetrievalService:
    def __init__(self) -> None:
        self.settings = get_settings()

    def retrieve(
        self,
        db: Session,
        conversation_id: str,
        query_text: str,
        query_embedding: list[float] | None,
        temporal_range: dict[str, Any] | None = None,
        resolved_image_ids: list[str] | None = None,
        prefer_fast: bool = False,
    ) -> list[dict[str, Any]]:
        semantic_items = []
        if query_embedding and not prefer_fast:
            semantic_items = self._semantic_search(db, conversation_id, query_embedding, temporal_range)
        keyword_items = self._keyword_search(db, conversation_id, query_text, temporal_range)
        alias_items = self._alias_search(db, conversation_id, query_text)
        resolved_items = self._resolved_image_context(db, conversation_id, resolved_image_ids or [])

        bucket: dict[str, dict[str, Any]] = {}
        for collection in (resolved_items, semantic_items, keyword_items, alias_items):
            for item in collection:
                bucket.setdefault(item['key'], item)
                for score_key in ('direct_score', 'semantic_score', 'keyword_score', 'alias_score', 'temporal_score'):
                    if score_key in item:
                        bucket[item['key']][score_key] = item[score_key]

        results = []
        for item in bucket.values():
            semantic_score = item.get('semantic_score', 0.0)
            keyword_score = item.get('keyword_score', 0.0)
            alias_score = item.get('alias_score', 0.0)
            direct_score = item.get('direct_score', 0.0)
            temporal_score = item.get('temporal_score', 1.0 if temporal_range else 0.5)
            item['final_score'] = round(
                0.30 * semantic_score + 0.22 * keyword_score + 0.10 * alias_score + 0.28 * direct_score + 0.10 * temporal_score,
                5,
            )
            results.append(item)

        results.sort(key=lambda x: x['final_score'], reverse=True)
        return results[: self.settings.max_retrieved_items]

    def _semantic_search(self, db: Session, conversation_id: str, query_embedding: list[float], temporal_range: dict[str, Any] | None) -> list[dict[str, Any]]:
        stmt = (
            select(MemoryItem)
            .where(MemoryItem.conversation_id == conversation_id)
            .where(MemoryItem.embedding.is_not(None))
            .order_by(MemoryItem.embedding.cosine_distance(query_embedding))
            .limit(8)
        )
        if temporal_range:
            stmt = stmt.where(
                MemoryItem.event_time_start >= temporal_range['start_time'],
                MemoryItem.event_time_end <= temporal_range['end_time'],
            )

        rows = db.execute(stmt).scalars().all()
        results = []
        for rank, row in enumerate(rows, start=1):
            results.append(
                {
                    'key': f'memory:{row.id}',
                    'id': str(row.id),
                    'kind': 'memory',
                    'content': row.content,
                    'memory_type': row.memory_type,
                    'semantic_score': max(0.0, 1.0 - ((rank - 1) * 0.08)),
                    'temporal_score': 1.0 if temporal_range else 0.5,
                }
            )
        return results

    def _keyword_search(self, db: Session, conversation_id: str, query_text: str, temporal_range: dict[str, Any] | None) -> list[dict[str, Any]]:
        query_text = (query_text or '').strip()
        if not query_text:
            return []
        like = f'%{query_text}%'
        stmt = (
            select(MemoryItem)
            .where(MemoryItem.conversation_id == conversation_id)
            .where(MemoryItem.content.ilike(like))
            .order_by(desc(MemoryItem.created_at))
            .limit(8)
        )
        if temporal_range:
            stmt = stmt.where(
                MemoryItem.event_time_start >= temporal_range['start_time'],
                MemoryItem.event_time_end <= temporal_range['end_time'],
            )

        items = db.execute(stmt).scalars().all()
        out: list[dict[str, Any]] = []
        for row in items:
            out.append(
                {
                    'key': f'memory:{row.id}',
                    'id': str(row.id),
                    'kind': 'memory',
                    'content': row.content,
                    'memory_type': row.memory_type,
                    'keyword_score': 0.9,
                    'temporal_score': 1.0 if temporal_range else 0.5,
                }
            )

        img_stmt = (
            select(ImageAsset, ImageUnderstanding)
            .join(ImageUnderstanding, ImageUnderstanding.image_id == ImageAsset.id)
            .where(ImageAsset.conversation_id == conversation_id)
            .where(
                or_(
                    ImageUnderstanding.short_caption.ilike(like),
                    ImageUnderstanding.detailed_caption.ilike(like),
                    ImageUnderstanding.ocr_text.ilike(like),
                    ImageUnderstanding.ocr_text_compressed.ilike(like),
                )
            )
            .order_by(desc(ImageAsset.created_at))
            .limit(4)
        )
        if temporal_range:
            img_stmt = img_stmt.where(
                ImageAsset.created_at >= temporal_range['start_time'],
                ImageAsset.created_at <= temporal_range['end_time'],
            )

        for image, understanding in db.execute(img_stmt).all():
            out.append(
                {
                    'key': f'image:{image.id}',
                    'id': str(image.id),
                    'kind': 'image',
                    'content': '\n'.join([
                        understanding.short_caption or '',
                        understanding.detailed_caption or '',
                        understanding.ocr_text_compressed or '',
                    ]).strip(),
                    'image_type': image.image_type,
                    'keyword_score': 0.85,
                    'temporal_score': 1.0 if temporal_range else 0.5,
                }
            )
        return out

    def _alias_search(self, db: Session, conversation_id: str, query_text: str) -> list[dict[str, Any]]:
        query_text = (query_text or '').strip()
        if not query_text:
            return []
        like = f'%{query_text}%'
        stmt = (
            select(ImageAlias)
            .join(ImageAsset, ImageAsset.id == ImageAlias.image_id)
            .where(ImageAsset.conversation_id == conversation_id)
            .where(ImageAlias.alias_text.ilike(like))
            .order_by(desc(ImageAlias.created_at))
            .limit(4)
        )
        results = []
        for row in db.execute(stmt).scalars().all():
            results.append(
                {
                    'key': f'image:{row.image_id}',
                    'id': str(row.image_id),
                    'kind': 'image',
                    'content': row.alias_text,
                    'alias_score': min(1.0, row.confidence),
                }
            )
        return results

    def _resolved_image_context(self, db: Session, conversation_id: str, resolved_image_ids: list[str]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        seen = set()
        for image_id in resolved_image_ids:
            if not image_id or image_id in seen:
                continue
            seen.add(image_id)
            try:
                image_uuid = UUID(image_id)
            except Exception:
                continue
            row = db.execute(
                select(ImageAsset, ImageUnderstanding)
                .outerjoin(ImageUnderstanding, ImageUnderstanding.image_id == ImageAsset.id)
                .where(ImageAsset.conversation_id == conversation_id, ImageAsset.id == image_uuid)
            ).first()
            if not row:
                continue
            image, understanding = row
            content_parts = []
            if understanding:
                content_parts.extend([
                    understanding.short_caption or '',
                    understanding.detailed_caption or '',
                    understanding.ocr_text_compressed or '',
                ])
            results.append(
                {
                    'key': f'image:{image.id}',
                    'id': str(image.id),
                    'kind': 'image',
                    'content': '\n'.join(part for part in content_parts if part).strip(),
                    'image_type': image.image_type,
                    'direct_score': 1.0,
                    'temporal_score': 0.9,
                }
            )
        return results
