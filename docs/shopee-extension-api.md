# Shopee Extension API

Tài liệu này mô tả các API để Chrome extension đẩy sản phẩm Shopee sang Content Forge.

Base URL local:

```text
http://127.0.0.1:8000/api
```

Authentication:

- Tạo token ở màn `Settings` trong UI.
- Dùng header:

```http
Authorization: Bearer <your_extension_token>
```

Mục tiêu:

- Extension gửi sản phẩm Shopee dạng raw sang backend.
- Backend chuẩn hóa sang cấu trúc WooCommerce `simple` hoặc `variable`.
- UI hiển thị danh sách sản phẩm đã chuẩn hóa.
- Người dùng chọn site và tạo job viết lại nội dung từ sản phẩm đã chuẩn hóa đó.

## 1. Upsert sản phẩm Shopee

Endpoint:

```http
POST /api/shopee/products
Content-Type: application/json
Authorization: Bearer <your_extension_token>
```

Body:

```json
{
  "product": {
    "url": "https://shopee.vn/...",
    "title": "Tên sản phẩm",
    "price": 3750000,
    "shortDescription": "Mô tả ngắn",
    "description": "Mô tả đầy đủ",
    "images": [
      "https://down-vn.img.susercontent.com/file/..."
    ],
    "attributes": [
      { "name": "Chất liệu", "value": "Nhựa ABS, Kim loại" }
    ],
    "rating": 5,
    "itemId": "22085816825",
    "shopId": "572738017",
    "variants": [
      {
        "modelId": "166204834470",
        "name": "ABM-8688 Ghi",
        "price": 3750000,
        "priceBeforeDiscount": 5000000,
        "stock": 10,
        "image": "https://down-vn.img.susercontent.com/file/...",
        "tierIndex": [0]
      }
    ],
    "tierVariations": [
      {
        "name": "Màu sắc",
        "options": ["ABM-8688 Ghi", "ABM-8688 Đỏ", "ABM-8688 Xanh"]
      }
    ],
    "variantCount": 3,
    "hasVariants": true,
    "currency": "VND"
  }
}
```

Yêu cầu tối thiểu:

- `product.itemId`
- `product.title`

Khuyến nghị gửi đầy đủ:

- `url`
- `description`
- `images`
- `attributes`
- `variants`
- `tierVariations`

Response `200`:

```json
{
  "item_id": "22085816825",
  "raw": { "...": "raw product" },
  "normalized": {
    "source": "shopee",
    "item_id": "22085816825",
    "shop_id": "572738017",
    "source_url": "https://shopee.vn/...",
    "product_title": "Tên sản phẩm",
    "product_slug": "ten-san-pham",
    "type": "variable",
    "regular_price": 3750000,
    "sale_price": 5000000,
    "short_description": "Mô tả ngắn",
    "description_text": "Mô tả đã làm sạch",
    "images": ["https://..."],
    "attributes": [],
    "variations": [],
    "raw_variant_count": 3,
    "currency": "VND",
    "seed_content": "..."
  }
}
```

## 2. Danh sách sản phẩm đã chuẩn hóa

Endpoint:

```http
GET /api/shopee/products
```

Query hỗ trợ:

- `search`
- `limit`

Ví dụ:

```http
GET /api/shopee/products?search=abm8688&limit=50
```

Response:

```json
{
  "source_url": "chrome-extension",
  "category_label": "Shopee normalized catalog",
  "total": 1,
  "items": [
    {
      "item_id": "22085816825",
      "shop_id": "572738017",
      "title": "Tên sản phẩm",
      "type": "variable",
      "regular_price": 3750000,
      "sale_price": 5000000,
      "variant_count": 3,
      "image_count": 8,
      "url": "https://shopee.vn/...",
      "updated_at": "2026-04-24T02:49:10.780079"
    }
  ]
}
```

## 3. Chi tiết một sản phẩm đã chuẩn hóa

Endpoint:

```http
GET /api/shopee/products/{item_id}
```

