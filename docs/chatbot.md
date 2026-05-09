# Chatbot AI tư vấn sản phẩm cho Content Forge

> Mục tiêu: thêm module chatbot AI vào hệ thống Content Forge hiện tại để tư vấn sản phẩm qua Zalo OA trước, sau đó có thể mở rộng sang Facebook Messenger và web chat.

## 1. Kết luận thiết kế

Không dựng project `/opt/chatbot` riêng. Chatbot nên là một module trong Content Forge vì hệ thống hiện đã có:

- FastAPI backend, Docker Compose, Nginx/SSL.
- Postgres để lưu cấu hình, sản phẩm, hội thoại, log.
- Redis để cache hội thoại ngắn hạn và realtime.
- ChromaDB persistent tại `CHROMA_PATH=/data/chroma`.
- RAG tiếng Việt với `dangvantuan/vietnamese-embedding`.
- LLM router qua `ROUTER_BASE`/Cliproxy, có quota tracking.
- Webhook/realtime pattern đã chạy ổn với Facebook.

Tài liệu này thay thế hướng cũ “tạo chatbot-api riêng” bằng hướng tích hợp trực tiếp vào app hiện tại.

## 2. Phạm vi MVP

MVP chỉ cần làm tốt luồng tư vấn text:

1. Khách nhắn Zalo OA.
2. Zalo gọi webhook vào Content Forge.
3. Backend lưu message vào DB.
4. Bot phân loại intent.
5. Bot search sản phẩm/kiến thức trong Chroma.
6. LLM sinh câu trả lời ngắn, tự nhiên, có CTA.
7. Gửi text và tối đa 3 product cards về Zalo.
8. Lưu toàn bộ hội thoại để review và cải thiện prompt.

Không làm ngay:

- CLIP image search.
- Đặt hàng trong chat.
- Đa kênh đầy đủ.
- Agent workflow phức tạp.
- Tự động chốt đơn không kiểm soát.

## 3. Kiến trúc tích hợp

```text
[Zalo User]
   ↓ user_send_text / user_send_image
[Zalo OA Webhook]
   ↓ HTTPS
[Nginx hiện tại]
   ↓ /api/chatbot/zalo/webhook
[Content Forge FastAPI]
   ├─ app/chatbot/*
   ├─ app/zalo/*
   ├─ Postgres: products, sessions, messages, logs
   ├─ Redis: short-term conversation state
   ├─ Chroma: chatbot_products + knowledge_base_*
   └─ LLM Router: ROUTER_BASE=http://cliproxy:8317/v1
       ↓
[Zalo OA API]
   ↓
[Khách hàng]
```

## 4. Module cần thêm

```text
app/
├── chatbot/
│   ├── __init__.py
│   ├── models.py              # Pydantic request/response + domain models
│   ├── store.py               # Postgres CRUD
│   ├── retriever.py           # search Chroma products + knowledge
│   ├── generator.py           # gọi LLM router
│   ├── orchestrator.py        # xử lý 1 lượt chat
│   ├── image_understanding.py # phase 2: vision
│   └── router.py              # API nội bộ quản lý chatbot
├── zalo/
│   ├── __init__.py
│   ├── client.py              # gửi text/card, download attachment
│   ├── token_manager.py       # refresh token OA
│   └── webhook.py             # parse event + signature verify
└── main.py                    # include router
```

Không đưa logic chatbot vào `app/main.py` để tránh file chính tiếp tục phình to.

## 5. Database đề xuất

Thêm migration trong `app/postgres.py`.

### `chatbot_products`

Lưu catalog phục vụ tư vấn.

Trường chính:

- `product_id`
- `site_id` hoặc `channel`
- `title`
- `description`
- `price`
- `currency`
- `image_url`
- `product_url`
- `category`
- `brand`
- `attributes jsonb`
- `status`
- `updated_at`
- `data jsonb`

### `chatbot_sessions`

Một session theo user/channel.

- `session_id`
- `channel` = `zalo`
- `external_user_id`
- `display_name`
- `status` = `bot_active | human_handoff | closed`
- `last_message_at`
- `summary`
- `data jsonb`

### `chatbot_messages`

Lưu message inbound/outbound.

- `message_id`
- `session_id`
- `channel`
- `direction` = `inbound | outbound`
- `message_type` = `text | image | product_card | system`
- `content`
- `attachments jsonb`
- `llm_metadata jsonb`
- `created_at`

### `chatbot_events`

Lưu webhook raw và lỗi xử lý.

- `event_id`
- `channel`
- `event_name`
- `payload jsonb`
- `status`
- `error`
- `created_at`

