# Multimodal Context Window Demo (Gemini + PostgreSQL + pgvector)

Demo này bám sát thiết kế quản lý cửa sổ ngữ cảnh đa phương thức trong tài liệu của bạn:
- recent memory
- working memory
- long-term memory
- temporal resolver
- reference resolver
- image dehydrate / rehydrate
- hybrid retrieval bằng PostgreSQL + pgvector
- prompt builder theo ngữ cảnh hiện tại

## 1. Stack

- FastAPI
- PostgreSQL + pgvector
- SQLAlchemy
- Gemini API qua Google GenAI SDK
- Local file storage cho ảnh demo

## 2. Gemini đang được dùng như thế nào

Project dùng Gemini cho 4 việc:
1. tạo câu trả lời cuối cùng
2. phân tích ảnh: caption, OCR, tags, image type
3. cập nhật working memory
4. tạo embeddings để lưu vào pgvector

Biến môi trường được SDK tự nhận là `GEMINI_API_KEY`.

## 3. Cấu trúc thư mục

```text
multimodal_context_demo_gemini/
├── app/
│   ├── config.py
│   ├── db.py
│   ├── main.py
│   ├── models.py
│   ├── schemas.py
│   ├── utils.py
│   └── services/
│       ├── chat_service.py
│       ├── gemini_service.py
│       ├── memory_manager.py
│       ├── resolvers.py
│       └── retrieval.py
├── data/uploads/
├── .env.example
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── README.md
```

## 4. Chạy bằng Docker

### Bước 1

Tạo file `.env` từ mẫu:

```bash
cp .env.example .env
```

Sau đó điền `GEMINI_API_KEY` thật.

### Bước 2

Chạy toàn bộ hệ thống:

```bash
docker compose up --build
```

API sẽ chạy ở:

```text
http://localhost:8000
```

Swagger:

```text
http://localhost:8000/docs
```

## 5. Chạy local không cần Docker app

Bạn vẫn có thể dùng Docker chỉ cho PostgreSQL:

```bash
docker compose up db -d
```

Sau đó chạy app local:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload
```

## 6. API chính

### 6.1 Tạo conversation

```bash
curl -X POST http://localhost:8000/conversations
```

Ví dụ response:

```json
{
  "conversation_id": "...",
  "title": "Multimodal Context Demo",
  "created_at": "2026-04-01T..."
}
```

### 6.2 Gửi chat text + ảnh

```bash
curl -X POST \
  http://localhost:8000/conversations/<CONVERSATION_ID>/chat \
  -F 'text=Phân tích ảnh này giúp tôi' \
  -F 'images=@/absolute/path/to/dashboard.png'
```

Ví dụ hỏi tiếp:

```bash
curl -X POST \
  http://localhost:8000/conversations/<CONVERSATION_ID>/chat \
  -F 'text=So sánh với ảnh trước đó'
```

Ví dụ temporal/reference query:

```bash
curl -X POST \
  http://localhost:8000/conversations/<CONVERSATION_ID>/chat \
  -F 'text=Ảnh hôm qua có phải dashboard doanh thu quý không?'
```

Ví dụ rehydrate trực quan:

```bash
curl -X POST \
  http://localhost:8000/conversations/<CONVERSATION_ID>/chat \
  -F 'text=So sánh bố cục ảnh hiện tại với ảnh tôi gửi thứ 2 tuần trước'
```

### 6.3 Xem memory snapshot

```bash
curl http://localhost:8000/conversations/<CONVERSATION_ID>/memory
```

## 7. Mapping với thiết kế trong tài liệu

### Recent Memory
- lấy `MAX_RECENT_TURNS` turn gần nhất từ `conversation_turns`
- dùng khi build prompt

### Working Memory
- bảng `conversation_working_memory`
- lưu:
  - user_goal
  - current_task
  - current_focus
  - active_image_ids
  - constraints
  - decisions
  - unresolved_questions
  - summary_buffer

### Long-term Memory
- bảng `memory_items`
- lưu:
  - turn summaries
  - image textual memory
  - metadata theo thời gian
  - vector embeddings bằng pgvector

### Image Understanding
- bảng `image_understanding`
- lưu:
  - short_caption
  - detailed_caption
  - ocr_text
  - ocr_text_compressed
  - tags
  - entities
  - visual_summary
  - dehydrate_payload
  - embedding

### Resolver
- `resolvers.py`
- hỗ trợ:
  - ảnh này
  - ảnh trước đó
  - dashboard cũ
  - hôm qua
  - hôm kia
  - tuần trước
  - thứ 2 tuần trước

### Retrieval
- `retrieval.py`
- semantic retrieval qua pgvector
- keyword retrieval qua caption/OCR/content ILIKE
- alias lookup
- hybrid score hợp nhất

### Dehydrate / Rehydrate
- ảnh luôn được lưu local path trong `images.storage_uri`
- textual memory lưu trong `image_understanding` + `memory_items`
- nếu câu hỏi có tín hiệu trực quan như `bố cục`, `layout`, `màu sắc`, hệ thống nạp lại file ảnh cũ vào prompt

## 8. Các endpoint / file quan trọng

- `app/main.py`: API entrypoint
- `app/services/chat_service.py`: luồng xử lý chính
- `app/services/gemini_service.py`: gọi Gemini
- `app/services/retrieval.py`: retrieval hybrid
- `app/services/resolvers.py`: temporal/reference resolution
- `app/services/memory_manager.py`: recent/working/long-term memory
- `app/models.py`: schema PostgreSQL + pgvector

## 9. Gợi ý mở rộng

Nếu bạn muốn nâng cấp từ demo lên production, nên thêm:
- Alembic migrations
- object storage S3/MinIO thay local disk
- queue cho image pipeline
- image thumbnails / resize pipeline
- FTS chuẩn hơn bằng tsvector generated column
- eval suite cho reference resolution
- batch embedding
- auth và multi-user

## 10. Lưu ý thực tế

- Demo này ưu tiên sự rõ ràng và chạy được.
- Gemini đang được dùng trực tiếp để OCR/captioning, nên chất lượng phụ thuộc model và file ảnh thực tế.
- Embedding mặc định đang để `gemini-embedding-2-preview` với `EMBEDDING_DIM=768` để phù hợp demo đa phương thức và giảm chi phí/vector size. Bạn có thể đổi model trong `.env`.

