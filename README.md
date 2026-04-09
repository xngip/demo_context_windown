# Multimodal Context Window Demo Final

## 1. Cấu trúc project

```text
multimodal_context_demo_final/
├── app/
│   ├── config.py
│   ├── db.py
│   ├── main.py
│   ├── models.py
│   ├── schemas.py
│   ├── utils.py
│   ├── services/
│   │   ├── chat_service.py
│   │   ├── gemini_service.py
│   │   ├── memory_manager.py
│   │   ├── resolvers.py
│   │   └── retrieval.py
│   └── static/
│       ├── index.html
│       ├── app.js
│       └── styles.css
├── data/uploads/
├── .env.example
├── .env.docker.example
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── README.md
```

## 2. Chạy theo cách khuyên dùng

Cách dễ nhất là
- app chạy local bằng virtualenv
- PostgreSQL cộng pgvector chạy bằng Docker ở cổng 5434

### Bước 1. Giải nén và vào thư mục project

Windows PowerShell

```powershell
cd C:\Users\Admin\Downloads
Expand-Archive .\multimodal_context_demo_final.zip -DestinationPath .\multimodal_context_demo_final
cd .\multimodal_context_demo_final
```

Nếu bạn đã có folder rồi thì chỉ cần cd vào folder đó.

### Bước 2. Tạo virtualenv

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

Nếu PowerShell chặn activate thì chạy tạm

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.venv\Scripts\Activate.ps1
```

### Bước 3. Cài thư viện

```powershell
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### Bước 4. Tạo file env cho local app

```powershell
copy .env.example .env
notepad .env
```

Điền Gemini key mới của bạn vào file `.env`.

Lưu ý rất quan trọng
- Bạn đã từng làm lộ Gemini key cũ trong chat
- Hãy rotate xóa key cũ và dùng key mới

`.env.example` mặc định đã trỏ tới Docker DB local ở cổng 5434 nên thường không phải sửa database url nữa.

### Bước 5. Chạy PostgreSQL cộng pgvector bằng Docker

```powershell
docker compose down -v
docker compose up db -d
```

Project này map DB ra cổng `5434` để tránh đụng PostgreSQL local cài sẵn trên máy.

Kiểm tra log DB nếu cần

```powershell
docker compose logs db
```

### Bước 6. Chạy app

```powershell
uvicorn app.main:app --reload
```

Nếu thành công bạn sẽ thấy dòng tương tự

```text
Application startup complete.
```

### Bước 7. Mở web demo

```text
http://127.0.0.1:8000
```

## 3. Chạy full Docker cả app lẫn DB

Nếu muốn chạy toàn bộ bằng Docker Compose thì tạo file `.env.docker` từ mẫu.

```powershell
copy .env.docker.example .env.docker
notepad .env.docker
```

Sau đó chạy

```powershell
docker compose up --build
```

Lúc này app container sẽ dùng database host là `db` đúng như `.env.docker.example`.

## 4. Các endpoint chính

### Tạo conversation

```bash
curl -X POST http://127.0.0.1:8000/conversations
```

### Gửi text và ảnh

```bash
curl -X POST \
  http://127.0.0.1:8000/conversations/<CONVERSATION_ID>/chat \
  -F "text=tóm tắt ảnh này" \
  -F "images=@/absolute/path/to/image.png"
```

### Xem memory snapshot

```bash
curl http://127.0.0.1:8000/conversations/<CONVERSATION_ID>/memory
```


## 4b. Chế độ Nano Banana 2

### Tạo ảnh mới hoặc chỉnh ảnh từ lịch sử hội thoại

```bash
curl -X POST   http://127.0.0.1:8000/conversations/<CONVERSATION_ID>/images/generate   -F "text=tạo poster cyberpunk với mèo phi hành gia"
```

### Chỉnh ảnh bằng ảnh upload mới làm nguồn

```bash
curl -X POST   http://127.0.0.1:8000/conversations/<CONVERSATION_ID>/images/generate   -F "text=giữ bố cục chính nhưng đổi nền thành hoàng hôn và làm kiểu poster"   -F "files=@/absolute/path/to/image.png"
```

### Chỉnh lại ảnh cũ qua context window

Ví dụ sau khi user đã gửi 5 ảnh ở 5 turn khác nhau hoặc đã có nhiều vòng chỉnh sửa, có thể gọi

```bash
curl -X POST   http://127.0.0.1:8000/conversations/<CONVERSATION_ID>/images/generate   -F "text=sửa ảnh 2, giữ chủ thể chính nhưng đổi thành phong cách tối giản"
```

