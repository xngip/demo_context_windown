# Multimodal Context Window Demo Final

Bản này giữ nguyên hướng thiết kế của project gốc bạn cung cấp nhưng tối ưu lại ở 4 điểm chính.

1. Trả lời nhanh hơn
- Nếu không có ảnh thì bỏ toàn bộ pipeline phân tích ảnh và đi theo fast text path.
- Nếu có ảnh mới thì assistant trả lời ngay bằng cách dùng trực tiếp ảnh đang attach vào request.
- Việc caption OCR tags embedding lưu long term memory và refine working memory được chạy tiếp ở nền.

2. Quản lý ngữ cảnh tốt hơn
- Có recent memory
- Có working memory nhanh để dùng ngay cho lượt hiện tại
- Có working memory refine lại ở nền sau khi đã có câu trả lời
- Có long term memory cho turn summary và image memory
- Có resolution log để debug việc resolve ảnh này ảnh trước đó ảnh đầu tiên ảnh thứ 2 hôm qua tuần trước

3. UI rõ hơn
- Có sidebar lịch sử hội thoại
- Có optimistic UI khi gửi tin nhắn
- Có pending bubble trong lúc chờ
- Có badge hiển thị latency processing mode resolve retrieve background enrichment
- Có auto refresh khi memory đang được enrich ở nền
- Có nút xem memory snapshot

4. Thay đổi ít nhất có thể
- Giữ FastAPI
- Giữ PostgreSQL cộng pgvector
- Giữ Gemini service
- Giữ cấu trúc app services static gần như cũ
- Không thêm thư viện ngoài bắt buộc

## 1. Những cải tiến chính so với bản trước

### A. Fast path cho trải nghiệm người dùng
Trước đây luồng là
- save ảnh
- analyze ảnh
- OCR
- tạo embedding
- update working memory bằng LLM
- summarize turn
- lưu DB
- rồi mới trả lời

Bản này đổi thành
- save ảnh và tạo placeholder nhanh
- resolve reference và lấy recent memory nhanh
- nếu có ảnh mới thì dùng trực tiếp ảnh đó để trả lời ngay
- trả response cho người dùng
- sau đó chạy background enrichment để phân tích ảnh lưu OCR caption embedding alias summary vào DB

Điểm này giúp giảm rõ thời gian chờ ở các câu như
- tóm tắt ảnh này
- mô tả ảnh này
- đọc chữ trong ảnh này

### B. Working memory gọn hơn
- Có fast working memory tạo bằng rule để dùng ngay
- Có refine working memory bằng Gemini sau khi assistant đã trả lời
- Giảm tình trạng unresolved questions bị phình và giữ lại cả những câu đã trả lời

### C. Resolver tốt hơn
Hiện hỗ trợ thêm
- ảnh này
- ảnh trước đó
- ảnh vừa rồi
- ảnh đầu tiên
- bức ảnh đầu tiên
- ảnh thứ 2
- bức ảnh thứ 2
- người ấy
- hôm qua
- hôm kia
- tuần trước
- thứ 2 tuần trước
- dashboard cũ

### D. Retrieval tốt hơn
- Ưu tiên context của ảnh đã resolve
- Alias search có filter theo conversation
- Fast mode sẽ giảm phụ thuộc semantic retrieval để trả lời sớm hơn

## 2. Cấu trúc project

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

## 3. Chạy theo cách khuyên dùng

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

## 4. Chạy full Docker cả app lẫn DB

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

## 5. Các endpoint chính

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

## 6. Hành vi tối ưu mới

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

### Khi câu hỏi cần so sánh trực quan
Các trigger như bố cục layout màu sắc giao diện cấu trúc biểu đồ sẽ làm hệ thống rehydrate ảnh cũ vào prompt nếu resolve được ảnh mục tiêu.

## 7. Những file đáng xem nhất

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

## 8. Gợi ý test đúng bài toán

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

## 9. Lưu ý thực tế

- Vì background enrichment chạy sau khi đã trả lời nên một số metadata như OCR rút gọn tags long term memory có thể cập nhật chậm hơn câu trả lời đầu tiên một chút.
- Điều này là chủ đích để giảm latency cho người dùng.
- Web UI sẽ tự làm mới khi thấy background enrichment đang chạy.
- Nếu bạn dùng PostgreSQL local của riêng bạn thì DB đó phải có pgvector. Nếu không có, hãy dùng Docker DB như hướng dẫn trên.

## 10. Nếu gặp lỗi thường gặp

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


## 8. Streaming token v2

Bản v2.1 thêm streaming token để người dùng thấy câu trả lời hiện dần ra theo thời gian thực.

### Web UI
- Nút Gửi trên giao diện web bây giờ mặc định dùng endpoint stream
- Khi assistant bắt đầu trả lời bạn sẽ thấy token hiện dần trong bong bóng chat
- Sau khi stream xong UI tự reload conversation để đồng bộ metadata latency retrieval resolve và trạng thái background enrichment

### API stream

Endpoint mới

```bash
curl -N -X POST \
  http://127.0.0.1:8000/conversations/<CONVERSATION_ID>/chat/stream \
  -H "Accept: text/event-stream" \
  -F "text=tóm tắt ảnh này" \
  -F "images=@/absolute/path/to/image.png"
```

Endpoint này trả về SSE event stream với các event chính
- meta
- token
- done
- error

### Khi nào nên dùng
- UI web dùng stream mặc định
- API `/chat` cũ vẫn giữ nguyên để tiện test nhanh hoặc tích hợp đơn giản

## 9. File thay đổi chính cho streaming

- `app/main.py` thêm endpoint stream
- `app/services/chat_service.py` thêm luồng prepare trước rồi stream token sau
- `app/services/gemini_service.py` thêm `stream_answer`
- `app/static/app.js` đọc SSE từ fetch stream và render token dần

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
