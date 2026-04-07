from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import asc, desc, or_, select
from sqlalchemy.orm import Session

from app.models import ImageAlias, ImageAsset, TurnImage
from app.utils import resolve_temporal_expression


@dataclass
class ResolutionResult:
    expression: str
    resolution_type: str
    resolved_image_id: str | None
    confidence: float
    payload: dict[str, Any]


REFERENCE_PHRASES = [
    'ảnh này', 'ảnh trước đó', 'ảnh vừa rồi', 'dashboard cũ', 'ảnh hôm qua', 'ảnh hôm kia', 'thứ 2 tuần trước',
    'tuần trước', 'ảnh đầu tiên', 'bức ảnh đầu tiên', 'bức ảnh thứ 2', 'ảnh thứ 2', 'bức ảnh thứ hai', 'người ấy'
]

ORDINAL_PATTERNS = {
    'ảnh đầu tiên': 0,
    'bức ảnh đầu tiên': 0,
    'ảnh thứ 2': 1,
    'bức ảnh thứ 2': 1,
    'bức ảnh thứ hai': 1,
}


def detect_reference_expressions(text: str) -> list[str]:
    lower = text.lower()
    found: list[str] = []
    for phrase in REFERENCE_PHRASES:
        if phrase in lower:
            found.append(phrase)
    if re.search(r'ảnh\s+thứ\s*\d+', lower):
        found.append('ảnh thứ N')
    return list(dict.fromkeys(found))


def _get_ordered_image_ids(db: Session, conversation_id: str) -> list[str]:
    stmt = (
        select(ImageAsset.id)
        .where(ImageAsset.conversation_id == conversation_id)
        .order_by(asc(ImageAsset.created_at))
    )
    return [str(row[0]) for row in db.execute(stmt).all()]


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
            .limit(4)
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

    for phrase, index in ORDINAL_PATTERNS.items():
        if phrase in lower and index < len(ordered_ids):
            results.append(
                ResolutionResult(
                    expression=phrase,
                    resolution_type='ordinal_image',
                    resolved_image_id=ordered_ids[index],
                    confidence=0.92,
                    payload={'strategy': 'ordinal_lookup', 'index': index + 1},
                )
            )

    m = re.search(r'ảnh\s+thứ\s*(\d+)', lower)
    if m:
        index = max(1, int(m.group(1))) - 1
        if index < len(ordered_ids):
            results.append(
                ResolutionResult(
                    expression=m.group(0),
                    resolution_type='ordinal_image',
                    resolved_image_id=ordered_ids[index],
                    confidence=0.92,
                    payload={'strategy': 'ordinal_lookup', 'index': index + 1},
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