## 6. Chroma collections

Giữ RAG kiến thức hiện tại cho bài viết và kiến thức chung:

```text
knowledge_base_dangvantuan_vietnamese-embedding
```

Thêm collection riêng cho sản phẩm chatbot:

```text
chatbot_products_dangvantuan_vietnamese-embedding
```

Lý do tách:

- Sản phẩm cần metadata khác knowledge.
- Có thể reindex catalog mà không ảnh hưởng RAG bài viết.
- Search sản phẩm cần filter theo site/category/status.

Document nên index theo format:

```text
Tên: ...
Giá: ...
Danh mục: ...
Thương hiệu: ...
Mô tả ngắn: ...
Thuộc tính: ...
Tình huống phù hợp: ...
Từ khóa: ...
```

Metadata:

```json
{
  "product_id": "SP001",
  "title": "Áo thun nam cotton premium",
  "price": 299000,
  "category": "ao-thun-nam",
  "brand": "Example",
  "image_url": "https://...",
  "product_url": "https://...",
  "status": "active"
}
```

## 7. LLM router

Không gọi OpenAI trực tiếp trong chatbot. Dùng router hiện tại:

```env
ROUTER_BASE=http://cliproxy:8317/v1
ROUTER_KEY=...
LLM_MODEL_WRITER=gpt-5.4
LLM_MODEL_EXTRACT_PLANNER=gpt-5.4-mini
```

Model dùng theo tác vụ:

- Intent classification: `LLM_MODEL_EXTRACT_PLANNER`.
- Reply generation: `LLM_MODEL_WRITER` hoặc alias `fast` nếu cần tiết kiệm quota.
- Vision phase 2: dùng model vision qua router nếu Cliproxy hỗ trợ image input; nếu chưa hỗ trợ thì thêm provider vision riêng sau.

## 8. Prompt nguyên tắc

System prompt chatbot:

```text
Bạn là tư vấn viên bán hàng tiếng Việt.

Quy tắc:
- Chỉ tư vấn dựa trên sản phẩm và chính sách được cung cấp.
- Không bịa giá, tồn kho, khuyến mãi.
- Nếu thiếu thông tin, hỏi lại ngắn gọn.
- Trả lời tự nhiên, thân thiện, không dài dòng.
- Ưu tiên chốt nhu cầu: ngân sách, kích thước, mẫu mã, mục đích sử dụng.
- Kết thúc bằng CTA nhẹ: anh/chị muốn em gửi mẫu phù hợp không?
- Khi cần nhân viên, trả về intent human_handoff.
```

Input cho generator:

```json
{
  "customer_message": "...",
  "conversation_summary": "...",
  "recent_history": [],
  "matched_products": [],
  "knowledge_facts": [],
  "business_rules": []
}
```

Output nội bộ nên ép JSON:

```json
{
  "reply": "text gửi khách",
  "intent": "product_consulting",
  "handoff": false,
  "product_ids": ["SP001", "SP002"],
  "confidence": 0.82
}
```

Sau đó backend mới quyết định gửi product cards.

## 9. Zalo integration

### Endpoint

Thêm endpoint:

```text
GET  /api/chatbot/zalo/webhook      # verify nếu Zalo yêu cầu
POST /api/chatbot/zalo/webhook      # nhận events
```

Events MVP:

- `user_send_text`
- `user_send_image` nhận nhưng trả lời fallback ở phase 1
- `user_send_sticker` trả lời gợi mở
- `follow`

### Token

Lưu token vào Postgres hoặc Redis:

- Access token cache ngắn hạn.
- Refresh token lưu bền trong Postgres/settings.
- Khi access token lỗi hết hạn, refresh và retry một lần.

Không hard-code token trong code.

### Signature

Webhook phải verify signature trước khi xử lý production. Không để đoạn verify bị comment như bản nháp cũ.

## 10. Luồng xử lý text

```text
receive webhook
→ verify signature
→ normalize event
→ upsert session
→ insert inbound message
→ if session.status == human_handoff: stop bot
→ classify intent
→ retrieve products
→ retrieve knowledge/policies
→ generate reply JSON
→ send text to Zalo
→ send product cards if product_ids
→ insert outbound messages
→ publish realtime event for dashboard
```

## 11. Luồng xử lý ảnh

Phase 1:

- Nhận ảnh.
- Lưu attachment metadata, không cần lưu file lâu dài.
- Trả lời: “Anh/chị cho em thêm mô tả/sản phẩm cần tìm để em tư vấn đúng hơn.”

Phase 2:

