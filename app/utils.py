import hashlib
import json
import mimetypes
import os
import re
import uuid
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


def ensure_dir(path: str) -> None:
    Path(path).mkdir(parents=True, exist_ok=True)


def sha256_of_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            h.update(chunk)
    return h.hexdigest()


def guess_mime_type(filename: str) -> str:
    mime, _ = mimetypes.guess_type(filename)
    return mime or 'application/octet-stream'


def save_upload_bytes(base_dir: str, conversation_id: str, original_name: str, content: bytes) -> str:
    ensure_dir(base_dir)
    target_dir = Path(base_dir) / conversation_id
    target_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(original_name).suffix or '.bin'
    file_name = f'{uuid.uuid4()}{suffix}'
    file_path = target_dir / file_name
    file_path.write_bytes(content)
    return str(file_path)


def compact_text(text: str | None, max_chars: int = 1200) -> str:
    if not text:
        return ''
    normalized = re.sub(r'\s+', ' ', text).strip()
    return normalized[:max_chars]


def safe_json_loads(value: str, default: dict | list | None = None):
    try:
        return json.loads(value)
    except Exception:
        return default


def utcnow() -> datetime:
    return datetime.utcnow()


def start_of_day(dt: datetime) -> datetime:
    return datetime.combine(dt.date(), time.min, tzinfo=dt.tzinfo)


def end_of_day(dt: datetime) -> datetime:
    return datetime.combine(dt.date(), time.max, tzinfo=dt.tzinfo)


WEEKDAY_VI = {
    'thứ 2': 0,
    'thứ 3': 1,
    'thứ 4': 2,
    'thứ 5': 3,
    'thứ 6': 4,
    'thứ 7': 5,
    'chủ nhật': 6,
}


def resolve_temporal_expression(expr: str, timezone_name: str = 'Asia/Bangkok') -> dict | None:
    tz = ZoneInfo(timezone_name)
    now = datetime.now(tz)
    expr_norm = expr.strip().lower()

    if expr_norm == 'hôm qua':
        target = now - timedelta(days=1)
        return {
            'expression': expr,
            'start_time': start_of_day(target),
            'end_time': end_of_day(target),
            'granularity': 'day',
            'confidence': 0.99,
        }

    if expr_norm == 'hôm kia':
        target = now - timedelta(days=2)
        return {
            'expression': expr,
            'start_time': start_of_day(target),
            'end_time': end_of_day(target),
            'granularity': 'day',
            'confidence': 0.99,
        }

    if expr_norm == 'tuần trước':
        this_monday = start_of_day(now - timedelta(days=now.weekday()))
        last_monday = this_monday - timedelta(days=7)
        last_sunday = last_monday + timedelta(days=6)
        return {
            'expression': expr,
            'start_time': last_monday,
            'end_time': end_of_day(last_sunday),
            'granularity': 'week',
            'confidence': 0.98,
        }

    m = re.search(r'(thứ\s*[2-7]|chủ nhật)\s+tuần trước', expr_norm)
    if m:
        key = m.group(1)
        if key in WEEKDAY_VI:
            this_monday = start_of_day(now - timedelta(days=now.weekday()))
            last_monday = this_monday - timedelta(days=7)
            target = last_monday + timedelta(days=WEEKDAY_VI[key])
            return {
                'expression': expr,
                'start_time': start_of_day(target),
                'end_time': end_of_day(target),
                'granularity': 'day',
                'confidence': 0.98,
            }

    return None


def needs_visual_rehydration(text: str) -> bool:
    text = text.lower()
    triggers = [
        'bố cục', 'layout', 'màu', 'màu sắc', 'giao diện', 'cấu trúc biểu đồ',
        'chart', 'widget', 'vị trí', 'khác nhau ở đâu', 'so sánh trực quan'
    ]
    return any(t in text for t in triggers)
