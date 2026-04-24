Đã đọc xong repo! Đây là 9Router — một OpenAI-compatible proxy chạy local ở localhost:20128. Dưới đây là curl cho tất cả các model:

🔑 Chuẩn bị
bash# Thay YOUR_API_KEY bằng key từ 9Router Dashboard
export ROUTER_KEY="YOUR_API_KEY"
export BASE="http://localhost:20128/v1"

📋 List tất cả models
bashcurl "$BASE/models" \
  -H "Authorization: Bearer $ROUTER_KEY"

💳 SUBSCRIPTION Providers
Claude Code (cc/)
bash# Opus 4.6
curl "$BASE/chat/completions" \
  -H "Authorization: Bearer $ROUTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "cc/claude-opus-4-6",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'

# Sonnet 4.5
curl "$BASE/chat/completions" \
  -H "Authorization: Bearer $ROUTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "cc/claude-sonnet-4-5-20250929",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'

# Haiku 4.5
curl "$BASE/chat/completions" \
  -H "Authorization: Bearer $ROUTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "cc/claude-haiku-4-5-20251001",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
OpenAI Codex (cx/)
bashcurl "$BASE/chat/completions" \
  -H "Authorization: Bearer $ROUTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "cx/gpt-5.2-codex",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'

curl "$BASE/chat/completions" \
  -H "Authorization: Bearer $ROUTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "cx/gpt-5.1-codex-max",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
Gemini CLI (gc/) — FREE 180K/tháng
bashcurl "$BASE/chat/completions" \
  -H "Authorization: Bearer $ROUTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gc/gemini-3-flash-preview",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'

curl "$BASE/chat/completions" \
  -H "Authorization: Bearer $ROUTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gc/gemini-2.5-pro",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
GitHub Copilot (gh/)
bashcurl "$BASE/chat/completions" \
  -H "Authorization: Bearer $ROUTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "gh/gpt-5", "messages": [{"role": "user", "content": "Hello!"}]}'

curl "$BASE/chat/completions" \
  -H "Authorization: Bearer $ROUTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "gh/claude-4.5-sonnet", "messages": [{"role": "user", "content": "Hello!"}]}'

curl "$BASE/chat/completions" \
  -H "Authorization: Bearer $ROUTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "gh/gemini-3-pro", "messages": [{"role": "user", "content": "Hello!"}]}'

💰 CHEAP Providers
GLM (glm/) — $0.6/1M tokens
bashcurl "$BASE/chat/completions" \
  -H "Authorization: Bearer $ROUTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "glm/glm-4.7", "messages": [{"role": "user", "content": "Hello!"}]}'
MiniMax (minimax/) — $0.2/1M tokens
bashcurl "$BASE/chat/completions" \
  -H "Authorization: Bearer $ROUTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "minimax/MiniMax-M2.1", "messages": [{"role": "user", "content": "Hello!"}]}'
Kimi (kimi/) — $9/tháng flat
bashcurl "$BASE/chat/completions" \
  -H "Authorization: Bearer $ROUTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "kimi/kimi-latest", "messages": [{"role": "user", "content": "Hello!"}]}'

🆓 FREE Providers (Unlimited)
iFlow (if/)
bash# Kimi K2 Thinking
curl "$BASE/chat/completions" \
  -H "Authorization: Bearer $ROUTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "if/kimi-k2-thinking", "messages": [{"role": "user", "content": "Hello!"}]}'

# Qwen3 Coder Plus
curl "$BASE/chat/completions" \
  -H "Authorization: Bearer $ROUTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "if/qwen3-coder-plus", "messages": [{"role": "user", "content": "Hello!"}]}'

# DeepSeek R1
curl "$BASE/chat/completions" \
  -H "Authorization: Bearer $ROUTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "if/deepseek-r1", "messages": [{"role": "user", "content": "Hello!"}]}'

# GLM-4.7 via iFlow
curl "$BASE/chat/completions" \
  -H "Authorization: Bearer $ROUTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "if/glm-4.7", "messages": [{"role": "user", "content": "Hello!"}]}'
Qwen (qw/)
bashcurl "$BASE/chat/completions" \
  -H "Authorization: Bearer $ROUTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "qw/qwen3-coder-plus", "messages": [{"role": "user", "content": "Hello!"}]}'

curl "$BASE/chat/completions" \
  -H "Authorization: Bearer $ROUTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "qw/qwen3-coder-flash", "messages": [{"role": "user", "content": "Hello!"}]}'
Kiro (kr/) — Claude FREE
bashcurl "$BASE/chat/completions" \
  -H "Authorization: Bearer $ROUTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "kr/claude-sonnet-4.5", "messages": [{"role": "user", "content": "Hello!"}]}'

curl "$BASE/chat/completions" \
  -H "Authorization: Bearer $ROUTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "kr/claude-haiku-4.5", "messages": [{"role": "user", "content": "Hello!"}]}'

🎨 Dùng Combo (Smart Fallback)
bash# Dùng tên combo thay vì model cụ thể
curl "$BASE/chat/completions" \
  -H "Authorization: Bearer $ROUTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "free-combo",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'

🌊 Streaming
bashcurl "$BASE/chat/completions" \
  -H "Authorization: Bearer $ROUTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "if/kimi-k2-thinking",
    "messages": [{"role": "user", "content": "Write a quicksort in Python"}],
    "stream": true
  }'