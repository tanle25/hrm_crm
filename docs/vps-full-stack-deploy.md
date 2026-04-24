# Full VPS Deploy: Content Forge + CLIProxyAPI

Tài liệu này dành cho trường hợp anh muốn đưa **cả app hiện tại** và **CLIProxyAPI** lên cùng một VPS.

## Kiến trúc khuyến nghị

- `nginx`
- `fastapi`
- `worker`
- `dlq_worker`
- `postgres`
- `redis`
- `cliproxy`

App gọi LLM qua mạng Docker nội bộ:

```text
fastapi / worker -> http://cliproxy:8317/v1
```

Tức là trên VPS:

- không cần `ROUTER_BASE=http://localhost:8317/v1`
- không cần mở public port `8317`
- chỉ expose `80/443`

## File đã có sẵn

- `deploy/vps-full-stack/docker-compose.yml`
- `deploy/vps-full-stack/.env.example`
- `deploy/vps-full-stack/cliproxy/config.yaml`
- `deploy/vps-full-stack/nginx/default.conf`

## Khi nào mô hình này phù hợp

Phù hợp khi:

- anh publish lên các site WordPress/WooCommerce từ xa
- không phụ thuộc vào WordPress local path trên máy cá nhân
- muốn worker chạy luôn trong Docker trên VPS

Không phù hợp nếu:

- anh vẫn cần publish vào một WordPress local nằm trên laptop/mac của anh
- anh vẫn cần dùng `WOO_LOCAL_SITE_PATH` kiểu `/Users/...`

Trên VPS, nên dùng site cấu hình qua UI với URL/credentials thật của từng site.

## Các bước triển khai

1. Chuẩn bị server:

- Ubuntu/Debian
- Docker + Docker Compose plugin
- domain trỏ về VPS, ví dụ `forge.example.com`

2. Copy thư mục deploy lên VPS:

```bash
mkdir -p /opt/content-forge
cd /opt/content-forge
```

Copy các file:

- `deploy/vps-full-stack/docker-compose.yml`
- `deploy/vps-full-stack/.env.example` -> đổi tên thành `.env`
- `deploy/vps-full-stack/cliproxy/config.yaml`
- `deploy/vps-full-stack/nginx/default.conf`

3. Copy source app lên VPS:

Đơn giản nhất:

```bash
git clone <repo-cua-anh> app
```

Sau đó sửa `docker-compose.yml` nếu cần để `build.context` đúng vị trí source.

Mặc định file hiện tại giả định:

```text
/opt/content-forge/
  docker-compose.yml
  .env
  cliproxy/
  nginx/
  app/   # repo này
```

Nếu anh giữ đúng layout đó, đổi trong `docker-compose.yml`:

- `context: ../..`

thành:

- `context: ./app`

và `dockerfile: Dockerfile` giữ nguyên.

## Layout khuyến nghị trên VPS

```text
/opt/content-forge/
  docker-compose.yml
  .env
  cliproxy/
    config.yaml
    auth/
  nginx/
    default.conf
  certbot/
    conf/
    www/
  app/
    Dockerfile
    app/
    ui/
    requirements.txt
    ...
```

## Các chỉnh sửa bắt buộc

### 1. `.env`

Điền tối thiểu:

- `POSTGRES_PASSWORD`
- `AUTH_USERNAME`
- `AUTH_PASSWORD`
- `AUTH_SECRET`
- `ROUTER_KEY`

### 2. `cliproxy/config.yaml`

Điền:

- `api-keys`
- toàn bộ provider/account config thật của anh

### 3. `nginx/default.conf`

Đổi:

- `forge.example.com`

thành domain thật.

## Khởi động stack

```bash
docker compose build
docker compose up -d
docker compose ps
```

## Cấp SSL

Anh có thể dùng `certbot` ngoài container hoặc bổ sung service certbot sau.

Nếu đã có cert sẵn trên VPS, chỉ cần mount vào:

```text
./certbot/conf:/etc/letsencrypt
```

Sau đó reload nginx:

```bash
docker compose restart nginx
```

## Kiểm tra sau deploy

### 1. FastAPI

```bash
curl http://127.0.0.1:8000/health
```

### 2. CLIProxyAPI trong network

```bash
docker compose exec fastapi sh -lc 'wget -qO- http://cliproxy:8317/v1/models'
```

### 3. Site public

```bash
curl -I https://forge.example.com
```

## Những điểm cần chú ý

### 1. Không dùng `--reload` trên VPS

File compose này đã bỏ `--reload`.

### 2. Worker trên VPS có thể chạy trong Docker

Khác với máy local trước đó, trên VPS:

- `cliproxy` cũng nằm trong Docker
- app không cần gọi `localhost:8317`
- nên worker container là phù hợp

### 3. Publish WordPress

Nếu site WordPress ở ngoài VPS:

- cấu hình site trong UI
- dùng `consumer_key`, `consumer_secret`, `username`, `app_password`

Pipeline production không còn local test mode. Muốn publish được thì site phải được khai báo đầy đủ trong UI/DB, không dựa vào `.env`.

## Biến môi trường router đúng cho mô hình này

Trong stack này, app và worker sẽ được ép:

```env
ROUTER_BASE=http://cliproxy:8317/v1
```

Nghĩa là anh chỉ cần giữ:

```env
ROUTER_KEY=your-cliproxy-api-key
```

## Quy trình triển khai thực tế tôi khuyên dùng

1. Đưa `CLIProxyAPI` lên VPS trước
2. Xác nhận `/v1/models` chạy ổn
3. Lên app + worker
4. Login vào UI
5. Khai báo site đích trong `Website Manage`
6. Submit 1 job test nhỏ

## Nếu muốn đơn giản hơn

Tôi khuyên chia 2 giai đoạn:

1. deploy `cliproxy` riêng, test ổn
2. deploy full stack app sau

Làm vậy dễ debug hơn nhiều so với lên tất cả cùng một lúc.
