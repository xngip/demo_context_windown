import json
from typing import Any, Iterator

from google import genai
from google.genai import types

from app.config import get_settings
from app.utils import compact_text, safe_json_loads


class GeminiService:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.client = genai.Client(api_key=self.settings.gemini_api_key)

    def _generate_json(self, prompt: str, schema: dict[str, Any], parts: list[Any] | None = None) -> dict[str, Any]:
        contents = [prompt]
        if parts:
            contents.extend(parts)
        response = self.client.models.generate_content(
            model=self.settings.generation_model,
            contents=contents,
            config={
                'response_mime_type': 'application/json',
                'response_json_schema': schema,
                'temperature': 0.5,
            },
        )
        parsed = getattr(response, 'parsed', None)
        if parsed is not None:
            if isinstance(parsed, dict):
                return parsed
            return json.loads(json.dumps(parsed, default=str))
        return safe_json_loads(getattr(response, 'text', '{}'), default={}) or {}

    def embed_text(self, text: str) -> list[float]:
        response = self.client.models.embed_content(
            model=self.settings.embedding_model,
            contents=[text],
            config=types.EmbedContentConfig(output_dimensionality=self.settings.embedding_dim),
        )
        embeddings = getattr(response, 'embeddings', None) or []
        if not embeddings:
            return [0.0] * self.settings.embedding_dim
        first = embeddings[0]
        values = getattr(first, 'values', None)
        if values is None and isinstance(first, dict):
            values = first.get('values')
        if not values:
            return [0.0] * self.settings.embedding_dim
        return list(values)

    def file_part_from_path(self, file_path: str, mime_type: str):
        if 'wordprocessingml.document' in mime_type:
            try:
                import docx
                doc = docx.Document(file_path)
                full_text = [para.text for para in doc.paragraphs]
                return types.Part.from_text(text='\n'.join(full_text))
            except Exception as e:
                return types.Part.from_text(text=f'[Lỗi đọc file DOCX: {str(e)}]')

        with open(file_path, 'rb') as f:
            file_bytes = f.read()
        return types.Part.from_bytes(data=file_bytes, mime_type=mime_type)

    def analyze_image(self, file_path: str, mime_type: str) -> dict[str, Any]:
        schema = {
            'type': 'object',
            'required': ['short_caption', 'detailed_caption', 'ocr_text', 'ocr_text_compressed', 'tags', 'image_type', 'visual_summary'],
            'properties': {
                'short_caption': {'type': 'string'},
                'detailed_caption': {'type': 'string'},
                'ocr_text': {'type': 'string'},
                'ocr_text_compressed': {'type': 'string'},
                'tags': {'type': 'array', 'items': {'type': 'string'}},
                'image_type': {'type': 'string'},
                'visual_summary': {'type': 'string'},
                'entities': {
                    'type': 'array',
                    'items': {
                        'type': 'object',
                        'properties': {
                            'name': {'type': 'string'},
                            'kind': {'type': 'string'},
                        },
                        'required': ['name', 'kind'],
                    },
                },
            },
        }
        prompt = (
            'Phân tích ảnh cho hệ thống quản lý ngữ cảnh đa phương thức. '
            'Trả về JSON với caption ngắn, caption chi tiết, OCR đầy đủ, OCR rút gọn, tags, image_type, visual_summary, entities. '
            'image_type chỉ chọn một giá trị gần nhất trong nhóm: dashboard, screenshot, document, photo, chart, ui, receipt, other. '
            'OCR rút gọn phải giữ lại các chữ, số, tên riêng và từ khóa quan trọng nhất.'
        )
        result = self._generate_json(prompt, schema, parts=[self.file_part_from_path(file_path, mime_type)])
        result['ocr_text'] = compact_text(result.get('ocr_text', ''), max_chars=4000)
        result['ocr_text_compressed'] = compact_text(result.get('ocr_text_compressed', ''), max_chars=self.settings.max_ocr_chars)
        result['textual_memory'] = '\n'.join([
            result.get('short_caption', ''),
            result.get('detailed_caption', ''),
            f"OCR: {result.get('ocr_text_compressed', '')}",
            f"Tags: {', '.join(result.get('tags', []))}",
        ]).strip()
        result['embedding'] = self.embed_text(result['textual_memory'])
        return result

    def analyze_document(self, file_path: str, mime_type: str) -> dict[str, Any]:
        schema = {
            'type': 'object',
            'required': ['summary', 'extracted_text', 'tags'],
            'properties': {
                'summary': {'type': 'string'},
                'extracted_text': {'type': 'string'},
                'tags': {'type': 'array', 'items': {'type': 'string'}},
                'entities': {
                    'type': 'array',
                    'items': {
                        'type': 'object',
                        'properties': {
                            'name': {'type': 'string'},
                            'kind': {'type': 'string'},
                        },
                        'required': ['name', 'kind'],
                    },
                },
            },
        }
        prompt = (
            'Phân tích tài liệu cho hệ thống quản lý ngữ cảnh đa phương thức. '
            'Trả về JSON với: summary (tóm tắt ý chính tài liệu), extracted_text (nội dung text, ưu tiên giữ thông tin quan trọng rút gọn nếu quá dài), '
            'tags (các từ khóa), entities (thực thể quan trọng có name, kind). '
        )
        result = self._generate_json(prompt, schema, parts=[self.file_part_from_path(file_path, mime_type)])
        result['extracted_text'] = compact_text(result.get('extracted_text', ''), max_chars=8000)
        result['textual_memory'] = '\n'.join([
            f"Document Summary: {result.get('summary', '')}",
            f"Text content: {result.get('extracted_text', '')}",
            f"Tags: {', '.join(result.get('tags', []))}",
        ]).strip()
        result['embedding'] = self.embed_text(result['textual_memory'])
        return result

    def summarize_turn(self, user_text: str, answer_text: str) -> str:
        schema = {
            'type': 'object',
            'required': ['summary'],
            'properties': {'summary': {'type': 'string'}},
        }
        prompt = (
            'Tóm tắt ngắn gọn một lượt hội thoại cho long term memory. '
            'Giữ các dữ kiện quan trọng, task hiện tại, ảnh được nhắc tới và kết luận đã trả lời.\n\n'
            f'User: {user_text}\nAssistant: {answer_text}'
        )
        result = self._generate_json(prompt, schema)
        return result.get('summary', '')

    def update_working_memory(
        self,
        previous_memory: dict[str, Any],
        user_text: str,
        assistant_answer: str,
        image_summaries: list[dict[str, Any]],
        resolved_references: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        schema = {
            'type': 'object',
            'required': [
                'user_goal', 'current_task', 'current_focus', 'active_image_ids',
                'constraints', 'decisions', 'unresolved_questions', 'summary_buffer'
            ],
            'properties': {
                'user_goal': {'type': 'string'},
                'current_task': {'type': 'string'},
                'current_focus': {'type': 'object'},
                'active_image_ids': {'type': 'array', 'items': {'type': 'string'}},
                'constraints': {'type': 'array', 'items': {'type': 'string'}},
                'decisions': {'type': 'array', 'items': {'type': 'string'}},
                'unresolved_questions': {'type': 'array', 'items': {'type': 'string'}},
                'summary_buffer': {'type': 'string'},
            },
        }
        prompt = (
            'Cập nhật working memory cho hệ thống hội thoại đa phương thức sau khi assistant đã trả lời. '
            'Chỉ giữ unresolved_questions là những câu hỏi thực sự CHƯA được giải quyết trong câu trả lời hiện tại. '
            'Hãy gọn, không nhắc lại toàn bộ lịch sử. current_focus nên chỉ rõ focus_type, primary_image_ids và lý do tập trung hiện tại nếu có. '
            'decisions chỉ nên giữ các kết luận đã chốt.\n\n'
            f'Previous memory: {json.dumps(previous_memory, ensure_ascii=False)}\n\n'
            f'New user text: {user_text}\n\n'
            f'Assistant answer: {assistant_answer}\n\n'
            f'Resolved references: {json.dumps(resolved_references or [], ensure_ascii=False)}\n\n'
            f'Image summaries: {json.dumps(image_summaries, ensure_ascii=False)}'
        )
        return self._generate_json(prompt, schema)

    def answer(self, prompt: str, image_parts: list[Any] | None = None) -> str:
        contents = [prompt]
        if image_parts:
            contents.extend(image_parts)
        response = self.client.models.generate_content(
            model=self.settings.generation_model,
            contents=contents,
            config=types.GenerateContentConfig(temperature=0.5),
        )
        return getattr(response, 'text', '').strip()

    def stream_answer(self, prompt: str, image_parts: list[Any] | None = None) -> Iterator[str]:
        contents = [prompt]
        if image_parts:
            contents.extend(image_parts)
        try:
            stream = self.client.models.generate_content_stream(
                model=self.settings.generation_model,
                contents=contents,
                config=types.GenerateContentConfig(temperature=0.5),
            )
            for chunk in stream:
                text = getattr(chunk, 'text', '') or ''
                if text:
                    yield text
        except Exception:
            answer = self.answer(prompt=prompt, image_parts=image_parts)
            if answer:
                yield answer
