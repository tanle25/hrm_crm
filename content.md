# 🧾 TÀI LIỆU CHUẨN NỘI DUNG SẢN PHẨM WOOCOMMERCE (SEO + GEO + AI-FRIENDLY)

> Tài liệu này dùng để:
> - Chuẩn hóa nội dung sản phẩm
> - Tối ưu SEO (Google)
> - Tối ưu GEO (AI như ChatGPT, Gemini)
> - Làm input/output cho hệ thống agent tự động

---

# 🧱 1. Tư duy cốt lõi

Một trang sản phẩm chuẩn cần phục vụ 3 tầng:

- 👤 Người dùng → dễ đọc, dễ hiểu, dễ mua  
- 🔍 Google → dễ crawl, dễ index  
- 🤖 AI → dễ trích xuất, dễ tóm tắt  

👉 Công thức:
**Structured Content + Semantic + FAQ + Bullet = TOP SEO + GEO**

---

# 🧱 2. Cấu trúc chuẩn trang sản phẩm (Full Template)

---

## 1. 🟢 Tiêu đề sản phẩm (H1)

### Công thức:
[Tên sản phẩm] + [thuộc tính chính] + [USP]

### Ví dụ:
Điếu Cày Trúc Bọc Đồng Cao Cấp 40cm – Gọn Nhẹ, Hút Êm, Có Bao Da

👉 SEO: chứa từ khóa chính  
👉 GEO: AI dễ hiểu entity + đặc điểm  

---

## 2. 🟡 Đoạn mô tả ngắn (Short Description)

👉 Quan trọng nhất cho:
- Featured snippet  
- AI summary  

### Cấu trúc:
- 2–3 câu  
- Trả lời nhanh: sản phẩm là gì + dành cho ai + điểm mạnh  

### Template:
[Tên sản phẩm] là [loại sản phẩm] được thiết kế dành cho [đối tượng].  
Sản phẩm nổi bật với [3 lợi ích chính].  
Phù hợp sử dụng cho [ngữ cảnh].  

---

## 3. 🔥 Khối “Lý do nên mua” (AI rất thích)

👉 Dạng bullet → AI extract cực tốt  

✔ Chất liệu: ...  
✔ Thiết kế: ...  
✔ Trải nghiệm: ...  
✔ Độ bền: ...  
✔ Phụ kiện: ...  

👉 Đây là block GEO cực mạnh  

---

## 4. 🧩 Thông tin chi tiết sản phẩm

👉 Viết theo dạng section rõ ràng + heading H2/H3  

---

### 4.1. Tổng quan sản phẩm (H2)

- Mô tả tự nhiên  
- Kể câu chuyện / bối cảnh sử dụng  

---

### 4.2. Thông số kỹ thuật (H2)

👉 BẮT BUỘC dạng list hoặc bảng  

- Chiều dài: 40cm (±2cm)  
- Chất liệu: Trúc + đồng  
- Trọng lượng: ...  
- Kiểu hút: ...  

👉 Google + AI cực thích format này  

---

### 4.3. Công dụng / lợi ích (H2)

### Lợi ích 1  
Mô tả  

### Lợi ích 2  
Mô tả  

---

### 4.4. Đối tượng sử dụng (H2)

Phù hợp với:
- Người mới  
- Người chơi lâu năm  
- Người thích nhỏ gọn  

👉 Đây là GEO keyword cực tốt  

---

### 4.5. Hướng dẫn sử dụng (H2)

👉 Tăng trust + SEO  

---

### 4.6. Bảo quản (H2)

👉 Google đánh giá cao E-E-A-T  

---

## 5. 🧠 FAQ (SIÊU QUAN TRỌNG)

👉 Đây là vũ khí GEO mạnh nhất  

### Format:

### Điếu cày này có dễ vệ sinh không?  
Có, sản phẩm có thể tháo rời...

### Có phù hợp cho người mới không?  
...

👉 Giúp:
- Rank Google  
- AI lấy làm nguồn trả lời  

---

## 6. ⭐ Review / đánh giá

👉 Nếu chưa có user:

Đánh giá từ người dùng:
- 95% hài lòng về độ êm  
- 90% đánh giá dễ sử dụng  

---

## 7. 🧩 Internal Linking

👉 RẤT QUAN TRỌNG  

Xem thêm:
- Điếu cày 60cm  
- Phụ kiện điếu cày  

---

## 8. 📊 Schema (BẮT BUỘC)

```json
{
  "@context": "https://schema.org/",
  "@type": "Product",
  "name": "...",
  "description": "...",
  "brand": "...",
  "offers": {
    "@type": "Offer",
    "price": "...",
    "priceCurrency": "VND",
    "availability": "https://schema.org/InStock"
  }
}