- Download ảnh tạm.
- Resize ảnh.
- Vision LLM mô tả ảnh thành text.
- Search `chatbot_products`.
- Trả lời kèm product cards.
- Xóa file tạm sau xử lý.

Không bật CLIP ngay. CLIP chỉ đáng làm khi catalog lớn hoặc lượng ảnh cao.

## 12. UI quản lý trong Content Forge

Thêm trang `AI CHATBOT` hoặc tab trong khu vực social.

MVP UI:

- Zalo connection status.
- Cấu hình OA: app id, secret, refresh token, webhook URL.
- Danh sách sản phẩm chatbot.
- Button reindex products.
- Log hội thoại gần đây.
- Bật/tắt bot theo session.
- Prompt settings.
- Test chat sandbox.

Phase 2 UI:

- Analytics: số hội thoại, intent, handoff, sản phẩm được gợi ý.
- Review câu trả lời AI.
- Import sản phẩm từ Shopee/Woo/site hiện có.
- Gán knowledge category cho chatbot.

## 13. Product source

Có 3 nguồn sản phẩm có thể dùng lại:

1. Shopee affiliate products đã lưu trong `shopee_products`.
2. Website/Woo product pipeline hiện có.
3. Upload CSV/JSON riêng cho chatbot.

MVP nên dùng bảng `chatbot_products` riêng và có job sync từ nguồn khác vào. Không query trực tiếp `shopee_products` khi trả lời khách, vì dữ liệu Shopee có thể nhiều field nhiễu.

## 14. API nội bộ đề xuất

```text
GET  /api/chatbot/products
POST /api/chatbot/products/import
POST /api/chatbot/products/reindex
GET  /api/chatbot/sessions
GET  /api/chatbot/sessions/{session_id}
POST /api/chatbot/sessions/{session_id}/handoff
POST /api/chatbot/test
GET  /api/chatbot/settings
PUT  /api/chatbot/settings
```

## 15. Bảo mật và vận hành

Yêu cầu bắt buộc:

- HTTPS webhook.
- Verify Zalo signature.
- Không log token.
- Rate limit webhook theo user/session.
- Timeout LLM rõ ràng.
- Retry gửi Zalo tối đa 1-2 lần.
- Lưu raw webhook để debug.
- Có nút tắt bot/handoff.

Giám sát:

```bash
docker compose -f ../docker-compose.yml logs -f fastapi | grep -iE "chatbot|zalo|webhook|error"
docker compose -f ../docker-compose.yml exec -T postgres psql -U content_forge -d content_forge -c "select status, count(*) from chatbot_messages group by status;"
```

## 16. Roadmap triển khai

### Phase 1: Zalo text MVP

- DB tables.
- Zalo token manager.
- Webhook receive.
- Text reply.
- Product retriever.
- Product card.
- Chat logs.

Thời gian ước tính: 3-5 ngày nếu token Zalo/API không vướng review.

### Phase 2: UI quản lý và handoff

- Trang quản lý chatbot.
- Danh sách session/message.
- Bật/tắt bot theo session.
- Test sandbox.
- Reindex products.

Thời gian ước tính: 3-5 ngày.

### Phase 3: Image understanding

- Download ảnh từ Zalo.
- Vision LLM mô tả ảnh.
- Search sản phẩm từ mô tả.
- Cache mô tả ảnh theo hash.

Thời gian ước tính: 2-4 ngày.

### Phase 4: Tối ưu chất lượng

- Hybrid search keyword + vector.
- Reranking top products.
- Conversation summary.
- Prompt A/B.
- Analytics chuyển đổi.

## 17. Rủi ro chính

- Zalo OA API có thể yêu cầu review/quyền trước khi gửi/nhận đủ event.
- Token Zalo dễ lỗi nếu refresh flow không chuẩn.
- Product data kém thì bot tư vấn sai.
- LLM có thể bịa nếu prompt không ép chỉ dùng context.
- Vision ảnh sản phẩm có thể sai nếu ảnh mờ hoặc nhiều vật thể.
- Chroma embedding model cần được giữ ổn định; đổi model phải reindex collection.

## 18. Quyết định kỹ thuật cuối

- Tích hợp vào Content Forge, không tạo repo/service riêng.
- Dùng Postgres hiện tại, không MariaDB.
- Dùng Redis hiện tại.
- Dùng Chroma hiện tại nhưng collection riêng cho sản phẩm chatbot.
- Dùng LLM router hiện tại, không gọi OpenAI trực tiếp.
- MVP ưu tiên text trước, ảnh sau.
- Mọi logic nằm trong module riêng `app/chatbot` và `app/zalo`.

