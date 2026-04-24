# HỆ THỐNG MULTI-AGENT TẠO BÀI VIẾT TỰ ĐỘNG TỪ URL → WOOCOMMERCE
## Content Forge v2.0 — 2026 Edition (Improved)

> **Phiên bản:** 2.0 (cải tiến toàn diện từ v1.0)
> **Ngày cập nhật:** 21/04/2026
> **Mục tiêu:** Gửi 1 URL → Hệ thống tự động enqueue job → Xử lý sequential (1 URL/lần) → Publish thẳng thành bài viết chuẩn SEO/GEO/AI-Friendly + Rank Math trên WooCommerce.

---

## Mục lục

1. [Tổng quan hệ thống](#1-tổng-quan-hệ-thống)
2. [Tech Stack](#2-tech-stack)
3. [Kiến trúc tổng thể](#3-kiến-trúc-tổng-thể)
4. [Luồng xử lý (Job Queue)](#4-luồng-xử-lý-job-queue)
5. [Chi tiết từng Agent](#5-chi-tiết-từng-agent)
6. [JSON Schema State (LangGraph)](#6-json-schema-state-langgraph)
7. [System Prompts](#7-system-prompts)
8. [QA Scoring Rubric](#8-qa-scoring-rubric)
9. [Cấu trúc thư mục](#9-cấu-trúc-thư-mục)
10. [Cấu hình Docker Compose](#10-cấu-hình-docker-compose)
11. [Chi tiết code các module chính](#11-chi-tiết-code-các-module-chính)
12. [API Endpoints](#12-api-endpoints)
13. [Chiến lược tối ưu chi phí LLM](#13-chiến-lược-tối-ưu-chi-phí-llm)
14. [Monitoring & Observability](#14-monitoring--observability)
15. [ChromaDB Warm-up Strategy](#15-chromadb-warm-up-strategy)
16. [Hướng dẫn triển khai](#16-hướng-dẫn-triển-khai)
17. [Xử lý lỗi & Dead Letter Queue](#17-xử-lý-lỗi--dead-letter-queue)
18. [Changelog v1.0 → v2.0](#18-changelog-v10--v20)

---

## 1. Tổng quan hệ thống

Content Forge v2.0 là một **pipeline multi-agent stateful** sử dụng LangGraph + RQ Queue, được thiết kế để tự động hóa toàn bộ quá trình từ thu thập nội dung đến publish bài viết chuẩn SEO trên WooCommerce.

### Nguyên tắc cốt lõi

| Nguyên tắc | Mô tả |
|---|---|
| **Source-first** | Source URL chiếm 70–80% nội dung, knowledge chỉ bổ sung tối đa 20–25% |
| **No duplication** | Kiểm tra hash URL trước khi xử lý, tránh publish bài trùng |
| **E-E-A-T 2026** | Author, Date, Source, Experience signals bắt buộc |
| **GEO-ready** | TL;DR, Table, FAQ, Direct Answer trong mọi bài viết |
| **Cost-aware** | Phân tầng model theo độ phức tạp của từng agent |
| **Observable** | Mọi job đều có metrics, logging, và alert khi fail |

### Những gì hệ thống KHÔNG làm

- Không sử dụng Firecrawl, Browserless hay bất kỳ API scraping trả phí nào
- Không publish tự động khi QA fail (luôn chuyển sang Dead Letter Queue)
- Không để LLM tự đánh giá plagiarism (dùng difflib thay thế)
- Không để knowledge lấn át source gốc

---

## 2. Tech Stack

### Core Stack (100% miễn phí, trừ LLM API)

| Layer | Công nghệ | Phiên bản | Ghi chú |
|---|---|---|---|
| **Scraping** | Trafilatura | latest | Extract main content cực sạch, không cần JS rendering |
| **Orchestration** | LangGraph (LangChain) | 0.2+ | Stateful graph + feedback loop |
| **Job Queue** | RQ (Redis Queue) | latest | concurrency=1, retry, dashboard |
| **Dead Letter Queue** | RQ (queue riêng) | latest | Lưu job fail để review thủ công |
| **Vector DB** | ChromaDB (persistent) | latest | Local, không cần server |
| **Plagiarism Check** | difflib (Python built-in) | — | So sánh với source text, không cần API |
| **Backend API** | FastAPI + Python 3.12 | — | Async, type-safe |
| **LLM (Writer/QA)** | Claude Sonnet 4 | latest | Chất lượng cao cho content |
| **LLM (Extract/Plan)** | Claude Haiku 4 | latest | Nhanh, rẻ cho structured tasks |
| **Image Search** | Unsplash API | v1 | Free tier, 50 req/giờ |
| **Publisher** | WooCommerce REST API + Rank Math | WP 6.5+ | Tạo Post + Schema tự động |
| **Monitoring** | Prometheus + RQ Dashboard | latest | Metrics + job visibility |
| **Deploy** | Docker + Docker Compose | — | VPS VN |

---

## 3. Kiến trúc tổng thể

```
                        ┌─────────────────────────────────┐
                        │          FastAPI Backend         │
                        │  POST /api/submit                │
                        │  GET  /api/job/{job_id}          │
                        └──────────────┬──────────────────┘
                                       │ enqueue
                                       ▼
                        ┌─────────────────────────────────┐
                        │     RQ Queue (Redis)             │
                        │  "content_pipeline" queue        │
                        │  "dlq" dead letter queue         │
                        │  "high_priority" queue           │
                        └──────────────┬──────────────────┘
                                       │ dequeue (concurrency=1)
                                       ▼
          ┌────────────────────────────────────────────────────────┐
          │              LangGraph Workflow (Stateful)              │
          │                                                         │
          │  [0] Deduplicator ──► [1] Fetcher ──► [2] Extractor   │
          │                                              │          │
          │                                              ▼          │
          │  [6] Internal Linker ◄── [5] Enricher ◄── [3] Knowledge│
          │         │                                    │          │
          │         ▼                                    ▼          │
          │  [8] Publisher ◄── [7] QA ◄── [4b] Humanizer           │
          │         │            │               ▲                  │
          │         │         fail│               │retry            │
          │         │            └───────────────┘                  │
          │         │                 (max 2 lần)                   │
          │         ▼                                               │
          │  WooCommerce REST API                                   │
          └────────────────────────────────────────────────────────┘
                        │
                        ▼
           ┌────────────────────────┐
           │   ChromaDB (Vectors)   │
           │   Redis (Cache/Queue)  │
           │   Prometheus (Metrics) │
           └────────────────────────┘
```

---

## 4. Luồng xử lý (Job Queue)

```
POST /api/submit { url, woo_category_id, focus_keyword, priority }
    │
    ▼
FastAPI → validate input → check URL format
    │
    ▼
enqueue vào RQ Queue ("high_priority" hoặc "content_pipeline")
    │
    ▼
Worker (concurrency=1) dequeue job
    │
    ▼
LangGraph Workflow:

  Step 0: Deduplicator
    - Hash URL → kiểm tra Redis SET "processed_urls"
    - Nếu đã tồn tại → trả về {"status": "duplicate", "existing_url": "..."}
    - Nếu mới → thêm vào SET, tiếp tục

  Step 1: Fetcher (Trafilatura)
    - Scrape URL → clean text, HTML, metadata

  Step 2: Extractor (Claude Haiku)
    - Trích xuất key_points, entities, tone, original_intent

  Step 3: Knowledge (ChromaDB RAG)
    - Query ChromaDB → lấy 4–8 facts bổ sung
    - Nếu ChromaDB thiếu → fallback: Google Search API free tier

  Step 4: Enricher (optional)
    - Tìm 1–2 URL liên quan từ search
    - Scrape và extract thêm để làm giàu ngữ cảnh

  Step 5: Planner (Claude Haiku)
    - Tạo outline GEO-ready, focus_keyword, article_type

  Step 6a: Writer (Claude Sonnet)
    - Viết bài full theo outline

  Step 6b: Humanizer (Claude Sonnet)
    - Thêm human touch, ví dụ cụ thể, câu chuyện ngắn
    - Tách riêng để QA có thể retry độc lập

  Step 7: Internal Linker
    - Query ChromaDB tìm 2–3 bài đã published liên quan
    - Chèn internal links tự động

  Step 8: QA (Claude Sonnet)
    - difflib plagiarism check (ngưỡng < 35%)
    - Scoring rubric 4 chiều
    - pass → tiếp tục
    - fail → feedback → retry Writer+Humanizer (max 2 lần)
    - fail sau 2 retry → đẩy vào Dead Letter Queue

  Step 9: Image Selector
    - Gọi Unsplash API với focus_keyword
    - Chọn ảnh phù hợp + generate alt text

  Step 10: Publisher
    - Build WooCommerce POST payload
    - Inject Rank Math meta
    - Inject Schema JSON-LD (Article/FAQ/HowTo)
    - Publish hoặc Draft tùy config
    - Lưu woo_post_id + link vào Redis
    - Update ChromaDB với bài mới (cho internal linking)

    ▼
Trả kết quả JSON + webhook (nếu có)
```

---

## 5. Chi tiết từng Agent

### 5.0. Agent Deduplicator (MỚI)

**Input:** `url`
**Output:** `{"is_duplicate": bool, "existing_post_id": int|null}`

**Logic:**
```python
import hashlib
url_hash = hashlib.sha256(url.encode()).hexdigest()
is_duplicate = redis_client.sismember("processed_urls", url_hash)
if not is_duplicate:
    redis_client.sadd("processed_urls", url_hash)
```

**Lý do cần:** Tránh publish bài trùng lặp khi cùng URL được submit nhiều lần (ví dụ user gửi lại do không thấy kết quả).

---

### 5.1. Agent Fetcher

**Input:** `url`
**Output:** `title`, `clean_content` (text), `html`, `metadata`
**Tool:** Trafilatura

```python
import trafilatura

def fetch(url: str) -> dict:
    downloaded = trafilatura.fetch_url(url)
    result = trafilatura.extract(
        downloaded,
        include_comments=False,
        include_tables=True,
        output_format='json'
    )
    return json.loads(result)
```

**Metadata thu thập:** `author`, `publish_date`, `url`, `sitename`, `language`

---

### 5.2. Agent Extractor

**Input:** `clean_content`
**Output:** `key_points`, `important_facts`, `entities`, `original_intent`, `tone`
**Model:** Claude Haiku (nhanh, rẻ — chỉ cần structured extraction)

**Output format:**
```json
{
  "key_points": ["...", "..."],
  "important_facts": ["...", "..."],
  "entities": {"people": [], "places": [], "organizations": [], "products": []},
  "original_intent": "informational|transactional|navigational|commercial",
  "tone": "professional|casual|technical|conversational"
}
```

---

### 5.3. Agent Knowledge

**Input:** `key_points`
**Output:** 4–8 facts ngắn + nguồn từ ChromaDB
**Nguyên tắc:** Chỉ bổ sung, không lấn át source

```python
def get_knowledge(key_points: list[str]) -> list[dict]:
    results = []
    for point in key_points[:3]:  # Query top 3 điểm quan trọng nhất
        docs = chroma_collection.query(
            query_texts=[point],
            n_results=2
        )
        results.extend(docs['documents'][0])
    
    # Deduplicate + giới hạn tối đa 8 facts
    return deduplicate(results)[:8]
```

**Fallback:** Nếu ChromaDB trả về < 2 results → gọi Google Custom Search API (free tier: 100 req/ngày).

---

### 5.4. Agent Enricher (MỚI)

**Input:** `key_points`, `focus_keyword`
**Output:** `additional_sources` (list của extracted content từ 1–2 URL liên quan)
**Mục đích:** Làm giàu ngữ cảnh khi ChromaDB còn ít dữ liệu (giai đoạn đầu triển khai)

**Logic:**
```python
def enrich(key_points, focus_keyword):
    search_query = f"{focus_keyword} {key_points[0]}"
    related_urls = google_search(search_query, num=2)
    
    additional = []
    for url in related_urls:
        content = trafilatura.fetch_url(url)
        extracted = trafilatura.extract(content)
        if extracted:
            additional.append({
                "url": url,
                "summary": extracted[:500]  # Chỉ lấy 500 ký tự đầu
            })
    return additional
```

---

### 5.5. Agent Planner

**Input:** `key_points` + `knowledge_facts` + `metadata` + `additional_sources`
**Output (JSON):**

```json
{
  "article_type": "Comprehensive Guide | Review | How-to | Local Guide | Comparison",
  "target_intent": "informational|commercial|transactional",
  "tone": "professional|friendly|technical",
  "seo_geo_keywords": ["keyword1 VN", "keyword2 local", "..."],
  "focus_keyword": "keyword chính (gợi ý)",
  "outline": {
    "intro": "TL;DR + direct answer ngắn gọn",
    "sections": [
      {"h2": "Câu hỏi dạng H2?", "content_hint": "..."},
      {"h2": "Table so sánh H2", "content_hint": "comparison table"},
      {"h2": "FAQ Section", "content_hint": "5-7 câu hỏi thường gặp"}
    ],
    "conclusion": "Tóm tắt + CTA"
  },
  "e_e_a_t_elements": {
    "author_note": true,
    "publish_date": true,
    "source_citations": true,
    "experience_signals": ["ví dụ thực tế", "case study"]
  },
  "schema_type": "Article | FAQPage | HowTo | Product"
}
```

**Model:** Claude Haiku

---

### 5.6a. Agent Writer

**Input:** Tất cả output trước + outline
**Output:** `draft_markdown`, `draft_html`, `schema_suggestion`
**Model:** Claude Sonnet (chất lượng cao)
**Độ dài:** 1200–2500 từ tùy `article_type`

**Cấu trúc bắt buộc:**
```
# [Title]

> **TL;DR:** [Direct answer trong 2–3 câu]

## [H2 — Câu hỏi chính?]
[Nội dung, bullet points]

## [H2 — So sánh / Table]
| Tiêu chí | Option A | Option B |
|---|---|---|

## [H2 — Câu hỏi thực tế]
[Experience signal, ví dụ cụ thể]

## FAQ

**Q: ...**
A: ...

---
*Bài viết được biên soạn bởi [Author]. Cập nhật: [Date]. Nguồn tham khảo: [URL]*
```

---

### 5.6b. Agent Humanizer (MỚI)

**Input:** `draft_markdown` từ Writer
**Output:** `humanized_markdown`, `humanized_html`
**Model:** Claude Sonnet

**Nhiệm vụ cụ thể:**
- Thêm câu chuyện ngắn / anecdote mở đầu hoặc giữa bài
- Thêm ví dụ cụ thể, số liệu thực tế (khi có thể)
- Điều chỉnh tone giọng văn phù hợp với độc giả VN
- Tránh câu quá dài, quá sách vở
- Thêm transition phrases tự nhiên giữa các section

**Prompt riêng biệt:** Tách humanizer thành node độc lập giúp QA retry chính xác hơn — nếu QA fail vì "giọng văn quá cứng", chỉ cần retry Humanizer thay vì chạy lại Writer.

---

### 5.7. Agent Internal Linker (MỚI)

**Input:** `humanized_html`, `focus_keyword`, `key_points`
**Output:** `linked_html` (HTML với internal links đã được chèn)

**Logic:**
```python
def add_internal_links(html: str, focus_keyword: str) -> str:
    # Query ChromaDB lấy bài đã published liên quan
    related = chroma_collection.query(
        query_texts=[focus_keyword],
        n_results=5,
        where={"status": "published"}
    )
    
    # Lấy top 2-3 bài liên quan nhất
    top_posts = related['metadatas'][0][:3]
    
    # Chèn link tự nhiên vào HTML
    for post in top_posts:
        anchor_text = post['title']
        link = f'<a href="{post["url"]}">{anchor_text}</a>'
        # Tìm vị trí phù hợp trong HTML để chèn (không phải tiêu đề)
        html = insert_link_naturally(html, link, post['keywords'])
    
    return html
```

---

### 5.8. Agent QA

**Input:** `linked_html`, `clean_content` (source gốc)
**Output:** `qa_result` (JSON với scoring rubric đầy đủ)
**Model:** Claude Sonnet

**Quy trình QA 2 bước:**

**Bước 1 — Plagiarism check (code, không dùng LLM):**
```python
from difflib import SequenceMatcher

def check_plagiarism(source: str, generated: str) -> float:
    matcher = SequenceMatcher(None, source.lower(), generated.lower())
    similarity = matcher.ratio()
    return round(similarity, 3)
```

**Bước 2 — LLM QA check** (chỉ chạy nếu plagiarism < 0.35):

Output format từ LLM QA:
```json
{
  "scores": {
    "plagiarism_similarity": 0.28,
    "eeat_score": 7,
    "geo_structure_score": 8,
    "readability_score": 7,
    "rank_math_readiness": 9
  },
  "pass": true,
  "overall_score": 7.75,
  "feedback": {
    "strengths": ["TL;DR rõ ràng", "Table so sánh tốt"],
    "improvements": ["Cần thêm ví dụ cụ thể ở section 2", "FAQ cần 1 câu hỏi nữa"],
    "retry_target": "humanizer"
  }
}
```

**Ngưỡng pass:** `overall_score >= 7.0` VÀ `plagiarism_similarity < 0.35`

**`retry_target`:** Chỉ định node nào cần retry (`writer` hoặc `humanizer`), giúp tiết kiệm token.

---

### 5.9. Agent Image Selector (MỚI)

**Input:** `focus_keyword`, `article_type`
**Output:** `featured_image_url`, `alt_text`, `photographer_credit`

```python
import httpx

def select_image(focus_keyword: str) -> dict:
    resp = httpx.get(
        "https://api.unsplash.com/search/photos",
        params={"query": focus_keyword, "per_page": 5, "orientation": "landscape"},
        headers={"Authorization": f"Client-ID {UNSPLASH_ACCESS_KEY}"}
    )
    photos = resp.json()["results"]
    
    # Chọn ảnh có resolution tốt nhất
    best = max(photos, key=lambda p: p["width"])
    
    return {
        "url": best["urls"]["regular"],
        "alt_text": f"{focus_keyword} - {best['alt_description'] or best['description']}",
        "photographer": best["user"]["name"],
        "unsplash_link": best["links"]["html"]
    }
```

**Alt text generation:** Kết hợp `focus_keyword` + description từ Unsplash để tạo alt text chuẩn SEO.

---

### 5.10. Agent Publisher

**Input:** `linked_html`, `plan`, `qa_result`, `image_data`
**Action:** Tạo Post trên WooCommerce

**Payload WooCommerce:**
```python
def build_woo_payload(state: dict) -> dict:
    return {
        "title": state["plan"]["title"],
        "content": state["linked_html"],
        "status": "publish",  # hoặc "draft" nếu config
        "categories": [state["woo_category_id"]],
        "featured_media": state["image_data"]["media_id"],
        "meta": {
            "_rank_math_focus_keyword": state["plan"]["focus_keyword"],
            "_rank_math_seo_score": "80",
            "_rank_math_robots": "index,follow",
            "_rank_math_description": state["plan"]["meta_description"],
        },
        "yoast_head_json": build_schema(state),  # JSON-LD
    }
```

**Schema JSON-LD tự động theo article_type:**

```python
SCHEMA_TEMPLATES = {
    "Article": lambda s: {
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": s["plan"]["title"],
        "author": {"@type": "Person", "name": s["metadata"]["author"]},
        "datePublished": s["metadata"]["publish_date"],
        "url": s["url"]
    },
    "FAQPage": lambda s: {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [
            {"@type": "Question", "name": q["question"],
             "acceptedAnswer": {"@type": "Answer", "text": q["answer"]}}
            for q in s["extracted"]["faq_items"]
        ]
    },
    "HowTo": lambda s: {
        "@context": "https://schema.org",
        "@type": "HowTo",
        "name": s["plan"]["title"],
        "step": [
            {"@type": "HowToStep", "text": step}
            for step in s["extracted"]["steps"]
        ]
    }
}
```

**Sau khi publish thành công:**
- Lưu `woo_post_id`, `woo_url`, `url_hash` vào Redis
- Update ChromaDB với metadata bài mới (cho internal linking những bài sau)
- Gọi webhook nếu config

---

## 6. JSON Schema State (LangGraph)

```json
{
  "url": "string",
  "priority": "normal | high",
  "woo_category_id": "number",
  "focus_keyword_override": "string | null",

  "dedup_result": {
    "is_duplicate": false,
    "url_hash": "string"
  },

  "fetch_result": {
    "title": "string",
    "clean_content": "string",
    "html": "string",
    "metadata": {
      "author": "string",
      "publish_date": "string",
      "url": "string",
      "sitename": "string",
      "language": "string"
    }
  },

  "extracted": {
    "key_points": ["..."],
    "important_facts": ["..."],
    "entities": {},
    "original_intent": "string",
    "tone": "string",
    "faq_items": [{"question": "...", "answer": "..."}],
    "steps": ["..."]
  },

  "knowledge_facts": [
    {"fact": "string", "source": "chromadb | google_search"}
  ],

  "additional_sources": [
    {"url": "string", "summary": "string"}
  ],

  "plan": {
    "article_type": "string",
    "target_intent": "string",
    "tone": "string",
    "seo_geo_keywords": ["..."],
    "focus_keyword": "string",
    "meta_description": "string",
    "outline": {},
    "e_e_a_t_elements": {},
    "schema_type": "string"
  },

  "draft": {
    "markdown": "string",
    "html": "string"
  },

  "humanized": {
    "markdown": "string",
    "html": "string"
  },

  "linked_html": "string",

  "image_data": {
    "url": "string",
    "media_id": "number",
    "alt_text": "string",
    "photographer": "string"
  },

  "qa_result": {
    "scores": {
      "plagiarism_similarity": 0.0,
      "eeat_score": 0,
      "geo_structure_score": 0,
      "readability_score": 0,
      "rank_math_readiness": 0
    },
    "overall_score": 0.0,
    "pass": false,
    "feedback": {
      "strengths": ["..."],
      "improvements": ["..."],
      "retry_target": "writer | humanizer"
    },
    "retry_count": 0
  },

  "final_article": {
    "title": "string",
    "html": "string",
    "schema": {}
  },

  "woo_post_id": "number",
  "woo_link": "string",

  "metrics": {
    "total_tokens_used": 0,
    "estimated_cost_usd": 0.0,
    "processing_time_sec": 0.0,
    "agents_used": ["..."]
  },

  "error": "string | null",
  "status": "pending | processing | completed | failed | duplicate"
}
```

---

## 7. System Prompts

### 7.1. Prompt Extractor (Claude Haiku)

```
Bạn là Extractor chuyên phân tích bài viết tiếng Việt.
Từ nội dung được cung cấp, hãy trích xuất và trả về JSON hợp lệ:

{
  "key_points": [tối đa 7 điểm quan trọng nhất, mỗi điểm 1 câu],
  "important_facts": [số liệu, tên, ngày tháng cụ thể],
  "entities": {
    "people": [], "places": [], "organizations": [], "products": []
  },
  "original_intent": "informational|transactional|navigational|commercial",
  "tone": "professional|casual|technical|conversational",
  "faq_items": [{"question": "...", "answer": "..."} — tối đa 5 cặp],
  "steps": [nếu là how-to, liệt kê các bước, nếu không để array rỗng]
}

Chỉ trả về JSON. Không thêm giải thích hay markdown.
```

### 7.2. Prompt Planner (Claude Haiku)

```
Bạn là Planner 2026 chuyên tối ưu E-E-A-T, GEO và Rank Math cho thị trường Việt Nam.

Từ key_points, knowledge_facts, và metadata, hãy:
1. Chọn article_type phù hợp nhất:
   - Comprehensive Guide: bài dài, nhiều thông tin
   - Review: đánh giá sản phẩm/dịch vụ
   - How-to: hướng dẫn từng bước
   - Local Guide: thông tin địa phương VN
   - Comparison: so sánh nhiều lựa chọn

2. Tạo outline GEO-ready bắt buộc có:
   - Intro: TL;DR + direct answer (2-3 câu)
   - H2 là câu hỏi thực tế người dùng hay tìm
   - Ít nhất 1 table so sánh
   - FAQ section (5-7 câu hỏi)
   - Kết bài có CTA rõ ràng

3. Gợi ý focus_keyword (tiếng Việt, long-tail, local nếu phù hợp)
4. Gợi ý meta_description (tối đa 155 ký tự)
5. Liệt kê 5-8 seo_geo_keywords liên quan

Trả về JSON hợp lệ theo schema đã định nghĩa. Không thêm giải thích.
```

### 7.3. Prompt Writer (Claude Sonnet)

```
Bạn là nhà viết content chuyên nghiệp người Việt, giọng văn tự nhiên, thân thiện, am hiểu thị trường VN 2026.

Viết bài theo outline đã cung cấp với các yêu cầu bắt buộc:

CẤU TRÚC:
- Bắt đầu bằng TL;DR direct answer (2-3 câu, trả lời thẳng vào trọng tâm)
- H2 phải là câu hỏi người dùng thực sự tìm kiếm
- Có ít nhất 1 bảng so sánh
- Có FAQ section cuối bài (5-7 câu hỏi/trả lời)
- Kết bài có tóm tắt + CTA

NỘI DUNG:
- Paraphrase MẠNH từ source (không copy nguyên văn)
- Knowledge facts chỉ bổ sung nhẹ, không chiếm quá 20%
- Thêm E-E-A-T signals: tác giả, ngày, nguồn tham khảo
- Sử dụng bullet points, table để dễ đọc
- Độ dài: {target_word_count} từ

FORMAT OUTPUT:
- markdown (chuẩn) cho phần đầu
- html (đã render) cho phần sau
- schema_suggestion: loại schema phù hợp (không cần viết full JSON)

Tuyệt đối không bịa số liệu hay trích dẫn sai.
```

### 7.4. Prompt Humanizer (Claude Sonnet)

```
Bạn là biên tập viên content người Việt, chuyên thêm "hơi thở con người" vào bài viết.

Nhận bài viết draft đã có, hãy:

THÊM VÀO:
- 1-2 câu chuyện ngắn hoặc ví dụ thực tế từ cuộc sống hàng ngày VN
- Câu mở đầu section hấp dẫn, không khô khan
- Transition phrases tự nhiên giữa các phần
- Nếu bài review: thêm góc nhìn "người dùng thực tế"
- Nếu bài how-to: thêm "tip nhỏ từ kinh nghiệm"

GIỮ NGUYÊN:
- Cấu trúc outline, H2/H3
- Số liệu, tên, ngày tháng đã có
- TL;DR và FAQ

TRÁNH:
- Câu quá dài (> 25 từ)
- Lặp từ nhiều lần trong 1 đoạn
- Giọng văn dịch máy hoặc quá sách vở

Trả về: markdown + html (giữ nguyên format output từ Writer).
```

### 7.5. Prompt QA (Claude Sonnet)

```
Bạn là QA Editor 2026 nghiêm ngặt, chuyên kiểm tra bài viết SEO/GEO cho thị trường VN.

LƯU Ý: Điểm plagiarism đã được tính bằng code (difflib), chỉ cần đánh giá 4 tiêu chí còn lại.

ĐÁNH GIÁ (thang điểm 1-10):

1. eeat_score (E-E-A-T):
   - Có tên tác giả hoặc nguồn không? (+2)
   - Có ngày xuất bản không? (+2)
   - Có dẫn nguồn cụ thể không? (+3)
   - Có experience signals (ví dụ thực tế, case study) không? (+3)

2. geo_structure_score (GEO):
   - Có TL;DR / direct answer đầu bài không? (+3)
   - Có bảng so sánh không? (+2)
   - Có FAQ section không? (+3)
   - H2 có dạng câu hỏi không? (+2)

3. readability_score:
   - Câu văn tự nhiên, không dịch máy? (+3)
   - Có bullet/table dễ đọc? (+3)
   - Transition phrases mượt mà? (+2)
   - Phù hợp độc giả VN phổ thông? (+2)

4. rank_math_readiness:
   - Focus keyword xuất hiện trong title không? (+2)
   - Focus keyword xuất hiện trong H2 đầu tiên? (+2)
   - Meta description hợp lý (< 155 ký tự)? (+2)
   - Internal links có không? (+2)
   - Alt text ảnh có không? (+2)

QUYẾT ĐỊNH:
- overall_score = trung bình cộng 4 điểm trên
- pass = overall_score >= 7.0
- Nếu fail: chỉ rõ retry_target ("writer" hoặc "humanizer") và improvements cụ thể

Trả về JSON theo schema đã định nghĩa. Không thêm giải thích ngoài JSON.
```

---

## 8. QA Scoring Rubric

### Thang điểm chi tiết

| Tiêu chí | Trọng số | Ngưỡng Pass | Mô tả |
|---|---|---|---|
| `plagiarism_similarity` | — | < 0.35 | Tính bằng difflib, không phải LLM |
| `eeat_score` | 25% | ≥ 6 | Author, date, source, experience |
| `geo_structure_score` | 30% | ≥ 7 | TL;DR, table, FAQ, H2 dạng câu hỏi |
| `readability_score` | 25% | ≥ 6 | Natural language, bullets, transitions |
| `rank_math_readiness` | 20% | ≥ 7 | Keyword density, meta, links, alt text |
| **overall_score** | — | **≥ 7.0** | Weighted average của 4 tiêu chí |

### Quy trình retry

```
QA fail lần 1:
  → Nếu retry_target = "humanizer": chỉ chạy lại Humanizer + Internal Linker + QA
  → Nếu retry_target = "writer": chạy lại Writer + Humanizer + Internal Linker + QA
  → Truyền feedback JSON vào prompt của node cần retry

QA fail lần 2 (retry_count = 2):
  → Không retry nữa
  → Đẩy job vào Dead Letter Queue với đầy đủ state
  → Log metrics + alert Slack/email nếu config
  → Trả về {"status": "failed", "job_id": "...", "dlq": true}
```

---

## 9. Cấu trúc thư mục

```
content-forge/
├── app/
│   ├── main.py                     # FastAPI app, routes
│   ├── agents/
│   │   ├── __init__.py
│   │   ├── deduplicator.py         # (MỚI) Hash URL check
│   │   ├── fetcher.py              # Trafilatura scraper
│   │   ├── extractor.py            # Key points extraction
│   │   ├── knowledge.py            # ChromaDB RAG
│   │   ├── enricher.py             # (MỚI) Additional sources
│   │   ├── planner.py              # SEO/GEO outline
│   │   ├── writer.py               # Full article writer
│   │   ├── humanizer.py            # (MỚI) Human touch layer
│   │   ├── internal_linker.py      # (MỚI) Auto internal links
│   │   ├── qa.py                   # QA + plagiarism check
│   │   ├── image_selector.py       # (MỚI) Unsplash integration
│   │   └── publisher.py            # WooCommerce REST API
│   ├── graph.py                    # LangGraph workflow definition
│   ├── queue.py                    # RQ queue setup + DLQ
│   ├── schemas.py                  # Pydantic models
│   ├── llm.py                      # LLM client factory (Haiku/Sonnet)
│   ├── chroma.py                   # ChromaDB client + collections
│   ├── metrics.py                  # Prometheus metrics
│   └── config.py                   # Settings từ .env
├── scripts/
│   ├── seed_chroma.py              # Warm-up ChromaDB với dữ liệu ban đầu
│   ├── test_pipeline.py            # Test với 3 URL mẫu
│   └── migrate_v1_to_v2.py        # Migration từ v1.0
├── monitoring/
│   ├── prometheus.yml
│   └── grafana/
│       └── dashboard.json
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── .env.example
└── README.md
```

---

## 10. Cấu hình Docker Compose

```yaml
version: '3.9'

services:
  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"
    volumes:
      - redis_data:/data
    command: redis-server --appendonly yes
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      timeout: 5s
      retries: 5

  fastapi:
    build: .
    ports:
      - "8000:8000"
    environment:
      - REDIS_URL=redis://redis:6379
      - CHROMA_PATH=/data/chroma
    env_file:
      - .env
    volumes:
      - chroma_data:/data/chroma
    depends_on:
      redis:
        condition: service_healthy
    command: uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

  worker:
    build: .
    environment:
      - REDIS_URL=redis://redis:6379
      - CHROMA_PATH=/data/chroma
    env_file:
      - .env
    volumes:
      - chroma_data:/data/chroma
    depends_on:
      redis:
        condition: service_healthy
    command: rq worker content_pipeline high_priority --url redis://redis:6379 --with-scheduler
    deploy:
      replicas: 1  # QUAN TRỌNG: chỉ 1 worker, concurrency=1

  dlq_worker:
    build: .
    environment:
      - REDIS_URL=redis://redis:6379
    env_file:
      - .env
    depends_on:
      redis:
        condition: service_healthy
    command: rq worker dlq --url redis://redis:6379

  rq_dashboard:
    image: eoranged/rq-dashboard
    ports:
      - "9181:9181"
    environment:
      - RQ_DASHBOARD_REDIS_URL=redis://redis:6379
    depends_on:
      - redis

  prometheus:
    image: prom/prometheus:latest
    ports:
      - "9090:9090"
    volumes:
      - ./monitoring/prometheus.yml:/etc/prometheus/prometheus.yml
      - prometheus_data:/prometheus

volumes:
  redis_data:
  chroma_data:
  prometheus_data:
```

---

## 11. Chi tiết code các module chính

### 11.1. `app/llm.py` — LLM Factory theo tầng

```python
import anthropic
from functools import lru_cache

# Phân tầng model theo độ phức tạp
AGENT_MODEL_MAP = {
    "extractor": "claude-haiku-4-5-20251001",    # Nhanh, rẻ
    "planner": "claude-haiku-4-5-20251001",       # Nhanh, rẻ
    "knowledge": "claude-haiku-4-5-20251001",     # Nhanh, rẻ
    "writer": "claude-sonnet-4-6",               # Chất lượng cao
    "humanizer": "claude-sonnet-4-6",            # Chất lượng cao
    "qa": "claude-sonnet-4-6",                   # Chất lượng cao
}

@lru_cache(maxsize=1)
def get_client() -> anthropic.Anthropic:
    return anthropic.Anthropic()

def call_llm(agent_name: str, system: str, user: str, max_tokens: int = 4096) -> str:
    client = get_client()
    model = AGENT_MODEL_MAP.get(agent_name, "claude-sonnet-4-6")
    
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}]
    )
    
    # Track token usage
    track_tokens(agent_name, response.usage.input_tokens, response.usage.output_tokens)
    
    return response.content[0].text
```

### 11.2. `app/queue.py` — RQ Setup với Priority Queue

```python
from rq import Queue
from redis import Redis

redis_conn = Redis.from_url(settings.REDIS_URL)

# 3 queues: high priority, normal, dead letter
queue_high = Queue("high_priority", connection=redis_conn)
queue_normal = Queue("content_pipeline", connection=redis_conn)
queue_dlq = Queue("dlq", connection=redis_conn)

def enqueue_job(url: str, priority: str = "normal", **kwargs) -> str:
    q = queue_high if priority == "high" else queue_normal
    job = q.enqueue(
        "app.graph.run_pipeline",
        args=(url,),
        kwargs=kwargs,
        job_timeout=600,          # 10 phút timeout
        retry=Retry(max=2, intervals=[60, 120]),  # Retry sau 1 và 2 phút
        result_ttl=86400,         # Giữ result 24 giờ
        failure_ttl=604800        # Giữ failed job 7 ngày
    )
    return job.id

def send_to_dlq(job_id: str, state: dict, reason: str):
    queue_dlq.enqueue(
        "app.dlq.handle_failed_job",
        args=(job_id, state, reason),
        result_ttl=2592000  # Giữ 30 ngày để review
    )
```

### 11.3. `app/agents/qa.py` — QA với difflib

```python
from difflib import SequenceMatcher
import json

def check_plagiarism(source: str, generated: str) -> float:
    """Tính similarity ratio giữa source và bài generated."""
    # Normalize text
    source_clean = " ".join(source.lower().split())
    generated_clean = " ".join(generated.lower().split())
    
    matcher = SequenceMatcher(
        None,
        source_clean,
        generated_clean,
        autojunk=False  # Không bỏ qua "junk" sequences
    )
    return round(matcher.ratio(), 3)

def run_qa(state: dict) -> dict:
    source = state["fetch_result"]["clean_content"]
    generated = state["linked_html"]
    
    # Bước 1: Plagiarism check bằng code
    similarity = check_plagiarism(source, generated)
    
    if similarity >= 0.35:
        return {
            "qa_result": {
                "scores": {"plagiarism_similarity": similarity},
                "pass": False,
                "feedback": {
                    "improvements": [f"Similarity quá cao ({similarity:.0%}), cần paraphrase mạnh hơn"],
                    "retry_target": "writer"
                },
                "retry_count": state["qa_result"]["retry_count"] + 1
            }
        }
    
    # Bước 2: LLM QA check
    qa_prompt = build_qa_prompt(state, similarity)
    raw = call_llm("qa", QA_SYSTEM_PROMPT, qa_prompt)
    
    qa_data = json.loads(raw)
    qa_data["scores"]["plagiarism_similarity"] = similarity
    qa_data["retry_count"] = state["qa_result"]["retry_count"] + 1
    
    return {"qa_result": qa_data}
```

### 11.4. `app/metrics.py` — Prometheus Metrics

```python
from prometheus_client import Counter, Histogram, Gauge, start_http_server

# Counters
jobs_submitted = Counter("content_forge_jobs_submitted_total", "Total jobs submitted")
jobs_completed = Counter("content_forge_jobs_completed_total", "Total jobs completed")
jobs_failed = Counter("content_forge_jobs_failed_total", "Total jobs failed", ["reason"])
jobs_duplicate = Counter("content_forge_jobs_duplicate_total", "Total duplicate URLs")

# Histograms
job_duration = Histogram(
    "content_forge_job_duration_seconds",
    "Job processing time",
    buckets=[30, 60, 120, 180, 300, 600]
)
token_usage = Histogram(
    "content_forge_tokens_used",
    "LLM tokens per job",
    ["agent"],
    buckets=[100, 500, 1000, 2000, 4000, 8000]
)
qa_score = Histogram(
    "content_forge_qa_score",
    "QA overall scores",
    buckets=[5, 6, 7, 7.5, 8, 9, 10]
)

# Gauges
active_jobs = Gauge("content_forge_active_jobs", "Currently processing jobs")
dlq_size = Gauge("content_forge_dlq_size", "Jobs in Dead Letter Queue")

def start_metrics_server(port: int = 8001):
    start_http_server(port)
```

---

## 12. API Endpoints

### POST `/api/submit`

Submit URL để xử lý.

**Request:**
```json
{
  "url": "https://example.com/bai-viet-mau",
  "woo_category_id": 123,
  "focus_keyword": "từ khóa chính (optional)",
  "priority": "normal | high",
  "publish_status": "publish | draft"
}
```

**Response:**
```json
{
  "job_id": "abc123",
  "status": "queued",
  "queue": "content_pipeline",
  "estimated_wait_sec": 120,
  "check_url": "/api/job/abc123"
}
```

**Errors:**
- `400` — URL không hợp lệ
- `409` — URL đã được xử lý (duplicate)
- `429` — Queue đầy (> 100 jobs đang chờ)

---

### GET `/api/job/{job_id}`

Kiểm tra trạng thái job.

**Response (đang xử lý):**
```json
{
  "job_id": "abc123",
  "status": "processing",
  "current_step": "writer",
  "progress_percent": 60
}
```

**Response (hoàn thành):**
```json
{
  "job_id": "abc123",
  "status": "completed",
  "woo_post_id": 456,
  "woo_link": "https://yoursite.com/ten-bai-viet/",
  "qa_score": 8.2,
  "processing_time_sec": 145,
  "tokens_used": 4200,
  "estimated_cost_usd": 0.032
}
```

**Response (failed):**
```json
{
  "job_id": "abc123",
  "status": "failed",
  "error": "QA failed after 2 retries: geo_structure_score too low",
  "dlq": true,
  "dlq_review_url": "/api/dlq/abc123"
}
```

---

### GET `/api/dlq`

Xem danh sách job trong Dead Letter Queue.

**Response:**
```json
{
  "total": 3,
  "jobs": [
    {
      "job_id": "...",
      "url": "...",
      "failed_at": "2026-04-21T10:30:00Z",
      "reason": "QA fail 2 retries",
      "qa_score": 6.1,
      "review_url": "/api/dlq/{job_id}"
    }
  ]
}
```

---

### POST `/api/dlq/{job_id}/retry`

Retry thủ công job trong DLQ (sau khi đã review).

---

### GET `/api/stats`

Thống kê tổng quan.

```json
{
  "total_processed": 142,
  "success_rate": 0.94,
  "avg_processing_time_sec": 134,
  "avg_qa_score": 7.8,
  "avg_cost_per_article_usd": 0.028,
  "dlq_size": 8,
  "duplicate_rate": 0.06
}
```

---

## 13. Chiến lược tối ưu chi phí LLM

### Phân tầng model

| Agent | Model | Lý do |
|---|---|---|
| Extractor | Claude Haiku | Chỉ cần structured extraction, không cần sáng tạo |
| Planner | Claude Haiku | Tạo outline có cấu trúc rõ ràng, không cần prose quality |
| Knowledge | Claude Haiku | Chỉ filter và format facts |
| Writer | Claude Sonnet | Cần chất lượng cao, sáng tạo |
| Humanizer | Claude Sonnet | Cần hiểu sắc thái văn phong tiếng Việt |
| QA | Claude Sonnet | Cần judgment phức tạp |

### Ước tính chi phí mỗi bài viết

| Component | Tokens (ước tính) | Chi phí (USD) |
|---|---|---|
| Extractor (Haiku) | ~800 input + 500 output | ~$0.001 |
| Planner (Haiku) | ~1500 input + 800 output | ~$0.002 |
| Writer (Sonnet) | ~2000 input + 3000 output | ~$0.021 |
| Humanizer (Sonnet) | ~3500 input + 3500 output | ~$0.028 |
| QA (Sonnet) | ~4000 input + 800 output | ~$0.018 |
| **Tổng (1 lần pass)** | **~19,400 tokens** | **~$0.070** |
| **Tổng (có 1 retry)** | **~30,000 tokens** | **~$0.110** |

> **Lưu ý:** Giá tính theo Claude API pricing tháng 04/2026. Cập nhật tại [anthropic.com/pricing](https://www.anthropic.com/pricing).

### Token Budget

```python
TOKEN_BUDGETS = {
    "extractor": {"max_input": 8000, "max_output": 1000},
    "planner": {"max_input": 6000, "max_output": 2000},
    "writer": {"max_input": 8000, "max_output": 4096},
    "humanizer": {"max_input": 8000, "max_output": 4096},
    "qa": {"max_input": 8000, "max_output": 1000},
}
```

Nếu source content > token budget của extractor → truncate thông minh (giữ đầu 40% + cuối 20% + giữa 40%).

---

## 14. Monitoring & Observability

### Metrics Prometheus (expose tại `:8001/metrics`)

```
# Job metrics
content_forge_jobs_submitted_total
content_forge_jobs_completed_total
content_forge_jobs_failed_total{reason="qa_fail|fetch_fail|llm_error"}
content_forge_jobs_duplicate_total

# Performance
content_forge_job_duration_seconds (histogram)
content_forge_tokens_used{agent="writer|qa|..."} (histogram)
content_forge_qa_score (histogram)

# Health
content_forge_active_jobs (gauge)
content_forge_dlq_size (gauge)
```

### Alerting Rules (Prometheus)

```yaml
groups:
  - name: content_forge
    rules:
      - alert: HighFailureRate
        expr: rate(content_forge_jobs_failed_total[1h]) / rate(content_forge_jobs_submitted_total[1h]) > 0.15
        for: 10m
        annotations:
          summary: "Failure rate > 15% trong 1 giờ"

      - alert: DLQGrowing
        expr: content_forge_dlq_size > 10
        for: 5m
        annotations:
          summary: "DLQ có > 10 jobs cần review"

      - alert: SlowProcessing
        expr: content_forge_job_duration_seconds{quantile="0.95"} > 300
        for: 15m
        annotations:
          summary: "95th percentile processing time > 5 phút"
```

### Logging

```python
import structlog

log = structlog.get_logger()

# Mỗi agent log đầy đủ context
log.info("agent_completed",
    job_id=job_id,
    agent="writer",
    tokens_used=1234,
    duration_sec=12.3,
    status="success"
)
```

---

## 15. ChromaDB Warm-up Strategy

ChromaDB cần được seeded trước khi Knowledge Agent có thể hoạt động hiệu quả.

### Script seed ban đầu

```python
# scripts/seed_chroma.py
import chromadb
import trafilatura

SEED_URLS = [
    # Thêm 20-50 URL bài viết chất lượng cao trong niche của bạn
    "https://example.com/bai-viet-mau-1",
    "https://example.com/bai-viet-mau-2",
    # ...
]

def seed_chromadb():
    client = chromadb.PersistentClient(path=CHROMA_PATH)
    collection = client.get_or_create_collection("knowledge_base")
    
    for i, url in enumerate(SEED_URLS):
        try:
            downloaded = trafilatura.fetch_url(url)
            content = trafilatura.extract(downloaded)
            if content:
                collection.add(
                    documents=[content[:2000]],  # Chunk 2000 chars
                    metadatas=[{"source": url, "type": "seed"}],
                    ids=[f"seed_{i}"]
                )
                print(f"✅ Seeded: {url}")
        except Exception as e:
            print(f"❌ Failed: {url} — {e}")

if __name__ == "__main__":
    seed_chromadb()
    print("ChromaDB seeded successfully!")
```

### Strategy cập nhật ongoing

Sau mỗi bài viết được publish thành công, Publisher tự động thêm vào ChromaDB:

```python
def update_knowledge_base(state: dict):
    collection.add(
        documents=[state["fetch_result"]["clean_content"][:2000]],
        metadatas=[{
            "source": state["url"],
            "woo_post_id": str(state["woo_post_id"]),
            "woo_url": state["woo_link"],
            "title": state["plan"]["title"],
            "keywords": ",".join(state["plan"]["seo_geo_keywords"]),
            "status": "published",
            "published_at": datetime.now().isoformat()
        }],
        ids=[f"post_{state['woo_post_id']}"]
    )
```

---

## 16. Hướng dẫn triển khai

### Bước 1 — Chuẩn bị môi trường

```bash
# Clone repo
git clone https://github.com/yourorg/content-forge.git
cd content-forge

# Copy env file
cp .env.example .env
# Điền vào .env: ANTHROPIC_API_KEY, WOO_URL, WOO_KEY, WOO_SECRET, UNSPLASH_ACCESS_KEY
```

### Bước 2 — Cấu hình `.env`

```env
# LLM
ANTHROPIC_API_KEY=sk-ant-...

# WooCommerce
WOO_URL=https://yoursite.com
WOO_CONSUMER_KEY=ck_...
WOO_CONSUMER_SECRET=cs_...
WOO_DEFAULT_STATUS=draft  # publish hoặc draft

# Unsplash
UNSPLASH_ACCESS_KEY=...

# Redis
REDIS_URL=redis://redis:6379

# ChromaDB
CHROMA_PATH=/data/chroma

# Webhook (optional)
WEBHOOK_URL=https://your-webhook.com/notify

# Google Search (optional, cho Enricher)
GOOGLE_SEARCH_API_KEY=...
GOOGLE_SEARCH_ENGINE_ID=...
```

### Bước 3 — Build và khởi động

```bash
# Build images
docker-compose build

# Khởi động tất cả services
docker-compose up -d

# Kiểm tra logs
docker-compose logs -f worker
```

### Bước 4 — Seed ChromaDB

```bash
# Chạy script seed với 20-50 URL mẫu
docker-compose exec worker python scripts/seed_chroma.py
```

### Bước 5 — Test với URL mẫu

```bash
# Test 3 URL mẫu
docker-compose exec worker python scripts/test_pipeline.py

# Hoặc gọi API trực tiếp
curl -X POST http://localhost:8000/api/submit \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com/bai-viet-test", "woo_category_id": 1}'
```

### Bước 6 — Kiểm tra dashboards

- **RQ Dashboard:** http://localhost:9181
- **Prometheus:** http://localhost:9090
- **API Docs:** http://localhost:8000/docs

---

## 17. Xử lý lỗi & Dead Letter Queue

### Phân loại lỗi

| Loại lỗi | Xử lý | Retry? |
|---|---|---|
| URL không fetch được (404, timeout) | Log + DLQ | Không |
| Trafilatura extract rỗng | Log + DLQ | Không |
| LLM API error (rate limit, 429) | Retry sau 60s | Có (tối đa 3 lần) |
| LLM trả về JSON không hợp lệ | Retry ngay | Có (tối đa 2 lần) |
| QA fail sau 2 retry | DLQ + alert | Không (cần review thủ công) |
| WooCommerce API error | Retry sau 30s | Có (tối đa 3 lần) |

### DLQ Review Flow

```
Job trong DLQ
    │
    ▼
GET /api/dlq/{job_id}
    → Xem full state + QA feedback + error reason
    │
    ▼
Option A: POST /api/dlq/{job_id}/retry
    → Retry từ đầu với cùng URL
    
Option B: POST /api/dlq/{job_id}/publish-anyway
    → Force publish dù QA score thấp (có warning)
    
Option C: DELETE /api/dlq/{job_id}
    → Xóa job, không làm gì thêm
```

---

## 18. Changelog v1.0 → v2.0

### Tính năng mới

| Feature | Mô tả | Ưu tiên |
|---|---|---|
| **Agent Deduplicator** | Hash URL check trước khi xử lý, tránh duplicate | 🔴 Cao |
| **difflib Plagiarism Check** | Dùng code thay vì LLM để đánh giá similarity | 🔴 Cao |
| **QA Scoring Rubric** | 4-dimension scoring thay vì pass/fail đơn giản | 🔴 Cao |
| **retry_target** | QA chỉ định retry Writer hay Humanizer, tiết kiệm token | 🔴 Cao |
| **Agent Humanizer** | Tách riêng layer thêm human touch | 🟡 Trung bình |
| **Agent Internal Linker** | Auto chèn internal links từ ChromaDB | 🟡 Trung bình |
| **Agent Image Selector** | Unsplash API tự động chọn featured image + alt text | 🟡 Trung bình |
| **Agent Enricher** | Tìm thêm 1-2 URL liên quan để làm giàu ngữ cảnh | 🟡 Trung bình |
| **Dead Letter Queue** | Lưu job fail để review thủ công, không mất dữ liệu | 🟡 Trung bình |
| **Priority Queue** | high_priority và normal queue độc lập | 🟡 Trung bình |
| **Multi-tier LLM** | Haiku cho extract/plan, Sonnet cho write/qa | 🟢 Nice-to-have |
| **Prometheus Metrics** | Đầy đủ metrics + alerting rules | 🟢 Nice-to-have |
| **ChromaDB Warm-up** | Script seed + strategy cập nhật ongoing | 🟢 Nice-to-have |
| **Token Budget Tracking** | Track chi phí theo từng agent, từng job | 🟢 Nice-to-have |

### Breaking Changes từ v1.0

- `graph.py`: Thêm 4 node mới (deduplicator, enricher, humanizer, internal_linker, image_selector)
- `schemas.py`: State schema mở rộng đáng kể (xem Section 6)
- `queue.py`: Thêm 2 queues mới (high_priority, dlq)
- `qa.py`: QA output format thay đổi hoàn toàn (scoring rubric thay pass/fail)
- `requirements.txt`: Thêm `prometheus-client`, `structlog`

---

*Tài liệu này được tạo ngày 21/04/2026. Mọi đóng góp vui lòng tạo Issue hoặc PR trên repository.*
