from __future__ import annotations

import time
from typing import Any
from uuid import uuid4

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.config import get_settings
from app.metrics import record_tokens


settings = get_settings()
router = APIRouter(prefix=f"{settings.api_prefix}/public/v1", tags=["public-chat"])


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str = "default"
    messages: list[ChatMessage] = Field(default_factory=list)
    temperature: float | None = None
    max_tokens: int | None = 1024
    stream: bool | None = False


def _model_aliases() -> dict[str, str]:
    return {
        "default": settings.llm_model_writer,
        "fast": settings.llm_model_extract_planner,
        "quality": settings.llm_model_humanizer or settings.llm_model_writer,
    }


def _public_models() -> list[dict[str, Any]]:
    return [
        {"id": "default", "object": "model", "owned_by": "content-forge"},
        {"id": "fast", "object": "model", "owned_by": "content-forge"},
        {"id": "quality", "object": "model", "owned_by": "content-forge"},
    ]


def _resolve_model(model: str) -> str:
    aliases = _model_aliases()
    normalized = str(model or "default").strip()
    if normalized in aliases:
        return aliases[normalized]
    configured = set(aliases.values())
    if normalized in configured:
        return normalized
    raise HTTPException(status_code=400, detail=f"Unsupported model '{model}'. Use one of: default, fast, quality.")


def _validated_messages(messages: list[ChatMessage]) -> list[dict[str, str]]:
    if not messages:
        raise HTTPException(status_code=400, detail="messages is required.")
    allowed_roles = {"system", "user", "assistant"}
    normalized: list[dict[str, str]] = []
    total_chars = 0
    for message in messages:
        role = str(message.role or "").strip().lower()
        content = str(message.content or "")
        if role not in allowed_roles:
            raise HTTPException(status_code=400, detail=f"Unsupported role '{message.role}'.")
        if not content.strip():
            continue
        total_chars += len(content)
        normalized.append({"role": role, "content": content})
    if not normalized:
        raise HTTPException(status_code=400, detail="messages must contain non-empty content.")
    if total_chars > 60000:
        raise HTTPException(status_code=400, detail="messages content is too large.")
    return normalized


def _max_tokens(value: int | None) -> int:
    return max(1, min(int(value or 1024), 4096))


def _call_router(model: str, messages: list[dict[str, str]], max_tokens: int, temperature: float | None) -> dict[str, Any]:
    if not settings.router_base:
        raise HTTPException(status_code=503, detail="LLM router is not configured.")
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
    }
    if temperature is not None:
        payload["temperature"] = max(0.0, min(float(temperature), 2.0))
    headers = {"Content-Type": "application/json"}
    if settings.router_key:
        headers["Authorization"] = f"Bearer {settings.router_key}"
    try:
        response = httpx.post(
            f"{settings.router_base.rstrip('/')}/chat/completions",
            headers=headers,
            json=payload,
            timeout=settings.llm_timeout_writer_sec,
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as error:
        detail = error.response.text[:500] if error.response is not None else str(error)
        raise HTTPException(status_code=502, detail=f"LLM router failed: {detail}") from error
    except httpx.HTTPError as error:
        raise HTTPException(status_code=502, detail=f"LLM router failed: {error}") from error
    data = response.json()
    usage = data.get("usage") or {}
    record_tokens("public_chat", int(usage.get("total_tokens") or 0))
    return data


@router.get("/models")
async def public_chat_models() -> dict[str, Any]:
    return {"object": "list", "data": _public_models()}


@router.post("/chat/completions")
async def public_chat_completions(request: ChatCompletionRequest) -> dict[str, Any]:
    if request.stream:
        raise HTTPException(status_code=400, detail="stream=true is not supported yet.")
    public_model = str(request.model or "default").strip() or "default"
    resolved_model = _resolve_model(public_model)
    messages = _validated_messages(request.messages)
    max_tokens = _max_tokens(request.max_tokens)
    data = _call_router(resolved_model, messages, max_tokens, request.temperature)
    choices = data.get("choices") or []
    if not choices:
        raise HTTPException(status_code=502, detail="LLM router returned no choices.")
    usage = data.get("usage") or {}
    return {
        "id": data.get("id") or f"chatcmpl_{uuid4().hex}",
        "object": "chat.completion",
        "created": int(data.get("created") or time.time()),
        "model": public_model,
        "choices": choices,
        "usage": usage,
    }