Hoặc nếu đã có một chuỗi edit và user muốn quay lại bản thứ 3

```bash
curl -X POST   http://127.0.0.1:8000/conversations/<CONVERSATION_ID>/images/generate   -F "text=sửa ảnh 3 theo phong cách cinematic, giữ gương mặt như cũ"
```

## 5. Hành vi tối ưu mới

### Khi không có ảnh
- Không gọi analyze image
- Không OCR ảnh
- Không chờ image embedding
- Trả lời theo fast text path
- Summary và refine working memory vẫn đi nền

### Khi có ảnh mới
- Ảnh được lưu nhanh vào disk và DB
- Assistant trả lời ngay bằng ảnh attach trực tiếp
- Phân tích ảnh caption OCR tags embedding chạy ở nền
- Memory snapshot và image metadata sẽ đầy đủ hơn sau một lúc ngắn


### Khi bật chế độ Nano Banana 2
- Có nút mode riêng ở ô nhập để chuyển giữa Chat và Nano Banana 2
- Ở mode này backend luôn sẵn sàng cho generate hoặc edit ảnh
- Nếu user upload ảnh mới thì ảnh đó được ưu tiên làm nguồn chỉnh sửa
- Nếu user không upload ảnh nhưng yêu cầu như `sửa ảnh 2` hoặc `sửa ảnh 3` thì resolver sẽ cố resolve ảnh theo lịch sử hội thoại và lineage chỉnh sửa
- Ảnh do assistant tạo ra được lưu lại như image asset bình thường để các turn sau có thể tiếp tục tham chiếu

### Khi câu hỏi cần so sánh trực quan
Các trigger như bố cục layout màu sắc giao diện cấu trúc biểu đồ sẽ làm hệ thống rehydrate ảnh cũ vào prompt nếu resolve được ảnh mục tiêu.

## 6. Những file đáng xem nhất

- `app/services/chat_service.py`
  - luồng xử lý chính
  - fast path
  - background enrichment

- `app/services/memory_manager.py`
  - fast working memory
  - normalize working memory
  - snapshot

- `app/services/resolvers.py`
  - temporal resolver
  - reference resolver
  - ordinal image resolver

- `app/services/retrieval.py`
  - hybrid retrieval
  - ưu tiên context của ảnh đã resolve

- `app/static/app.js`
  - optimistic UI
  - pending bubble
  - auto refresh khi background enrichment đang chạy

## 7. Gợi ý test đúng bài toán

1. Tải ảnh mới và hỏi
- tóm tắt ảnh này
- mô tả ảnh này
- đọc chữ trong ảnh này

2. Tải nhiều ảnh rồi hỏi
- cho tôi thêm thông tin về bức ảnh thứ 2
- ảnh đầu tiên mô tả gì
- ảnh trước đó là gì

3. Hỏi tham chiếu thời gian
- ảnh hôm qua là gì
- ảnh thứ 2 tuần trước có phải dashboard doanh thu không

4. Hỏi so sánh trực quan
- so sánh bố cục ảnh này với ảnh trước đó

## 8. Lưu ý thực tế

- Vì background enrichment chạy sau khi đã trả lời nên một số metadata như OCR rút gọn tags long term memory có thể cập nhật chậm hơn câu trả lời đầu tiên một chút.
- Điều này là chủ đích để giảm latency cho người dùng.
- Web UI sẽ tự làm mới khi thấy background enrichment đang chạy.

## 9. Nếu gặp lỗi thường gặp

### Lỗi thiếu Gemini key
Kiểm tra `.env`

```env
GEMINI_API_KEY=KEY_MOI_CUA_BAN
```

### Lỗi không kết nối DB local app
Kiểm tra `.env`

```env
DATABASE_URL=postgresql+psycopg://demo:demo@localhost:5434/multimodal_memory
```

### Lỗi DB cũ giữ credential sai
Chạy lại sạch volume

```powershell
docker compose down -v
docker compose up db -d
```

### Lỗi `/` 404
Bản final này đã có web UI ở `/` nên nếu chạy đúng app bạn sẽ không còn gặp 404 ở root nữa.

---

Nếu bạn muốn nâng tiếp lên production sau bản này thì bước hợp lý nhất là thêm queue riêng cho background enrichment và thêm streaming token cho câu trả lời realtime.

## 10. Gợi ý chạy

Sau khi sửa `.env` và khởi động DB như hướng dẫn ở trên, chỉ cần chạy

```powershell
uvicorn app.main:app --reload
```

Rồi mở

```text
http://127.0.0.1:8000
```

Bạn sẽ thấy câu trả lời được stream ra trực tiếp trên giao diện.
