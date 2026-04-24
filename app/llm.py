from __future__ import annotations

import json
import re
import time
from functools import lru_cache
from typing import Any, Dict

import httpx

from app.config import get_settings
from app.metrics import record_tokens

try:
    import anthropic
except ImportError:  # pragma: no cover
    anthropic = None


AGENT_MODEL_MAP = {
    "classifier": "extract_planner",
    "extractor": "extract_planner",
    "planner": "extract_planner",
    "knowledge": "extract_planner",
    "writer": "writer",
    "humanizer": "humanizer",
    "qa": "qa",
    "seo_adjuster": "qa",
}


@lru_cache(maxsize=1)
def get_anthropic_client() -> Any:
    settings = get_settings()
    if not anthropic or not settings.anthropic_api_key:
        return None
    return anthropic.Anthropic(api_key=settings.anthropic_api_key)


def _select_model(agent_name: str) -> str:
    settings = get_settings()
    family = AGENT_MODEL_MAP.get(agent_name, "writer")
    if family == "extract_planner":
        return settings.llm_model_extract_planner
    if family == "writer":
        return settings.llm_model_writer
    if family == "humanizer":
        return settings.llm_model_humanizer
    if family == "qa":
        return settings.llm_model_qa
    return settings.llm_model_fallback


def _candidate_models(agent_name: str) -> list[str]:
    settings = get_settings()
    primary = _select_model(agent_name)
    models = [primary]
    if settings.llm_disable_fallbacks:
        return models
    fallback = settings.llm_model_fallback
    if fallback and fallback not in models:
        models.append(fallback)
    return models


def _is_retryable_router_error(exc: Exception) -> bool:
    if isinstance(exc, httpx.TimeoutException):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in {408, 429, 502, 503, 504}
    return False


def _timeout_for(agent_name: str) -> int:
    settings = get_settings()
    family = AGENT_MODEL_MAP.get(agent_name, "writer")
    if family == "extract_planner":
        return settings.llm_timeout_extract_planner_sec
    if family == "writer":
        return settings.llm_timeout_writer_sec
    if family == "humanizer":
        return settings.llm_timeout_humanizer_sec
    if family == "qa":
        return settings.llm_timeout_qa_sec
    return settings.llm_timeout_sec


def _fallback_text(agent_name: str, prompt: str) -> str:
    compact = re.sub(r"\s+", " ", prompt).strip()
    prefix = compact[:400]
    return f"[fallback:{agent_name}] {prefix}"


def _extract_json_candidate(raw: str) -> str | None:
    fenced = re.search(r"```(?:json)?\s*(\{.*\}|\[.*\])\s*```", raw, re.DOTALL | re.IGNORECASE)
    if fenced:
        return fenced.group(1)

    decoder = json.JSONDecoder()
    for start, char in enumerate(raw):
        if char not in "[{":
            continue
        try:
            obj, end = decoder.raw_decode(raw[start:])
            return json.dumps(obj, ensure_ascii=False)
        except json.JSONDecodeError:
            continue
    return None


def _call_router_with_model(model: str, agent_name: str, system: str, user: str, max_tokens: int) -> str:
    settings = get_settings()
    if not settings.router_base:
        raise RuntimeError("Router is not configured")

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
    }
    headers = {
        "Content-Type": "application/json",
    }
    if settings.router_key:
        headers["Authorization"] = f"Bearer {settings.router_key}"
    response = httpx.post(
        f"{settings.router_base.rstrip('/')}/chat/completions",
        headers=headers,
        json=payload,
        timeout=_timeout_for(agent_name),
    )
    response.raise_for_status()
    data = response.json()
    usage = data.get("usage", {})
    record_tokens(agent_name, int(usage.get("total_tokens", 0)))
    choices = data.get("choices", [])
    if not choices:
        raise RuntimeError("Router returned no choices")
    return choices[0]["message"]["content"]


def _call_router(agent_name: str, system: str, user: str, max_tokens: int) -> str:
    settings = get_settings()
    last_error: Exception | None = None
    for model in _candidate_models(agent_name):
        attempts = max(1, settings.llm_router_retry_attempts)
        for attempt in range(1, attempts + 1):
            try:
                return _call_router_with_model(model, agent_name, system, user, max_tokens)
            except Exception as exc:
                last_error = exc
                if not _is_retryable_router_error(exc) or attempt >= attempts:
                    break
                delay_sec = (settings.llm_router_retry_backoff_ms / 1000.0) * attempt
                time.sleep(delay_sec)
        continue
    raise RuntimeError(f"Router call failed for {agent_name}: {last_error}")


