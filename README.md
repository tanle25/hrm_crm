# Content Forge v2

Pipeline multi-agent chuyển `URL -> nội dung HTML -> WooCommerce product/post` theo tài liệu [content-forge-v2-2026.md](/Users/tanle/Documents/agent/content-forge-v2-2026.md).

## Trạng thái hiện tại

- Pipeline thật đang chạy được end-to-end với FastAPI + LangGraph + RQ/Redis.
- Hỗ trợ `article`, `simple product`, `variable product`.
- Dùng local router OpenAI-compatible làm LLM chính; hiện tắt fallback provider theo cấu hình.
- Writer và Humanizer sinh HTML trực tiếp, không đi qua markdown để tiết kiệm token.
- Có upload ảnh, chèn ảnh vào thân bài, internal link, outbound link, Rank Math meta, schema cơ bản, auto tags.
- Có `seo_adjuster` để sửa lỗi SEO nhỏ sau QA mà không chạy lại toàn bộ writer.
- Có structured logging JSON và webhook khi job `completed`, `failed`, `duplicate`, `forced_publish`.
- Content pipeline hiện không dùng Chroma/RAG nội bộ.
- Có subsystem RAG kiến thức riêng để ingest URL vào Chroma cho tra cứu độc lập.

## Các agent chính

- `deduplicator`
- `fetcher`
- `extractor`
- `knowledge`
- `enricher`
- `planner`
- `image_selector`
- `media_uploader`
- `writer`
- `humanizer`
- `internal_linker`
- `qa`
- `seo_adjuster`
- `publisher`

## Kiến trúc hiện tại

- Luồng `content pipeline`: lấy URL nguồn, sinh nội dung HTML, publish lên WordPress/WooCommerce.
- Luồng `knowledge RAG`: lấy URL bất kỳ, extract các knowledge units sạch và lưu vào Chroma để truy vấn riêng.
- Hai luồng này tách biệt. Publish xong bài viết sẽ không tự đẩy vào Chroma, và writer cũng không tự query Chroma.

## Yêu cầu môi trường

- Python `3.12+`
- Redis nếu chạy `QUEUE_MODE=rq`
- WordPress + WooCommerce nếu muốn publish thật
- Local router OpenAI-compatible tại `ROUTER_BASE`

## Chạy local

```bash
python3 -m venv venv
. venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload
```

API mặc định:

- `GET /health`
- `POST /api/submit`
- `GET /api/job/{job_id}`
- `GET /api/dlq`
- `POST /api/dlq/{job_id}/retry`
- `POST /api/dlq/{job_id}/publish-anyway`
- `GET /api/stats`

API cho subsystem RAG:

- `POST /api/rag/ingest`
- `GET /api/rag/search`
- `GET /api/rag/source`
- `DELETE /api/rag/source`

## Chạy bằng Docker

```bash
docker-compose build
docker-compose up -d
```

Services chính:

- `fastapi`
- `worker`
- `dlq_worker`
- `redis`
- `rq_dashboard`
- `prometheus`

## Cấu hình chính

LLM:

- `LLM_PRIMARY_PROVIDER=router`
- `LLM_DISABLE_FALLBACKS=true`
- `ROUTER_BASE=http://localhost:8317/v1`
- `LLM_MODEL_EXTRACT_PLANNER=gpt-5.4-mini`
- `LLM_MODEL_WRITER=gpt-5.4`
- `LLM_MODEL_HUMANIZER=gpt-5.4`
- `LLM_MODEL_QA=gpt-5.4-mini`

WooCommerce:

- `WOO_DEFAULT_STATUS=draft`
- `WOO_DEFAULT_PRICE=99000`

Site thật được cấu hình trong UI `Website Manage` và lưu vào database:

- `url`
- `consumer_key`
- `consumer_secret`
- `username`
- `app_password`

Quan sát và tích hợp:

- `METRICS_ENABLED=true`
- `METRICS_PORT=8001`
- `WEBHOOK_URL=http://your-endpoint.example/webhook`

RAG / Chroma:

- `CHROMA_DIR=./data/chroma`
- `RAG` hiện chỉ dùng khi gọi API/CLI ingest/search riêng, không tham gia publish pipeline

## Kiểm tra nhanh

Contract tests:

```bash
venv/bin/python scripts/check_pipeline_contracts.py
```

Chạy full pipeline từ code:

```bash
venv/bin/python -c 'from app.schemas import PipelineState; from app.queue import init_job_state; from app.graph import run_pipeline; payload=PipelineState(url="https://example.com", priority="normal", woo_category_id=1, publish_status="draft"); init_job_state("manual-test", payload); print(run_pipeline("manual-test", payload.model_dump(by_alias=True))["status"])'
```

## RAG kiến thức

Subsystem này dành cho kho tri thức riêng. Mỗi URL được fetch, extract và cắt thành nhiều knowledge units như `overview`, `fact`, `specs`, `use_case`, `objection`, `faq`, `content`, rồi lưu vào Chroma với metadata để truy vấn lại sau.

Các loại nguồn hiện hỗ trợ:

- trang sản phẩm
- bài viết
- sản phẩm đơn
- sản phẩm có biến thể

Metadata chính mỗi chunk:

- `source_id`
- `source_url`
- `title`
- `source_type`
- `product_kind`
- `primary_category`
- `categories`
- `tags`
- `chunk_kind`

### Gọi API ingest

```bash
curl -X POST http://127.0.0.1:8000/api/rag/ingest \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://loctancuong.vn/sp-hong-shan-tra-ha-giang/",
    "manual_categories": ["tea_knowledge", "hong_tra"],
    "manual_tags": ["ha_giang", "co_thu"],
    "note": "Kho tri thuc tra de RAG rieng",
    "force_reingest": true
  }'
```

### Gọi API search

```bash
curl "http://127.0.0.1:8000/api/rag/search?q=hong%20tra%20ha%20giang%20hau%20ngot&limit=5"
```

### CLI cho RAG

Ingest một URL:

```bash
venv/bin/python -m app.rag_cli ingest \
  https://loctancuong.vn/sp-hong-shan-tra-ha-giang/ \
  --category tea_knowledge \
  --category hong_tra \
  --tag ha_giang \
  --tag co_thu \
  --note "Kho tri thuc tra"
```

Search trong kho:

```bash
venv/bin/python -m app.rag_cli search "hồng trà hà giang hậu ngọt" --limit 5
```

Xem toàn bộ chunk của một nguồn:

```bash
venv/bin/python -m app.rag_cli source https://loctancuong.vn/sp-hong-shan-tra-ha-giang/
```

Xóa toàn bộ chunk theo URL nguồn:

```bash
venv/bin/python -m app.rag_cli delete-source https://loctancuong.vn/sp-hong-shan-tra-ha-giang/
```

## Logging và webhook

- Log dùng `structlog`, output JSON ra stdout.
- Graph log theo `job_started`, `step_started`, `step_completed`, `step_failed`, `job_finished`, `job_failed`.
- Webhook bắn ở các event:
  - `job.completed`
  - `job.failed`
  - `job.duplicate`
  - `job.forced_publish`

Payload webhook gồm các trường chính:

- `event`
- `job_id`
- `status`
- `url`
- `current_step`
- `woo_post_id`
- `woo_link`
- `qa_score`
- `qa_pass`
- `error`
- `processing_time_sec`
- `tokens_used`
- `estimated_cost_usd`
- `step_timings`

## Ghi chú

- README này phản ánh pipeline hiện tại trong repo, không còn là scaffold ban đầu.
- Nếu dùng RAG kiến thức, nên tự phân loại `manual_categories` và `manual_tags` ngay từ lúc ingest để kho dễ kiểm soát hơn.
- Chroma hiện được giữ sạch cho kho tri thức riêng; content pipeline không tự ghi dữ liệu vào đó.
- Tài liệu thiết kế đầy đủ vẫn nằm ở [content-forge-v2-2026.md](/Users/tanle/Documents/agent/content-forge-v2-2026.md).
