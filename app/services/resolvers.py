from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import asc, desc, or_, select
from sqlalchemy.orm import Session

from app.models import ImageAlias, ImageAsset, ImageUnderstanding, Turn, TurnImage
from app.utils import is_image_edit_request, resolve_temporal_expression


@dataclass
class ResolutionResult:
    expression: str
    resolution_type: str
    resolved_image_id: str | None
    confidence: float
    payload: dict[str, Any]


def build_image_catalog(db: Session, conversation_id: str) -> list[dict[str, Any]]:
    """Build a comprehensive catalog of all images in the conversation for LLM reasoning."""
    stmt = (
        select(ImageAsset, ImageUnderstanding, Turn.turn_index)
        .outerjoin(ImageUnderstanding, ImageUnderstanding.image_id == ImageAsset.id)
        .outerjoin(Turn, Turn.id == ImageAsset.uploaded_by_turn_id)
        .where(ImageAsset.conversation_id == conversation_id)
        .order_by(asc(ImageAsset.created_at))
    )
    rows = db.execute(stmt).all()
    
    catalog = []
    for img, understanding, turn_idx in rows:
        meta = img.metadata_json or {}
        catalog.append({
            "image_id": str(img.id),
            "source_kind": meta.get("source_kind", "user_uploaded"),
            "short_caption": understanding.short_caption if understanding else None,
            "tags": understanding.tags if understanding else [],
            "image_type": img.image_type,
            "turn_index": turn_idx,
            "created_at": img.created_at.isoformat() if img.created_at else None,
            "generation_action": meta.get("generation_action"),
            "edit_generation_index": meta.get("edit_generation_index"),
            "lineage_root_image_id": meta.get("lineage_root_image_id"),
        })
    return catalog


REFERENCE_PHRASES = [
    'ảnh này', 'ảnh trước đó', 'ảnh vừa rồi', 'dashboard cũ', 'ảnh hôm qua', 'ảnh hôm kia', 'thứ 2 tuần trước',
    'tuần trước', 'ảnh đầu tiên', 'bức ảnh đầu tiên', 'bức ảnh thứ 2', 'ảnh thứ 2', 'bức ảnh thứ hai', 'người ấy',
    'ảnh vừa tạo', 'ảnh đã sửa', 'ảnh chatbot tạo ra', 'ảnh bạn tạo ra', 'ảnh gốc', 'ảnh gốc thứ 2', 'ảnh user thứ 2',
]

ORDINAL_PATTERNS = {
    'ảnh đầu tiên': 0,
    'bức ảnh đầu tiên': 0,
    'ảnh thứ 2': 1,
    'bức ảnh thứ 2': 1,
    'bức ảnh thứ hai': 1,
}


def _image_meta(image: ImageAsset) -> dict[str, Any]:
    return image.metadata_json or {}


def _image_source_kind(image: ImageAsset) -> str:
    meta = _image_meta(image)
    return str(meta.get('source_kind') or meta.get('origin_role') or 'user_uploaded')


def _lineage_root(image: ImageAsset) -> str:
    meta = _image_meta(image)
    return str(meta.get('lineage_root_image_id') or image.id)


def _edit_generation_index(image: ImageAsset) -> int:
    meta = _image_meta(image)
    try:
        return int(meta.get('edit_generation_index') or 0)
    except Exception:
        return 0


def _get_ordered_images(db: Session, conversation_id: str, source_kind: str | None = None) -> list[ImageAsset]:
    stmt = (
        select(ImageAsset)
        .where(ImageAsset.conversation_id == conversation_id)
        .order_by(asc(ImageAsset.created_at))
    )
    images = list(db.execute(stmt).scalars().all())
    if not source_kind:
        return images
    return [img for img in images if _image_source_kind(img) == source_kind]


def _get_ordered_image_ids(db: Session, conversation_id: str, source_kind: str | None = None) -> list[str]:
    return [str(img.id) for img in _get_ordered_images(db, conversation_id, source_kind=source_kind)]


def _latest_generated_image(db: Session, conversation_id: str) -> ImageAsset | None:
    stmt = (
        select(ImageAsset)
        .where(ImageAsset.conversation_id == conversation_id)
        .order_by(desc(ImageAsset.created_at))
    )
    for image in db.execute(stmt).scalars().all():
        if _image_source_kind(image) == 'assistant_generated':
            return image
    return None


def _get_lineage_images(db: Session, conversation_id: str, root_image_id: str | None) -> list[ImageAsset]:
    if not root_image_id:
        return []
    images = [img for img in _get_ordered_images(db, conversation_id) if _image_source_kind(img) == 'assistant_generated']
    lineage = [img for img in images if _lineage_root(img) == str(root_image_id)]
    lineage.sort(key=lambda img: (_edit_generation_index(img), img.created_at))
    return lineage


