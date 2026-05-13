from __future__ import annotations

import asyncio
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
PUBLIC_LLM_SEMAPHORE = asyncio.Semaphore(settings.public_llm_max_concurrency)
PUBLIC_VISION_SEMAPHORE = asyncio.Semaphore(settings.public_vision_max_concurrency)


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str = "default"
    messages: list[ChatMessage] = Field(default_factory=list)
    temperature: float | None = None
    max_tokens: int | None = 1024
    stream: bool | None = False


class VisionDescribeRequest(BaseModel):
    image_url: str | None = None
    image_base64: str | None = None
    mime_type: str | None = "image/jpeg"
    prompt: str | None = None
    model: str | None = None
    max_tokens: int | None = 800
    temperature: float | None = 0.2


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
        {"id": "vision", "object": "model", "owned_by": "content-forge", "model": settings.vision_model},
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


def _vision_max_tokens(value: int | None) -> int:
    return max(1, min(int(value or 800), 2048))


def _vision_prompt(prompt: str | None) -> str:
    text = (prompt or "").strip()
    if text:
        return text[:4000]
    return (
        "Mô tả ảnh để tìm sản phẩm trong catalog bán hàng. "
        "Không giải thích suy luận. Không đoán công dụng nếu không chắc. "
        "Trả JSON thuần với các field: product_name_visible, colors, packaging, visible_text, "
        "logos, shape, likely_category, search_keywords, summary_vi."
    )


def _vision_content(request: VisionDescribeRequest) -> list[dict[str, Any]]:
    return [
        {"type": "text", "text": _vision_prompt(request.prompt)},
        _vision_image_part(request),
    ]


