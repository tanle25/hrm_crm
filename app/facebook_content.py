from __future__ import annotations

import asyncio
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.agents import facebook_spinner
from app.config import get_settings
from app.facebook_pages import list_facebook_pages


settings = get_settings()
router = APIRouter(prefix=f"{settings.api_prefix}/facebook/content", tags=["facebook-content"])


class FacebookContentImageInput(BaseModel):
    name: str = ""
    type: str = ""
    size: int = 0
    url: str = ""


class FacebookContentVariantRequest(BaseModel):
    brief: str = Field(..., min_length=1, max_length=8000)
    page_ids: list[str] = Field(default_factory=list)
    groups: list[str] = Field(default_factory=list)
    images: list[FacebookContentImageInput] = Field(default_factory=list)
    tone: str = ""
    hashtag_count: int = Field(default=5, ge=0, le=12)
    core_caption_count: int | None = Field(default=None, ge=1, le=40)
    strategy: Literal["auto", "balanced"] = "auto"


class FacebookCoreCaption(BaseModel):
    angle: str = ""
    persona: str = ""
    caption: str = ""
    hashtags: list[str] = Field(default_factory=list)
    cta: str = ""


class FacebookPostVariant(BaseModel):
    page_id: str
    page_name: str = ""
    group: str = ""
    angle: str = ""
    caption: str = ""
    hashtags: list[str] = Field(default_factory=list)
    core_index: int = 0


class FacebookContentVariantResponse(BaseModel):
    strategy: str
    page_count: int
    core_caption_count: int
    recommended_core_caption_count: int
    core_captions: list[FacebookCoreCaption] = Field(default_factory=list)
    posts: list[FacebookPostVariant] = Field(default_factory=list)
    quality: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


def _select_pages(page_ids: list[str], groups: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    all_pages = list_facebook_pages()
    selected_ids = {str(item).strip() for item in page_ids if str(item).strip()}
    selected_groups = {str(item).strip() for item in groups if str(item).strip()}
    pages: list[dict[str, Any]] = []
    warnings: list[str] = []

    for page in all_pages:
        page_id = str(page.get("page_id") or "")
        group = str(page.get("group") or "").strip()
        if selected_ids and page_id in selected_ids:
            pages.append(page)
            continue
        if selected_groups and group in selected_groups:
            pages.append(page)

    if not selected_ids and not selected_groups:
        warnings.append("No page_ids or groups were provided; using all connected pages.")
        pages = all_pages

    found_ids = {str(page.get("page_id") or "") for page in pages}
    missing_ids = sorted(selected_ids - found_ids)
    if missing_ids:
        warnings.append(f"Some requested pages were not found: {', '.join(missing_ids[:5])}")

    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for page in pages:
        page_id = str(page.get("page_id") or "")
        if not page_id or page_id in seen:
            continue
        seen.add(page_id)
        deduped.append(page)
    return deduped, warnings


@router.post("/preview-variants", response_model=FacebookContentVariantResponse)
async def preview_facebook_content_variants(request: FacebookContentVariantRequest) -> FacebookContentVariantResponse:
    pages, warnings = await asyncio.to_thread(_select_pages, request.page_ids, request.groups)
    if not pages:
        raise HTTPException(status_code=400, detail="No Facebook pages matched the selected page_ids/groups.")
    result = await asyncio.to_thread(
        facebook_spinner.run,
        brief=request.brief,
        pages=pages,
        groups=request.groups,
        tone=request.tone,
        image_count=len(request.images),
        hashtag_count=request.hashtag_count,
        core_count=request.core_caption_count,
    )
    result["warnings"] = [*warnings, *(result.get("warnings") or [])]
    return FacebookContentVariantResponse(**result)