def _get_lineage_image_ids(db: Session, conversation_id: str, root_image_id: str | None) -> list[str]:
    return [str(img.id) for img in _get_lineage_images(db, conversation_id, root_image_id)]


def _extract_requested_index(text: str) -> int | None:
    m = re.search(r'ảnh\s+thứ\s*(\d+)', text)
    if m:
        return max(1, int(m.group(1)))
    m = re.search(r'(?:ảnh|bức ảnh)\s*(?:số\s*)?(\d+)', text)
    if m:
        return max(1, int(m.group(1)))
    if 'ảnh đầu tiên' in text or 'bức ảnh đầu tiên' in text:
        return 1
    return None


def _ordinal_result(expression: str, image_id: str, index: int, strategy: str, confidence: float, resolution_type: str) -> ResolutionResult:
    return ResolutionResult(
        expression=expression,
        resolution_type=resolution_type,
        resolved_image_id=image_id,
        confidence=confidence,
        payload={'strategy': strategy, 'index': index},
    )


def detect_reference_expressions(text: str) -> list[str]:
    lower = text.lower()
    found: list[str] = []
    for phrase in REFERENCE_PHRASES:
        if phrase in lower:
            found.append(phrase)
    if re.search(r'ảnh\s+thứ\s*\d+', lower):
        found.append('ảnh thứ N')
    if re.search(r'(?:ảnh|bức ảnh)\s*(?:số\s*)?\d+', lower):
        found.append('ảnh số N')
    return list(dict.fromkeys(found))


