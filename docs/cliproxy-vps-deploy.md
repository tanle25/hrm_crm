# CLIProxyAPI On VPS

Tài liệu này dùng cho trường hợp anh đang chạy `CLIProxyAPI` local và muốn đưa nó lên VPS để dùng ổn định qua domain riêng.

## Mục tiêu

- `CLIProxyAPI` chỉ nghe ở `127.0.0.1:8317`
- public ra ngoài qua `Nginx + HTTPS`
- dùng `api-key` để client gọi API
- không mở management API ra internet nếu chưa thật sự cần

## File deploy có sẵn

- `deploy/cliproxy/config.yaml`
- `deploy/cliproxy/docker-compose.yml`
- `deploy/cliproxy/nginx.conf`

## Cấu trúc thư mục trên VPS

```text
/opt/cliproxy/
  docker-compose.yml
  config.yaml
  auth/
```

## Bước triển khai

1. Tạo thư mục:

```bash
mkdir -p /opt/cliproxy/auth
cd /opt/cliproxy
```

2. Copy hai file:

- `deploy/cliproxy/docker-compose.yml`
- `deploy/cliproxy/config.yaml`

3. Sửa `config.yaml`:

- đổi `api-keys`
- thêm cấu hình provider/account thật của anh
- chỉ bật `remote-management.allow-remote: true` nếu anh thực sự cần quản trị từ xa
- nếu bật management từ xa thì bắt buộc điền `remote-management.secret-key` mạnh

4. Chạy:

```bash
docker compose up -d
docker compose logs -f cliproxy
```

5. Kiểm tra local trên VPS:

```bash
curl http://127.0.0.1:8317/v1/models \
  -H "Authorization: Bearer replace-with-a-strong-api-key"
```

## Reverse Proxy Với Nginx

1. Copy `deploy/cliproxy/nginx.conf`
2. Đổi `ai.example.com` thành domain thật
3. Cấp SSL bằng Let's Encrypt
4. Reload nginx

Ví dụ kiểm tra:

```bash
curl https://ai.example.com/v1/models \
  -H "Authorization: Bearer your-api-key"
```

## Firewall

Nên mở:

- `80/tcp`
- `443/tcp`

Không nên mở trực tiếp:

- `8317/tcp`

## Những điểm quan trọng

- `auth/` phải persistent, vì đây là nơi `CLIProxyAPI` giữ dữ liệu auth.
- `api-keys` là lớp bảo vệ cho client gọi API.
- `remote-management.secret-key` là lớp bảo vệ riêng cho management API.
- Nếu chưa cần panel quản trị từ xa, giữ:

```yaml
remote-management:
  allow-remote: false
  secret-key: ""
```

## Cách dùng từ app hiện tại

Nếu anh muốn app hiện tại gọi `CLIProxyAPI` trên VPS, có thể đổi:

```env
ROUTER_BASE=https://ai.example.com/v1
ROUTER_KEY=your-api-key
```

## Quy trình vận hành khuyến nghị

- VPS chạy `CLIProxyAPI`
- app nội dung của anh gọi qua domain HTTPS
- rotate `api-key` nếu nghi lộ
- backup:
  - `config.yaml`
  - thư mục `auth/`

## Khi nào nên bật management API

Chỉ bật khi anh thật sự cần xem panel hoặc quản lý từ xa.

Nếu bật:

```yaml
remote-management:
  allow-remote: true
  secret-key: "replace-with-a-very-strong-secret"
```

Và vẫn nên đặt sau HTTPS.
