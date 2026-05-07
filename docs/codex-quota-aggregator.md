# Codex Quota Aggregator

Sidecar local-only để đọc quota 5h/7d của các Codex ChatGPT accounts đang dùng qua CLIProxyAPI.

## Chạy Local

```bash
cd /Users/tanle/Documents/agent
venv/bin/python scripts/quota_aggregator.py
```

Mặc định:

- Host: `127.0.0.1`
- Port: `8320`
- Auth dir: `~/.cli-proxy-api`
- Cache TTL: `60` giây

Có thể override:

```bash
CLIPROXY_AUTH_DIR="$HOME/.cli-proxy-api" \
CACHE_TTL=300 \
QUOTA_HOST=127.0.0.1 \
QUOTA_PORT=8320 \
venv/bin/python scripts/quota_aggregator.py
```

## Endpoints

```bash
curl -sS http://127.0.0.1:8320/health | jq
```

```bash
curl -sS http://127.0.0.1:8320/accounts | jq
```

```bash
curl -sS http://127.0.0.1:8320/quota | jq '.summary'
```

```bash
curl -sS 'http://127.0.0.1:8320/quota?force=1' | jq '.summary'
```

```bash
curl -sS http://127.0.0.1:8320/quota/best | jq
```

```bash
curl -sS http://127.0.0.1:8320/quota/tanlt04690 | jq
```

## Status

- `ok`: account dùng được.
- `subscription_expired`: `wham/usage` trả `plan_type=free`, không tính vào healthy.
- `disabled`: auth file bị disable.
- `token_expired`: access token hết hạn theo metadata auth file.
- `unauthorized`: token bị từ chối bởi `chatgpt.com`.
- `http_XXX`: lỗi HTTP khác từ `wham/usage`.
- `exception`: lỗi network/runtime.

## Ghi Chú Bảo Mật

- Script chỉ bind `127.0.0.1`, không expose ra internet.
- Không log `access_token`.
- Không dùng `/v0/management/api-call` vì CLIProxyAPI `6.9.25` không substitute `$TOKEN$` ổn định cho endpoint `wham/usage`.
