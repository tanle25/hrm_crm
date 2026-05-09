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
- UI đã có khu vực danh sách sản phẩm và danh mục sản phẩm nhưng đang trống; đây sẽ là catalog chính cho chatbot.

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
   ├─ Postgres: catalog products, categories, variants, sessions, messages
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
│   ├── catalog_store.py       # CRUD sản phẩm/danh mục/biến thể
│   ├── store.py               # CRUD session/message/settings
│   ├── retriever.py           # search Chroma products + knowledge
│   ├── indexer.py             # RAG/index catalog vào Chroma
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

Catalog sản phẩm/danh mục hiện đang để trống trong UI sẽ được dùng làm nguồn dữ liệu chính cho chatbot. Không tạo một catalog tách rời chỉ để bot trả lời. Chatbot đọc từ catalog này, sau đó index sang Chroma để truy vấn ngữ nghĩa.

### `product_categories`

Danh mục sản phẩm dùng cho UI và chatbot.

Trường chính:

- `category_id`
- `name`
- `slug`
- `description`
- `parent_id`
- `status` = `active | inactive`
- `sort_order`
- `data jsonb`
- `updated_at`

### `products`

Sản phẩm phục vụ quản lý catalog và tư vấn.

Trường chính:

- `product_id`
- `title`
- `slug`
- `description`
- `short_description`
- `price`
- `compare_at_price`
- `currency`
- `category_id`
- `brand`
- `labels text[]`
- `images jsonb`
- `attributes jsonb`
- `variants jsonb`
- `is_active boolean`
- `product_url`
- `source`
- `source_id`
- `updated_at`
- `data jsonb`

Không lưu số lượng tồn kho ở MVP. Khi hết hàng, người dùng bật/tắt sản phẩm bằng `is_active`, nhưng trạng thái này chỉ là tín hiệu tư vấn cho chatbot. Sản phẩm vẫn được index vào RAG để AI biết catalog đầy đủ và có thể nói rõ sản phẩm đang tạm hết hàng/ngừng bán.

### `product_variants`

Biến thể sản phẩm. Có thể lưu bảng riêng để dễ bật/tắt từng biến thể, hoặc lưu trong `products.variants` nếu muốn MVP nhanh. Nếu đã xác định chatbot cần tư vấn theo biến thể thì nên dùng bảng riêng.

Trường chính:

- `variant_id`
- `product_id`
- `name`
- `sku`
- `price`
- `compare_at_price`
- `currency`
- `image_url`
- `attributes jsonb`
- `labels text[]`
- `is_active boolean`
- `sort_order`
- `updated_at`
- `data jsonb`

Không lưu số lượng tồn kho. Khi hết hàng, tắt biến thể bằng `is_active=false`. Biến thể vẫn được index vào RAG với trạng thái `availability_status=unavailable`; chatbot không được nói là còn hàng, nhưng vẫn có thể dùng thông tin đó để giải thích và gợi ý biến thể thay thế.

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

Thêm collection riêng cho catalog sản phẩm:

```text
chatbot_products_dangvantuan_vietnamese-embedding
```

Lý do tách:

- Sản phẩm cần metadata khác knowledge.
- Có thể reindex catalog mà không ảnh hưởng RAG bài viết.
- Search sản phẩm cần filter theo site/category/status.

Mỗi sản phẩm được index thành ít nhất 1 document tổng quan, bao gồm cả sản phẩm đang tắt/hết hàng. Mỗi biến thể nên được index thành document riêng để bot hiểu đúng màu/size/mẫu/giá và trạng thái còn/hết hàng.

Document sản phẩm nên index theo format:

```text
Tên: ...
Giá: ...
Danh mục: ...
Thương hiệu: ...
Mô tả ngắn: ...
Mô tả chi tiết: ...
Nhãn: ...
Ảnh: ...
Thuộc tính: ...
Biến thể và trạng thái: ...
Tình huống phù hợp: ...
Từ khóa: ...
```

Document biến thể:

```text
Sản phẩm: ...
Biến thể: ...
Giá biến thể: ...
Thuộc tính biến thể: ...
Nhãn: ...
Ảnh biến thể: ...
Trạng thái: còn hàng | tạm hết hàng | ngừng bán
```

Metadata:

```json
{
  "product_id": "SP001",
  "variant_id": "SP001-BLACK-M",
  "title": "Áo thun nam cotton premium",
  "price": 299000,
  "currency": "VND",
  "category": "ao-thun-nam",
  "brand": "Example",
  "labels": "basic, cotton, mùa hè",
  "image_urls": "https://.../1.jpg, https://.../2.jpg",
  "product_url": "https://...",
  "is_active": true,
  "availability_status": "available",
  "doc_type": "variant"
}
```