def resolve_reference(
    db: Session,
    conversation_id: str,
    user_text: str,
    current_image_ids: list[str],
    timezone_name: str,
) -> list[ResolutionResult]:
    lower = user_text.lower()
    results: list[ResolutionResult] = []

    ordered_ids = _get_ordered_image_ids(db, conversation_id)
    user_uploaded_ids = _get_ordered_image_ids(db, conversation_id, source_kind='user_uploaded')
    assistant_ordered_ids = _get_ordered_image_ids(db, conversation_id, source_kind='assistant_generated')
    latest_generated = _latest_generated_image(db, conversation_id)
    latest_lineage_root = _lineage_root(latest_generated) if latest_generated else None
    lineage_ids = _get_lineage_image_ids(db, conversation_id, latest_lineage_root)
    requested_index = _extract_requested_index(lower)
    edit_request = is_image_edit_request(lower)

    if 'ảnh này' in lower and current_image_ids:
        results.append(
            ResolutionResult(
                expression='ảnh này',
                resolution_type='current_image',
                resolved_image_id=current_image_ids[0],
                confidence=0.99,
                payload={'strategy': 'current_turn_image'},
            )
        )

    if 'ảnh trước đó' in lower or 'ảnh vừa rồi' in lower:
        stmt = (
            select(ImageAsset.id)
            .join(TurnImage, TurnImage.image_id == ImageAsset.id)
            .where(ImageAsset.conversation_id == conversation_id)
            .order_by(desc(ImageAsset.created_at))
            .limit(6)
        )
        ids = [str(row[0]) for row in db.execute(stmt).all()]
        previous = next((i for i in ids if i not in current_image_ids), None)
        if previous:
            results.append(
                ResolutionResult(
                    expression='ảnh trước đó',
                    resolution_type='previous_image',
                    resolved_image_id=previous,
                    confidence=0.95,
                    payload={'strategy': 'recent_previous_image'},
                )
            )

    if ('ảnh gốc' in lower or 'ảnh user' in lower or 'ảnh người dùng' in lower) and requested_index and requested_index <= len(user_uploaded_ids):
        results.append(
            _ordinal_result(
                expression=f'ảnh gốc #{requested_index}',
                image_id=user_uploaded_ids[requested_index - 1],
                index=requested_index,
                strategy='user_uploaded_ordinal_lookup',
                confidence=0.96,
                resolution_type='user_uploaded_ordinal_image',
            )
        )

    if edit_request and requested_index and lineage_ids and requested_index <= len(lineage_ids):
        results.append(
            _ordinal_result(
                expression=f'ảnh sửa #{requested_index}',
                image_id=lineage_ids[requested_index - 1],
                index=requested_index,
                strategy='assistant_generated_lineage_lookup',
                confidence=0.97,
                resolution_type='assistant_generated_lineage_image',
            )
        )
    elif requested_index and requested_index <= len(user_uploaded_ids):
        results.append(
            _ordinal_result(
                expression=f'ảnh user #{requested_index}',
                image_id=user_uploaded_ids[requested_index - 1],
                index=requested_index,
                strategy='user_uploaded_default_lookup',
                confidence=0.93,
                resolution_type='user_uploaded_ordinal_image',
            )
        )
    elif requested_index and requested_index <= len(assistant_ordered_ids):
        results.append(
            _ordinal_result(
                expression=f'ảnh assistant #{requested_index}',
                image_id=assistant_ordered_ids[requested_index - 1],
                index=requested_index,
                strategy='assistant_generated_ordinal_lookup',
                confidence=0.9,
                resolution_type='assistant_generated_ordinal_image',
            )
        )
    elif requested_index and requested_index <= len(ordered_ids):
        results.append(
            _ordinal_result(
                expression=f'ảnh tổng #{requested_index}',
                image_id=ordered_ids[requested_index - 1],
                index=requested_index,
                strategy='global_ordinal_lookup',
                confidence=0.86,
                resolution_type='ordinal_image',
            )
        )

    assistant_ordinal = re.search(r'ảnh\s+(?:thứ\s*)?(\d+)\s+(?:bạn|chatbot)\s+tạo(?:\s+ra)?', lower)
    if assistant_ordinal:
        index = max(1, int(assistant_ordinal.group(1))) - 1
        if index < len(assistant_ordered_ids):
            results.append(
                ResolutionResult(
                    expression=assistant_ordinal.group(0),
                    resolution_type='assistant_generated_ordinal_image',
                    resolved_image_id=assistant_ordered_ids[index],
                    confidence=0.95,
                    payload={'strategy': 'assistant_generated_ordinal_lookup', 'index': index + 1},
                )
            )

    if ('ảnh vừa tạo' in lower or 'ảnh chatbot tạo ra' in lower or 'ảnh bạn tạo ra' in lower or 'ảnh đã sửa' in lower) and assistant_ordered_ids:
        results.append(
            ResolutionResult(
                expression='ảnh assistant gần nhất',
                resolution_type='recent_assistant_generated_image',
                resolved_image_id=assistant_ordered_ids[-1],
                confidence=0.92,
                payload={
                    'strategy': 'most_recent_assistant_generated_image',
                    'lineage_root_image_id': latest_lineage_root,
                    'lineage_length': len(lineage_ids),
                },
            )
        )

    for phrase, index in ORDINAL_PATTERNS.items():
        if phrase in lower and index < len(ordered_ids):
            results.append(
                ResolutionResult(
                    expression=phrase,
                    resolution_type='ordinal_image',
                    resolved_image_id=ordered_ids[index],
                    confidence=0.9,
                    payload={'strategy': 'legacy_ordinal_lookup', 'index': index + 1},
                )
            )

    for expr in ['hôm qua', 'hôm kia', 'tuần trước', 'thứ 2 tuần trước']:
        if expr in lower:
            temporal = resolve_temporal_expression(expr, timezone_name)
            if not temporal:
                continue
            stmt = (
                select(ImageAsset.id, ImageAsset.created_at)
                .where(
                    ImageAsset.conversation_id == conversation_id,
                    ImageAsset.created_at >= temporal['start_time'],
                    ImageAsset.created_at <= temporal['end_time'],
                )
                .order_by(desc(ImageAsset.created_at))
                .limit(1)
            )
            row = db.execute(stmt).first()
            if row:
                results.append(
                    ResolutionResult(
                        expression=expr,
                        resolution_type='temporal_image',
                        resolved_image_id=str(row[0]),
                        confidence=float(temporal['confidence']),
                        payload={
                            'strategy': 'temporal_filter',
                            'start_time': temporal['start_time'].isoformat(),
                            'end_time': temporal['end_time'].isoformat(),
                        },
                    )
                )

    if 'dashboard cũ' in lower:
        stmt = (
            select(ImageAlias.image_id, ImageAlias.alias_text)
            .join(ImageAsset, ImageAsset.id == ImageAlias.image_id)
            .where(
                ImageAsset.conversation_id == conversation_id,
                or_(
                    ImageAlias.alias_text.ilike('%dashboard%'),
                    ImageAlias.alias_text.ilike('%cũ%'),
                    ImageAlias.alias_text.ilike('%doanh thu%'),
                ),
            )
            .order_by(desc(ImageAlias.created_at))
            .limit(1)
        )
        row = db.execute(stmt).first()
        if row:
            results.append(
                ResolutionResult(
                    expression='dashboard cũ',
                    resolution_type='alias_image',
                    resolved_image_id=str(row[0]),
                    confidence=0.8,
                    payload={'strategy': 'alias_lookup', 'alias': row[1]},
                )
            )

    if 'người ấy' in lower:
        stmt = (
            select(ImageAsset.id)
            .where(ImageAsset.conversation_id == conversation_id)
            .order_by(desc(ImageAsset.created_at))
            .limit(1)
        )
        row = db.execute(stmt).first()
        if row:
            results.append(
                ResolutionResult(
                    expression='người ấy',
                    resolution_type='recent_subject_image',
                    resolved_image_id=str(row[0]),
                    confidence=0.7,
                    payload={'strategy': 'most_recent_image_subject'},
                )
            )

    dedup: dict[tuple[str, str | None], ResolutionResult] = {}
    for item in results:
        dedup[(item.expression, item.resolved_image_id)] = item
    return list(dedup.values())