def _call_ollama(agent_name: str, system: str, user: str, max_tokens: int) -> str:
    settings = get_settings()
    if not settings.ollama_base or not settings.ollama_model:
        raise RuntimeError("Ollama is not configured")

    payload = {
        "model": settings.ollama_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "options": {
            "num_predict": max_tokens,
        },
    }
    response = httpx.post(
        f"{settings.ollama_base.rstrip('/')}/api/chat",
        json=payload,
        timeout=settings.ollama_timeout_sec,
    )
    response.raise_for_status()
    data = response.json()
    prompt_tokens = int(data.get("prompt_eval_count", 0))
    completion_tokens = int(data.get("eval_count", 0))
    record_tokens(agent_name, prompt_tokens + completion_tokens)
    message = data.get("message", {})
    content = message.get("content")
    if not content:
        raise RuntimeError("Ollama returned no content")
    return content


def _call_groq(agent_name: str, system: str, user: str, max_tokens: int) -> str:
    settings = get_settings()
    if not settings.groq_api_key:
        raise RuntimeError("Groq is not configured")

    payload = {
        "model": settings.groq_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.2,
    }
    response = httpx.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {settings.groq_api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=settings.groq_timeout_sec,
    )
    response.raise_for_status()
    data = response.json()
    usage = data.get("usage", {})
    total_tokens = int(usage.get("total_tokens", usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)))
    record_tokens(agent_name, total_tokens)
    choices = data.get("choices", [])
    if not choices:
        raise RuntimeError("Groq returned no choices")
    return choices[0]["message"]["content"]


def call_llm(agent_name: str, system: str, user: str, max_tokens: int = 2048) -> str:
    settings = get_settings()
    providers = [settings.llm_primary_provider, "router", "groq", "ollama", "anthropic"]
    seen: set[str] = set()
    ordered = [provider for provider in providers if not (provider in seen or seen.add(provider))]
    if settings.llm_disable_fallbacks:
        ordered = [settings.llm_primary_provider]

    last_error: Exception | None = None
    for provider in ordered:
        try:
            if provider == "router" and settings.router_base:
                return _call_router(agent_name, system, user, max_tokens)
            if provider == "groq" and settings.groq_api_key:
                return _call_groq(agent_name, system, user, max_tokens)
            if provider == "ollama" and settings.ollama_base and settings.ollama_model:
                return _call_ollama(agent_name, system, user, max_tokens)
            if provider == "anthropic":
                client = get_anthropic_client()
                if client is not None:
                    model = settings.anthropic_model_haiku if AGENT_MODEL_MAP.get(agent_name) == "extract_planner" else settings.anthropic_model_sonnet
                    response = client.messages.create(
                        model=model,
                        max_tokens=max_tokens,
                        system=system,
                        messages=[{"role": "user", "content": user}],
                    )
                    usage = getattr(response, "usage", None)
                    if usage is not None:
                        record_tokens(agent_name, getattr(usage, "input_tokens", 0) + getattr(usage, "output_tokens", 0))
                    return response.content[0].text
        except Exception as exc:
            last_error = exc
            continue

    if settings.llm_disable_fallbacks:
        raise RuntimeError(f"LLM call failed for {agent_name} via provider {settings.llm_primary_provider}: {last_error}")

    estimated_tokens = max(1, (len(system) + len(user)) // 4)
    record_tokens(agent_name, estimated_tokens)
    return _fallback_text(agent_name, user)


def call_json(agent_name: str, system: str, user: str, fallback: Dict[str, Any], max_tokens: int = 2048) -> Dict[str, Any]:
    settings = get_settings()
    raw = call_llm(agent_name, system, user, max_tokens=max_tokens)
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
        if settings.llm_disable_fallbacks:
            raise RuntimeError(f"{agent_name} returned JSON type {type(parsed).__name__}, expected object")
        return fallback
    except json.JSONDecodeError:
        candidate = _extract_json_candidate(raw)
        if candidate:
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict):
                    return parsed
                if settings.llm_disable_fallbacks:
                    raise RuntimeError(f"{agent_name} returned JSON type {type(parsed).__name__}, expected object")
                return fallback
            except json.JSONDecodeError:
                pass
        if settings.llm_disable_fallbacks:
            raise RuntimeError(f"{agent_name} returned invalid JSON")
        return fallback
