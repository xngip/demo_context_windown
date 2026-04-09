import base64
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
                'temperature': 0.1,
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

    def _extract_response_parts(self, response: Any) -> list[Any]:
        direct_parts = getattr(response, 'parts', None)
        if direct_parts:
            return list(direct_parts)

        parts: list[Any] = []
        for candidate in getattr(response, 'candidates', None) or []:
            content = getattr(candidate, 'content', None)
            candidate_parts = getattr(content, 'parts', None) if content is not None else None
            if candidate_parts:
                parts.extend(candidate_parts)
        return parts

    def _extract_part_text(self, part: Any) -> str | None:
        text = getattr(part, 'text', None)
        if text:
            return str(text)
        if isinstance(part, dict):
            value = part.get('text')
            return str(value) if value else None
        return None

    def _extract_part_inline_data(self, part: Any) -> tuple[bytes | None, str | None]:
        inline_data = getattr(part, 'inline_data', None) or getattr(part, 'inlineData', None)
        if inline_data is None and isinstance(part, dict):
            inline_data = part.get('inline_data') or part.get('inlineData')
        if inline_data is None:
            return None, None

        data = getattr(inline_data, 'data', None)
        mime_type = getattr(inline_data, 'mime_type', None) or getattr(inline_data, 'mimeType', None)
        if isinstance(inline_data, dict):
            data = data or inline_data.get('data')
            mime_type = mime_type or inline_data.get('mime_type') or inline_data.get('mimeType')
        if not data:
            return None, mime_type
        if isinstance(data, str):
            try:
                data = base64.b64decode(data)
            except Exception:
                data = data.encode('utf-8')
        return data, mime_type

    def analyze_image(self, file_path: str, mime_type: str) -> dict[str, Any]:
        schema = {
            'type': 'object',
            'required': ['short_caption', 'ocr_text_compressed', 'tags', 'image_type'],
            'properties': {
                'short_caption': {'type': 'string'},
                'detailed_caption': {'type': 'string'},
                'ocr_text': {'type': 'string'},
                'ocr_text_compressed': {'type': 'string'},
                'tags': {'type': 'array', 'items': {'type': 'string'}},
                'image_type': {'type': 'string'},
                'visual_summary': {'type': 'string'},
            },
        }
        prompt = (
            'Analyze the provided image and return a JSON response.\n'
            '- short_caption: A concise description.\n'
            '- ocr_text_compressed: Key text visible in the image.\n'
            '- tags: Keywords.\n'
            '- image_type: dashboard, screenshot, document, photo, chart, ui, receipt, other.'
        )
        result = self._generate_json(prompt, schema, parts=[self.file_part_from_path(file_path, mime_type)])
        result['ocr_text_compressed'] = compact_text(result.get('ocr_text_compressed', ''), max_chars=self.settings.max_ocr_chars)
        result['textual_memory'] = '\n'.join([
            result.get('short_caption', ''),
            f"OCR: {result.get('ocr_text_compressed', '')}",
            f"Tags: {', '.join(result.get('tags', []))}",
        ]).strip()
        result['embedding'] = self.embed_text(result['textual_memory'])
        return result

    def batch_analyze_images(self, image_jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Analyze multiple images in a single LLM call for better performance."""
        if not image_jobs:
            return []
            
        schema = {
            'type': 'object',
            'required': ['analyses'],
            'properties': {
                'analyses': {
                    'type': 'array',
                    'items': {
                        'type': 'object',
                        'required': ['image_id', 'short_caption', 'ocr_text_compressed', 'tags', 'image_type'],
                        'properties': {
                            'image_id': {'type': 'string'},
                            'short_caption': {'type': 'string'},
                            'ocr_text_compressed': {'type': 'string'},
                            'tags': {'type': 'array', 'items': {'type': 'string'}},
                            'image_type': {'type': 'string'},
                        }
                    }
                }
            }
        }
        
        prompt = (
            'You are an expert image analyst. Analyze the provided images and return a list of JSON analyses.\n'
            'For each image (identified by its provided ID), provide:\n'
            '- image_id: The exact ID provided for this image.\n'
            '- short_caption: A concise one-sentence description.\n'
            '- ocr_text_compressed: Key facts, numbers, and headings from any text in the image.\n'
            '- tags: Relevant keywords.\n'
            '- image_type: Choose from: dashboard, screenshot, document, photo, chart, ui, receipt, other.'
        )
        
        parts = []
        for job in image_jobs:
            parts.append(types.Part.from_text(text=f"Image ID: {job['image_id']}"))
            parts.append(self.file_part_from_path(job['file_path'], job['mime_type']))
            
        response_data = self._generate_json(prompt, schema, parts=parts)
        analyses = response_data.get('analyses', [])
        
        # Post-process results (add embeddings and memory text)
        for analysis in analyses:
            analysis['ocr_text_compressed'] = compact_text(analysis.get('ocr_text_compressed', ''), max_chars=self.settings.max_ocr_chars)
            analysis['textual_memory'] = '\n'.join([
                analysis.get('short_caption', ''),
                f"OCR: {analysis.get('ocr_text_compressed', '')}",
                f"Tags: {', '.join(analysis.get('tags', []))}",
            ]).strip()
            analysis['embedding'] = self.embed_text(analysis['textual_memory'])
            
        return analyses

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
            'You are an expert document analyst. Read the provided document carefully and analyze it thoroughly.\n'
            '- summary: A concise summary of the main points, core arguments, and key conclusions (3-6 sentences).\n'
            '- extracted_text: Extract the most important content — prioritize preserving key passages, statistics, definitions, and conclusions. Condense if the document is very long.\n'
            '- tags: Up to 10 keywords reflecting the topic, domain, and main content.\n'
            '- entities: Important entities found in the document (people, organizations, locations, products...) with name and kind.'
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
            'Summarize this conversation turn for long-term memory storage. Requirements:\n'
            '- Extract specific facts, conclusions reached, and the current task status.\n'
            '- Note if any images were generated, edited, or analyzed.\n'
            '- Record the user\'s question and the key points of the assistant\'s answer.\n'
            '- The summary must be self-contained — someone reading it later should fully understand this turn without seeing the original conversation.\n'
            '- Maximum 3 sentences, concise and accurate.\n\n'
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
            'Update the working memory after the assistant has responded. Rules:\n'
            '- user_goal: The user\'s overall goal for this session (do not change unless there is a clear topic shift).\n'
            '- current_task: The most specific task the user just requested or is waiting on.\n'
            '- current_focus: Clearly state focus_type, primary_image_ids if working with images, and keep the focus reason brief.\n'
            '- active_image_ids: List of image IDs currently being referenced or just generated, prioritizing the most recent ones.\n'
            '- constraints: Explicit constraints the user has stated (style, format, limits...).\n'
            '- decisions: Keep only conclusions that have been confirmed and finalized — do not repeat already-resolved items.\n'
            '- unresolved_questions: ONLY list questions or requests that the current answer has NOT yet resolved.\n'
            '- summary_buffer: A brief summary of conversation progress so far (maximum 3 sentences).\n\n'
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

    def resolve_image_references(
        self,
        user_text: str,
        recent_history: list[dict],
        image_catalog: list[dict],
        current_image_ids: list[str],
    ) -> dict[str, Any]:
        schema = {
            'type': 'object',
            'required': ['selected_image_ids', 'reasoning'],
            'properties': {
                'selected_image_ids': {'type': 'array', 'items': {'type': 'string'}},
                'reasoning': {'type': 'string'},
            },
        }
        prompt = (
            "You are an expert image reference resolver for a multimodal chat system.\n"
            "Your goal is to identify which image IDs from the 'Image Catalog' the user is referring to in their 'User Request'.\n\n"

            "INPUT DATA:\n"
            f"1. User Request: {user_text}\n"
            f"2. Current Turn Images: {json.dumps(current_image_ids)}\n"
            f"3. Recent Chat History: {json.dumps(recent_history, ensure_ascii=False)}\n"
            f"4. Image Catalog: {json.dumps(image_catalog, ensure_ascii=False)}\n\n"

            "RULES:\n"
            "- Analyze the chat history and the catalog carefully. The user might describe the image content, its origin (e.g., 'the image you created'), its timing (e.g., 'the first one'), or its relationship to others (e.g., 'the edited version').\n"
            "- Only select image IDs that are explicitly or strongly implicitly requested. If the user refers to images just uploaded in the current turn (Current Turn Images), you may include them if relevant, but prioritize identifying historical images if described.\n"
            "- ALWAYS return a JSON object with 'selected_image_ids' (list of UUID strings) and 'reasoning' (brief Vietnamese explanation).\n"
            "- If no historical images are being referred to, return an empty list for 'selected_image_ids'.\n"
            "- ONLY use image IDs present in the Image Catalog.\n"
        )
        return self._generate_json(prompt, schema)

    def generate_or_edit_image(self, instruction: str, image_parts: list[Any] | None = None) -> dict[str, Any]:
        contents: list[Any] = [instruction]
        if image_parts:
            contents.extend(image_parts)

        response = self.client.models.generate_content(
            model=self.settings.image_generation_model,
            contents=contents,
            config=types.GenerateContentConfig(
                temperature=0.7,
                response_modalities=['TEXT', 'IMAGE'],
            ),
        )

        text_chunks: list[str] = []
        images: list[dict[str, Any]] = []
        for part in self._extract_response_parts(response):
            text = self._extract_part_text(part)
            if text:
                text_chunks.append(text.strip())
            data, mime_type = self._extract_part_inline_data(part)
            if data:
                images.append({
                    'bytes': data,
                    'mime_type': mime_type or 'image/png',
                })

        fallback_text = getattr(response, 'text', '') or ''
        if not text_chunks and fallback_text:
            text_chunks.append(fallback_text.strip())

        return {
            'text': '\n'.join(chunk for chunk in text_chunks if chunk).strip(),
            'images': images[: self.settings.max_generated_images_per_turn],
        }
