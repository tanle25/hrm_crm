from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal, Optional


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _load_dotenv() -> None:
    env_path = Path(".env")
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())
@dataclass(frozen=True)
class Settings:
    app_name: str
    app_env: str
    api_prefix: str
    auth_username: str
    auth_password: str
    auth_secret: str
    auth_cookie_name: str
    queue_mode: Literal["inline", "rq"]
    default_publish_status: Literal["draft", "publish"]
    max_queue_size: int
    max_retries: int
    processing_timeout_sec: int
    anthropic_api_key: Optional[str]
    anthropic_model_haiku: str
    anthropic_model_sonnet: str
    llm_primary_provider: str
    llm_disable_fallbacks: bool
    router_base: Optional[str]
    router_key: Optional[str]
    groq_api_key: Optional[str]
    groq_model: str
    groq_timeout_sec: int
    ollama_base: Optional[str]
    ollama_model: Optional[str]
    ollama_timeout_sec: int
    llm_timeout_sec: int
    llm_timeout_extract_planner_sec: int
    llm_timeout_writer_sec: int
    llm_timeout_humanizer_sec: int
    llm_timeout_qa_sec: int
    llm_router_retry_attempts: int
    llm_router_retry_backoff_ms: int
    llm_model_extract_planner: str
    llm_model_writer: str
    llm_model_humanizer: str
    llm_model_qa: str
    llm_model_fallback: str
    redis_url: str
    postgres_url: Optional[str]
    chroma_path: str
    woo_default_status: Literal["draft", "publish"]
    woo_default_price: str
    unsplash_access_key: Optional[str]
    webhook_url: Optional[str]
    google_search_api_key: Optional[str]
    google_search_engine_id: Optional[str]
    author_name: str
    metrics_enabled: bool
    metrics_port: int


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    _load_dotenv()
    queue_mode = os.getenv("QUEUE_MODE", "inline").strip().lower()
    default_publish_status = os.getenv("DEFAULT_PUBLISH_STATUS", "draft").strip().lower()
    woo_default_status = os.getenv("WOO_DEFAULT_STATUS", "draft").strip().lower()
    return Settings(
        app_name=os.getenv("APP_NAME", "Content Forge v2"),
        app_env=os.getenv("APP_ENV", "development"),
        api_prefix=os.getenv("API_PREFIX", "/api"),
        auth_username=os.getenv("AUTH_USERNAME", "admin"),
        auth_password=os.getenv("AUTH_PASSWORD", "admin123"),
        auth_secret=os.getenv("AUTH_SECRET", "content-forge-local-secret"),
        auth_cookie_name=os.getenv("AUTH_COOKIE_NAME", "content_forge_session"),
        queue_mode="rq" if queue_mode == "rq" else "inline",
        default_publish_status="publish" if default_publish_status == "publish" else "draft",
        max_queue_size=_env_int("MAX_QUEUE_SIZE", 100),
        max_retries=_env_int("MAX_RETRIES", 2),
        processing_timeout_sec=_env_int("PROCESSING_TIMEOUT_SEC", 600),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY"),
        anthropic_model_haiku=os.getenv("ANTHROPIC_MODEL_HAIKU", "claude-3-5-haiku-latest"),
        anthropic_model_sonnet=os.getenv("ANTHROPIC_MODEL_SONNET", "claude-3-7-sonnet-latest"),
        llm_primary_provider=os.getenv("LLM_PRIMARY_PROVIDER", "router").strip().lower(),
        llm_disable_fallbacks=_env_bool("LLM_DISABLE_FALLBACKS", False),
        router_base=os.getenv("ROUTER_BASE"),
        router_key=os.getenv("ROUTER_KEY"),
        groq_api_key=os.getenv("GROQ_API_KEY") or os.getenv("GROG_API"),
        groq_model=os.getenv("GROQ_MODEL", "llama-3.1-8b-instant"),
        groq_timeout_sec=_env_int("GROQ_TIMEOUT_SEC", 60),
        ollama_base=os.getenv("OLLAMA_BASE", "http://127.0.0.1:11434"),
        ollama_model=os.getenv("OLLAMA_MODEL"),
        ollama_timeout_sec=_env_int("OLLAMA_TIMEOUT_SEC", 180),
        llm_timeout_sec=_env_int("LLM_TIMEOUT_SEC", 120),
        llm_timeout_extract_planner_sec=_env_int("LLM_TIMEOUT_EXTRACT_PLANNER_SEC", _env_int("LLM_TIMEOUT_SEC", 120)),
        llm_timeout_writer_sec=_env_int("LLM_TIMEOUT_WRITER_SEC", _env_int("LLM_TIMEOUT_SEC", 120)),
        llm_timeout_humanizer_sec=_env_int("LLM_TIMEOUT_HUMANIZER_SEC", _env_int("LLM_TIMEOUT_SEC", 120)),
        llm_timeout_qa_sec=_env_int("LLM_TIMEOUT_QA_SEC", _env_int("LLM_TIMEOUT_SEC", 120)),
        llm_router_retry_attempts=_env_int("LLM_ROUTER_RETRY_ATTEMPTS", 2),
        llm_router_retry_backoff_ms=_env_int("LLM_ROUTER_RETRY_BACKOFF_MS", 2000),
        llm_model_extract_planner=os.getenv("LLM_MODEL_EXTRACT_PLANNER", "cx/gpt-5.2"),
        llm_model_writer=os.getenv("LLM_MODEL_WRITER", "cx/gpt-5.2"),
        llm_model_humanizer=os.getenv("LLM_MODEL_HUMANIZER", "cx/gpt-5.2"),
        llm_model_qa=os.getenv("LLM_MODEL_QA", "cx/gpt-5.2"),
        llm_model_fallback=os.getenv("LLM_MODEL_FALLBACK", "cx/gpt-5.2"),
        redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
        postgres_url=os.getenv("POSTGRES_URL"),
        chroma_path=os.getenv("CHROMA_PATH", "./data/chroma"),
        woo_default_status="publish" if woo_default_status == "publish" else "draft",
        woo_default_price=os.getenv("WOO_DEFAULT_PRICE", "99000"),
        unsplash_access_key=os.getenv("UNSPLASH_ACCESS_KEY"),
        webhook_url=os.getenv("WEBHOOK_URL"),
        google_search_api_key=os.getenv("GOOGLE_SEARCH_API_KEY"),
        google_search_engine_id=os.getenv("GOOGLE_SEARCH_ENGINE_ID"),
        author_name=os.getenv("AUTHOR_NAME", "Content Forge"),
        metrics_enabled=_env_bool("METRICS_ENABLED", True),
        metrics_port=_env_int("METRICS_PORT", 8001),
    )