Index tất cả product/variant, không loại khỏi RAG chỉ vì `is_active=false`. Retriever có thể ưu tiên item `availability_status=available`, nhưng prompt bắt buộc AI phải đọc trạng thái và tư vấn đúng: item hết hàng thì báo hết hàng, không hứa có thể mua, đồng thời gợi ý sản phẩm/biến thể còn hàng phù hợp.

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
- Luôn đọc `availability_status` của sản phẩm/biến thể trước khi tư vấn.
- Nếu sản phẩm/biến thể đang hết hàng hoặc ngừng bán, nói rõ trạng thái và gợi ý lựa chọn còn hàng gần nhất.
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
- Danh sách danh mục sản phẩm.
- Danh sách sản phẩm: ảnh, giá, mô tả, nhãn, trạng thái bật/tắt.
- Chi tiết sản phẩm: nhiều ảnh, nhiều biến thể, toggle từng biến thể.
- Button reindex/RAG products vào Chroma.
- Log hội thoại gần đây.
- Bật/tắt bot theo session.
- Prompt settings.
- Test chat sandbox.

Phase 2 UI:

- Analytics: số hội thoại, intent, handoff, sản phẩm được gợi ý.
- Review câu trả lời AI.
- Import sản phẩm từ Shopee/Woo/site hiện có.
- Gán knowledge category cho chatbot.

## 13. Catalog sản phẩm và RAG

Catalog sản phẩm trong Content Forge là nguồn sự thật cho chatbot. Các nguồn khác chỉ là nguồn nhập/sync vào catalog này.

Nguồn nhập có thể gồm:

1. Nhập thủ công trong UI.
2. Shopee affiliate products đã lưu trong `shopee_products`.
3. Website/Woo product pipeline hiện có.
4. Upload CSV/JSON riêng.

Quy tắc dữ liệu:

- Sản phẩm có nhiều hình ảnh `images`.
- Sản phẩm có giá chính `price`.
- Sản phẩm có nhiều biến thể `variants`.
- Mỗi biến thể có thể có giá riêng, ảnh riêng, thuộc tính riêng.
- Sản phẩm và biến thể có `labels` để tăng khả năng search/tư vấn.
- Không dùng số lượng tồn kho trong MVP.
- Hết hàng thì tắt sản phẩm hoặc tắt biến thể bằng toggle.
- Toggle không quyết định có đưa vào RAG hay không; nó chỉ cập nhật `availability_status`.
- Chatbot vẫn biết sản phẩm/biến thể đang tắt, nhưng phải nói rõ trạng thái và ưu tiên gợi ý item còn hàng.

Reindex/RAG catalog:

```text
product/category updated
→ mark catalog dirty
→ user bấm reindex hoặc job tự chạy nền
→ build documents từ product + variants
→ upsert vào collection chatbot_products_*
→ update vectors khi trạng thái còn/hết hàng thay đổi
→ chỉ delete vectors khi product/variant bị xóa hẳn
```

Khi sinh câu trả lời, bot lấy dữ liệu từ Chroma để chọn ứng viên, sau đó hydrate lại từ Postgres để đảm bảo giá, ảnh, trạng thái mới nhất.

## 14. API nội bộ đề xuất

```text
GET  /api/chatbot/products
POST /api/chatbot/products/import
GET  /api/chatbot/products/{product_id}
POST /api/chatbot/products
PUT  /api/chatbot/products/{product_id}
DELETE /api/chatbot/products/{product_id}
POST /api/chatbot/products/{product_id}/toggle
POST /api/chatbot/products/{product_id}/variants/{variant_id}/toggle
POST /api/chatbot/products/reindex
GET  /api/chatbot/categories
POST /api/chatbot/categories
PUT  /api/chatbot/categories/{category_id}
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
- Product/category catalog UI nền tảng.
- Product/variant active toggles.
- Product RAG indexer.
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
- Toggle active không đồng bộ với Chroma thì bot có thể báo sai trạng thái còn/hết hàng.
- LLM có thể bịa nếu prompt không ép chỉ dùng context.
- Vision ảnh sản phẩm có thể sai nếu ảnh mờ hoặc nhiều vật thể.
- Chroma embedding model cần được giữ ổn định; đổi model phải reindex collection.

## 18. Quyết định kỹ thuật cuối

- Tích hợp vào Content Forge, không tạo repo/service riêng.
- Dùng Postgres hiện tại, không MariaDB.
- Dùng Redis hiện tại.
- Dùng catalog sản phẩm/danh mục hiện có trong UI làm nguồn sự thật.
- Dùng toggle `is_active` cho sản phẩm/biến thể thay cho số lượng tồn kho.
- RAG index toàn bộ catalog; trạng thái `is_active/availability_status` chỉ dùng để AI tư vấn đúng và ưu tiên item còn hàng.
- Dùng Chroma hiện tại nhưng collection riêng cho catalog sản phẩm chatbot.
- Dùng LLM router hiện tại, không gọi OpenAI trực tiếp.
- MVP ưu tiên text trước, ảnh sau.
- Mọi logic nằm trong module riêng `app/chatbot` và `app/zalo`.