def _vision_image_part(request: VisionDescribeRequest) -> dict[str, Any]:
    image_url = str(request.image_url or "").strip()
    if image_url:
        if not (image_url.startswith("http://") or image_url.startswith("https://") or image_url.startswith("data:image/")):
            raise HTTPException(status_code=400, detail="image_url must be http(s) or data:image URL.")
        if len(image_url) > 20000:
            raise HTTPException(status_code=400, detail="image_url is too large.")
        return {"type": "image_url", "image_url": {"url": image_url}}

    image_base64 = str(request.image_base64 or "").strip()
    if image_base64:
        if len(image_base64) > 12_000_000:
            raise HTTPException(status_code=400, detail="image_base64 is too large.")
        mime_type = str(request.mime_type or "image/jpeg").strip().lower()
        if mime_type not in {"image/jpeg", "image/png", "image/webp", "image/gif"}:
            raise HTTPException(status_code=400, detail="Unsupported image mime_type.")
        return {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{image_base64}"}}

    raise HTTPException(status_code=400, detail="image_url or image_base64 is required.")


async def _call_router(model: str, messages: list[dict[str, str]], max_tokens: int, temperature: float | None) -> dict[str, Any]:
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
        timeout = httpx.Timeout(float(settings.llm_timeout_writer_sec), connect=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{settings.router_base.rstrip('/')}/chat/completions",
                headers=headers,
                json=payload,
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


async def _call_openrouter_vision(request: VisionDescribeRequest) -> dict[str, Any]:
    if not settings.openrouter_api_key:
        raise HTTPException(status_code=503, detail="OPENROUTER_API_KEY is not configured.")
    model = str(request.model or settings.vision_model).strip() or settings.vision_model
    payload = {
        "model": model,
        "reasoning": {"enabled": False},
        "messages": [
            {
                "role": "user",
                "content": _vision_content(request),
            }
        ],
        "max_tokens": _vision_max_tokens(request.max_tokens),
        "temperature": max(0.0, min(float(request.temperature if request.temperature is not None else 0.2), 2.0)),
    }
    headers = {
        "Authorization": f"Bearer {settings.openrouter_api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://tool.hamrongmedia.net",
        "X-Title": settings.app_name,
    }
    try:
        timeout = httpx.Timeout(float(settings.vision_timeout_sec), connect=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{settings.openrouter_base.rstrip('/')}/chat/completions",
                headers=headers,
                json=payload,
            )
        response.raise_for_status()
    except httpx.HTTPStatusError as error:
        detail = error.response.text[:800] if error.response is not None else str(error)
        raise HTTPException(status_code=502, detail=f"Vision provider failed: {detail}") from error
    except httpx.HTTPError as error:
        raise HTTPException(status_code=502, detail=f"Vision provider failed: {error}") from error

    data = response.json()
    usage = data.get("usage") or {}
    record_tokens("public_vision", int(usage.get("total_tokens") or 0))
    return data


async def _call_router_vision(request: VisionDescribeRequest) -> dict[str, Any]:
    if not settings.router_base:
        raise HTTPException(status_code=503, detail="LLM router is not configured.")
    model = str(request.model or settings.vision_model).strip() or settings.vision_model
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": _vision_content(request)}],
        "max_tokens": _vision_max_tokens(request.max_tokens),
        "temperature": max(0.0, min(float(request.temperature if request.temperature is not None else 0.2), 2.0)),
    }
    headers = {"Content-Type": "application/json"}
    if settings.router_key:
        headers["Authorization"] = f"Bearer {settings.router_key}"
    try:
        timeout = httpx.Timeout(float(settings.vision_timeout_sec), connect=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{settings.router_base.rstrip('/')}/chat/completions",
                headers=headers,
                json=payload,
            )
        response.raise_for_status()
    except httpx.HTTPStatusError as error:
        detail = error.response.text[:800] if error.response is not None else str(error)
        raise HTTPException(status_code=502, detail=f"Vision router failed: {detail}") from error
    except httpx.HTTPError as error:
        raise HTTPException(status_code=502, detail=f"Vision router failed: {error}") from error

    data = response.json()
    usage = data.get("usage") or {}
    record_tokens("public_vision", int(usage.get("total_tokens") or 0))
    return data


async def _call_vision(request: VisionDescribeRequest) -> dict[str, Any]:
    if settings.vision_provider == "openrouter":
        return await _call_openrouter_vision(request)
    if settings.vision_provider in {"router", "cliproxy", "codex", "gpt"}:
        return await _call_router_vision(request)
    raise HTTPException(status_code=503, detail=f"Unsupported VISION_PROVIDER: {settings.vision_provider}")


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
    try:
        await asyncio.wait_for(PUBLIC_LLM_SEMAPHORE.acquire(), timeout=0.1)
    except asyncio.TimeoutError as error:
        raise HTTPException(status_code=429, detail="Public LLM is busy. Please retry shortly.") from error
    try:
        data = await _call_router(resolved_model, messages, max_tokens, request.temperature)
    finally:
        PUBLIC_LLM_SEMAPHORE.release()
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


@router.post("/vision/describe-image")
async def public_vision_describe_image(request: VisionDescribeRequest) -> dict[str, Any]:
    try:
        await asyncio.wait_for(PUBLIC_VISION_SEMAPHORE.acquire(), timeout=0.1)
    except asyncio.TimeoutError as error:
        raise HTTPException(status_code=429, detail="Public vision is busy. Please retry shortly.") from error
    try:
        data = await _call_vision(request)
    finally:
        PUBLIC_VISION_SEMAPHORE.release()

    choices = data.get("choices") or []
    if not choices:
        raise HTTPException(status_code=502, detail="Vision provider returned no choices.")
    message = choices[0].get("message") or {}
    content = message.get("content") or message.get("reasoning") or ""
    return {
        "id": data.get("id") or f"vision_{uuid4().hex}",
        "object": "vision.description",
        "created": int(data.get("created") or time.time()),
        "model": data.get("model") or request.model or settings.vision_model,
        "description": content,
        "choices": choices,
        "usage": data.get("usage") or {},
        "provider": data.get("provider") or settings.vision_provider,
    }