Ví dụ:

```http
GET /api/shopee/products/22085816825
```

Response:

- trả lại `raw`
- trả lại `normalized`

UI dùng endpoint này để hiển thị chi tiết sản phẩm đã chuẩn hóa.

## 4. Tạo job viết lại nội dung từ sản phẩm Shopee

Endpoint:

```http
POST /api/shopee/products/{item_id}/enqueue
Content-Type: application/json
```

Body:

```json
{
  "site_ids": ["fd023cce1699422890435171dd0f979d"],
  "content_mode": "shared",
  "publish_status": "draft",
  "woo_category_id": 1,
  "priority": "normal"
}
```

Ý nghĩa:

- `site_ids`: danh sách site đích
- `content_mode`:
  - `shared`: viết một lần rồi publish nhiều site
  - `per-site`: viết riêng cho từng site
- `publish_status`:
  - `draft`
  - `publish`
- `woo_category_id`: category Woo mặc định
- `priority`:
  - `normal`
  - `high`

Response:

```json
{
  "batch_id": "b6b6d3720032415fad27b26bda83b43e",
  "status": "queued",
  "total_jobs": 2,
  "master_job_ids": ["9dd3a40f50654fc78ee928089b8c2ac1"],
  "child_job_ids": ["d928c03b74bf44d1aa8bed8763d61ffd"]
}
```

## 5. Gợi ý workflow cho extension

Luồng khuyến nghị:

1. Crawl raw product từ Shopee.
2. Gọi `POST /api/shopee/products`.
3. Nếu cần kiểm tra, gọi `GET /api/shopee/products/{item_id}`.
4. Người dùng vào UI để chọn site và enqueue.

Nếu muốn extension tự enqueue luôn, có thể gọi tiếp:

5. `POST /api/shopee/products/{item_id}/enqueue`

## 6. Ví dụ `fetch()` từ Chrome extension

```js
async function pushShopeeProduct(product) {
  const response = await fetch("http://127.0.0.1:8000/api/shopee/products", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Authorization": `Bearer ${token}`,
    },
    body: JSON.stringify({ product }),
  });

  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `HTTP ${response.status}`);
  }

  return response.json();
}
```

## 7. Lưu ý implement phía extension

- Nên luôn gửi `itemId` ổn định. Backend dùng `itemId` làm khóa upsert.
- Có thể gửi lại cùng một sản phẩm nhiều lần. Backend sẽ update bản ghi cũ.
- Nếu sản phẩm có biến thể, nên gửi cả:
  - `variants`
  - `tierVariations`
- Không cần tự chuẩn hóa sang Woo ở extension. Backend sẽ làm việc đó.
- Không cần gửi HTML. Text sạch là đủ.

## 8. Mapping normalize hiện tại

Backend hiện tự map như sau:

- `title` -> `normalized.product_title`
- `itemId` -> `normalized.item_id`
- `shopId` -> `normalized.shop_id`
- `url` -> `normalized.source_url`
- `images` -> `normalized.images`
- `shortDescription` -> `normalized.short_description`
- `description` -> `normalized.description_text`
- `variants + tierVariations` -> `normalized.variations`
- `attributes` -> `normalized.attributes`
- có biến thể -> `type = variable`
- không có biến thể -> `type = simple`

## 9. Các mã lỗi thường gặp

`400 Bad Request`

- thiếu `product`
- thiếu `product.itemId`

`404 Not Found`

- `item_id` không tồn tại khi gọi detail hoặc enqueue
- `site_id` không tồn tại khi enqueue

`429 Too Many Requests`

- queue đang đầy

`401 Unauthorized`

- thiếu token
- token không hợp lệ
- token đã bị xóa

## 10. Trạng thái hiện tại

- API đã chạy trên backend hiện tại
- UI đã đọc danh sách sản phẩm đã chuẩn hóa từ store
- Extension chỉ cần gọi `POST /api/shopee/products` với `Bearer token` để đẩy dữ liệu sang
