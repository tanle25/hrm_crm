from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import os
import re
import secrets
from contextlib import suppress
from pathlib import Path

import httpx
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Query, Request, Response, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.api_tokens import create_api_token, delete_api_token, list_api_tokens, verify_api_token
from app.auth import authenticate_credentials, create_session_token, verify_session_token
from app.chatbot_products import (
    catalog_rag_status as chatbot_catalog_rag_status,
    delete_category as delete_chatbot_product_category,
    delete_catalog_product_vectors as delete_chatbot_catalog_product_vectors,
    delete_product as delete_chatbot_product,
    get_product as get_chatbot_product,
    list_labels as list_chatbot_product_labels,
    list_categories as list_chatbot_product_categories,
    list_products as list_chatbot_products,
    reindex_catalog as reindex_chatbot_catalog,
    search_catalog as search_chatbot_catalog,
    toggle_product as toggle_chatbot_product,
    toggle_variant as toggle_chatbot_product_variant,
    upsert_category as upsert_chatbot_product_category,
    upsert_product as upsert_chatbot_product,
)
from app.config import get_settings
from app.dlq import publish_anyway
from app.facebook_content import router as facebook_content_router
from app.facebook_pages import (
    connect_facebook_pages,
    create_facebook_page_group,
    debug_facebook_messages,
    enqueue_facebook_conversation_sync,
    facebook_aggregate_stats,
    facebook_comments,
    facebook_conversation_detail,
    facebook_conversations,
    get_facebook_page_asset,
    facebook_posts,
    get_facebook_sync_job,
    latest_facebook_sync_jobs,
    list_facebook_page_groups,
    list_facebook_pages,
    mark_facebook_conversation_read,
    process_facebook_webhook,
    resubscribe_facebook_page_webhooks,
    send_facebook_message,
    sync_facebook_comments,
    sync_facebook_aggregate_stats,
    sync_facebook_posts,
    update_facebook_page_group,
    verify_facebook_webhook_signature,
)
from app.facebook_reels import router as facebook_reels_router
from app.facebook_reel_flowkit import router as facebook_reel_flowkit_router
from app.facebook_slash_commands import delete_facebook_slash_command, list_facebook_slash_commands, upsert_facebook_slash_command
from app.flowkit import router as flowkit_router
from app.graph import retry_from_dlq, run_pipeline_async
from app.job_store import delete_dlq_entry, get_dlq_entry, get_job, get_jobs_version, list_dlq, list_jobs, stats_snapshot, wait_for_jobs_version
from app.logging import get_logger
from app.metrics import dlq_size, jobs_submitted, start_metrics_server_once
from app.postgres import init_schema as init_postgres_schema, migrate_local_state as migrate_local_postgres_state
from app.public_chat import router as public_chat_router
from app.public_chat import VisionDescribeRequest, _call_vision
from app.queue import create_job_id, enqueue_job, enqueue_saved_state, init_job_state, queue_is_full, update_job
from app.rag_categories import create_category, list_categories
from app.rag import delete_source_documents, get_source_documents, get_taxonomy_summary, list_rag_sources, search_knowledge
from app.shopee import delete_shopee_product, get_shopee_product, import_legacy_sample, list_shopee_products, upsert_shopee_product
from app.rag_jobs import create_rag_job, get_rag_job, list_rag_jobs, public_rag_job, run_rag_job
from app.schemas import (
    JobListItem,
    JobListResponse,
    JobProgressResponse,
    ApiTokenCreateRequest,
    ApiTokenCreateResponse,
    ApiTokenListItem,
    ApiTokenListResponse,
    AuthMeResponse,
    FacebookConnectRequest,
    FacebookConnectResponse,
    FacebookCommentListResponse,
    FacebookConversationListResponse,
    FacebookPageGroupCreateRequest,
    FacebookPageGroupListResponse,
    FacebookPageGroupUpdateRequest,
    FacebookPageListResponse,
    FacebookMessageSendRequest,
    FacebookMessageSendResponse,
    FacebookPostListResponse,
    FacebookStatsResponse,
    LoginRequest,
    LoginResponse,
    RAGCategoryCreate,
    RAGCategoryListResponse,
    PipelineState,
    RAGIngestRequest,
    RAGSearchResponse,
    RAGSourceListResponse,
    RAGSourceResponse,
    RAGTaxonomyResponse,
    RAGTextIngestRequest,
    ShopeeEnqueueRequest,
    ShopeeProductDetailResponse,
    ShopeeProductListItem,
    ShopeeProductListResponse,
    ShopeeUpsertRequest,
    SiteConfigCreate,
    SiteConfigResponse,
    SiteConfigUpdate,
    SiteListResponse,
    SiteTestResponse,
    StatsResponse,
    SubmitBatchRequest,
    SubmitBatchResponse,
    SubmitRequest,
    SubmitResponse,
    WebsitePostSubmitRequest,
)
from app.site_store import create_site, delete_site, get_site, list_sites, test_site_connection, update_site

try:
    from redis import Redis
except ImportError:  # pragma: no cover
    Redis = None

settings = get_settings()
app = FastAPI(title=settings.app_name, version="2.0.0")
log = get_logger("content_forge.api")
UI_DIR = Path("ui")
FACEBOOK_MESSAGE_MEDIA_DIR = Path("data/facebook_message_media")
CHATBOT_PRODUCT_MEDIA_DIR = Path("data/chatbot_product_media")
app.include_router(facebook_content_router)
app.include_router(facebook_reels_router)
app.include_router(facebook_reel_flowkit_router)
app.include_router(flowkit_router)
app.include_router(public_chat_router)

if UI_DIR.exists():
    app.mount("/ui", StaticFiles(directory=UI_DIR), name="ui")
FACEBOOK_MESSAGE_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/public/facebook-message-media", StaticFiles(directory=FACEBOOK_MESSAGE_MEDIA_DIR), name="facebook-message-media")
CHATBOT_PRODUCT_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/public/chatbot-product-media", StaticFiles(directory=CHATBOT_PRODUCT_MEDIA_DIR), name="chatbot-product-media")


def _vision_product_prompt(title: str = "") -> str:
    return (
        "Bạn là hệ thống mô tả ảnh để tìm sản phẩm tương tự trong catalog. "
        "TUYỆT ĐỐI không bịa tên sản phẩm, thương hiệu, model, chất liệu, công dụng, xuất xứ nếu không nhìn thấy rõ chữ hoặc bằng chứng trực tiếp trong ảnh. "
        "Chỉ mô tả đặc điểm thị giác quan sát được. Nếu không chắc, ghi null hoặc 'không xác định'. "
        f"Ngữ cảnh tên sản phẩm catalog nếu có: {title}. "
        "Trả JSON thuần với schema: {\"visible_object\":\"\", \"object_type_guess\":\"\", \"confidence\":0-1, \"colors\":[], \"shape\":\"\", \"materials_visible\":\"\", \"visible_text\":[], \"logos\":[], \"packaging\":\"\", \"distinctive_features\":[], \"background_context\":\"\", \"search_keywords_vi\":[], \"do_not_assume\":[]}."
    )


def _vision_description_text(payload: dict) -> str:
    choices = payload.get("choices") or []
    if choices:
        message = choices[0].get("message") or {}
        return str(message.get("content") or message.get("reasoning") or "").strip()
    return ""


async def _describe_catalog_image(image_url: str, title: str = "") -> str:
    if not image_url:
        return ""
    local_image = _chatbot_media_local_file(image_url)
    image_base64 = None
    mime_type = "image/jpeg"
    if local_image and local_image.exists():
        image_base64 = base64.b64encode(local_image.read_bytes()).decode("ascii")
        mime_type = mimetypes.guess_type(local_image.name)[0] or "image/jpeg"
        image_url = ""
    payload = await _call_vision(
        VisionDescribeRequest(
            image_url=image_url or None,
            image_base64=image_base64,
            mime_type=mime_type,
            prompt=_vision_product_prompt(title),
            max_tokens=800,
            temperature=0,
        )
    )
    return _vision_description_text(payload)


def _chatbot_media_local_file(image_url: str) -> Path | None:
    marker = "/public/chatbot-product-media/"
    if marker not in str(image_url or ""):
        return None
    filename = str(image_url).split(marker, 1)[1].split("?", 1)[0].split("#", 1)[0]
    if "/" in filename or "\\" in filename or not filename:
        return None
    return CHATBOT_PRODUCT_MEDIA_DIR / filename


def _has_summary_for_url(product: dict, image_url: str) -> bool:
    for item in product.get("image_summaries") or []:
        if isinstance(item, dict) and str(item.get("image_url") or item.get("url") or "") == image_url and str(item.get("summary") or "").strip():
            return True
    return False


async def _enrich_product_vision_and_reindex(product_id: str) -> None:
    product = await asyncio.to_thread(get_chatbot_product, product_id)
    if not product:
        return
    changed = False
    vision_errors: list[str] = []
    title = str(product.get("title") or "")
    image_summaries = list(product.get("image_summaries") or [])
    for image_url in list(product.get("images") or [])[:6]:
        image_url = str(image_url or "").strip()
        if not image_url or _has_summary_for_url(product, image_url):
            continue
        try:
            summary = await _describe_catalog_image(image_url, title)
            if summary:
                image_summaries.append({"image_url": image_url, "summary": summary})
                changed = True
        except Exception as error:
            vision_errors.append(f"product image {image_url}: {error}")
    product["image_summaries"] = image_summaries

    for variant in product.get("variants") or []:
        image_url = str(variant.get("image_url") or "").strip()
        if not image_url or str(variant.get("image_summary") or "").strip():
            continue
        try:
            summary = await _describe_catalog_image(image_url, f"{title} {variant.get('name') or ''}".strip())
            if summary:
                variant["image_summary"] = summary
                changed = True
        except Exception as error:
            vision_errors.append(f"variant {variant.get('variant_id') or variant.get('name')}: {error}")

    if vision_errors:
        data = product.get("data") if isinstance(product.get("data"), dict) else {}
        data["vision_errors"] = vision_errors[-10:]
        product["data"] = data
        changed = True
    if changed:
        product["rag_dirty"] = True
        product = await asyncio.to_thread(upsert_chatbot_product, product, product_id)
    await asyncio.to_thread(reindex_chatbot_catalog, product["product_id"], False)


async def _enrich_product_vision_sync(product_id: str) -> dict:
    await _enrich_product_vision_and_reindex(product_id)
    product = await asyncio.to_thread(get_chatbot_product, product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    return {
        "product_id": product_id,
        "image_summaries_count": len(product.get("image_summaries") or []),
        "variant_summaries": sum(1 for item in product.get("variants") or [] if item.get("image_summary")),
        "vision_errors": (product.get("data") or {}).get("vision_errors") if isinstance(product.get("data"), dict) else [],
    }


async def _enrich_catalog_vision_and_reindex(product_id: str | None = None, limit: int = 50) -> None:
    if product_id:
        await _enrich_product_vision_and_reindex(product_id)
        return
    payload = await asyncio.to_thread(list_chatbot_products, None, "", "", max(1, min(limit, 200)))
    for product in payload.get("items") or []:
        await _enrich_product_vision_and_reindex(str(product.get("product_id") or ""))


AUTH_EXEMPT_PATHS = {
    "/health",
    "/login",
    "/docs",
    "/openapi.json",
    "/redoc",
    f"{settings.api_prefix}/facebook/webhook",
}


def _is_authenticated_request(request: Request) -> bool:
    token = request.cookies.get(settings.auth_cookie_name)
    payload = verify_session_token(token)
    if not payload:
        return False
    request.state.auth_user = payload.get("sub", "")
    return True


def _is_exempt_path(path: str) -> bool:
    if path in AUTH_EXEMPT_PATHS:
        return True
    if path.startswith("/ui/"):
        return True
    if path.startswith("/public/facebook-message-media/"):
        return True
    if path.startswith("/public/chatbot-product-media/"):
        return True
    if path.startswith(f"{settings.api_prefix}/auth/"):
        return True
    return False


def _request_api_token(request: Request) -> str:
    auth_header = str(request.headers.get("authorization") or "").strip()
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()
    if auth_header.lower().startswith("token "):
        return auth_header[6:].strip()
    if auth_header.startswith("cf_ext_"):
        return auth_header
    return str(
        request.headers.get("x-api-token")
        or request.headers.get("x-api-key")
        or request.headers.get("x-extension-token")
        or request.query_params.get("token")
        or request.query_params.get("api_token")
        or ""
    ).strip()


def _is_shopee_extension_path(path: str) -> bool:
    normalized = path.rstrip("/")
    return normalized == f"{settings.api_prefix}/shopee/products"


def _extension_authorized(request: Request) -> bool:
    if _is_authenticated_request(request):
        return True
    return bool(verify_api_token(_request_api_token(request)))


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if request.method.upper() == "OPTIONS":
        return await call_next(request)
    if _is_exempt_path(path):
        return await call_next(request)
    if _is_authenticated_request(request):
        return await call_next(request)
    if path.startswith(f"{settings.api_prefix}/public/") and verify_api_token(_request_api_token(request)):
        return await call_next(request)
    if path.startswith(f"{settings.api_prefix}/facebook/") and verify_api_token(_request_api_token(request)):
        return await call_next(request)
    if _is_shopee_extension_path(path) and request.method.upper() == "POST" and verify_api_token(_request_api_token(request)):
        return await call_next(request)
    if _is_shopee_extension_path(path) and request.method.upper() == "POST":
        token_preview = _request_api_token(request)[:12]
        log.warning(
            "shopee_extension_auth_failed",
            has_authorization=bool(request.headers.get("authorization")),
            has_x_api_token=bool(request.headers.get("x-api-token")),
            has_x_api_key=bool(request.headers.get("x-api-key")),
            has_x_extension_token=bool(request.headers.get("x-extension-token")),
            token_prefix=token_preview,
        )
    if path.startswith(settings.api_prefix):
        return JSONResponse(status_code=401, content={"detail": "Authentication required"})
    return RedirectResponse(url="/login", status_code=307)


PIPELINE_PROGRESS_STEPS = [
    "deduplicator",
    "fetcher",
    "extractor",
    "knowledge",
    "enricher",
    "planner",
    "image_selector",
    "media_uploader",
    "writer",
    "humanizer",
    "internal_linker",
    "qa",
    "seo_adjuster",
    "publisher",
]


def _job_list_items(states: list[dict]) -> list[JobListItem]:
    items: list[JobListItem] = []
    for state in states:
        current_step = state.get("current_step") or ""
        progress = int(((PIPELINE_PROGRESS_STEPS.index(current_step) + 1) / len(PIPELINE_PROGRESS_STEPS)) * 100) if current_step in PIPELINE_PROGRESS_STEPS else 0
        items.append(
            JobListItem(
                job_id=str(state.get("job_id") or ""),
                url=str(state.get("url") or ""),
                title=str((state.get("plan") or {}).get("title") or (state.get("fetch_result") or {}).get("title") or ""),
                site_id=str(state.get("site_id") or ""),
                site_name=str((state.get("site_profile") or {}).get("site_name") or state.get("site_name") or ""),
                content_mode=str(state.get("content_mode") or "shared"),
                batch_id=str(state.get("batch_id") or ""),
                parent_job_id=str(state.get("parent_job_id") or ""),
                workflow_role=str(state.get("workflow_role") or "standard"),
                priority=str(state.get("priority") or "normal"),
                status=str(state.get("status") or "pending"),
                current_step=current_step,
                progress_percent=progress,
                woo_post_id=state.get("woo_post_id"),
                woo_link=state.get("woo_link"),
                qa_score=(state.get("qa_result") or {}).get("overall_score"),
                processing_time_sec=(state.get("metrics") or {}).get("processing_time_sec"),
                estimated_cost_usd=(state.get("metrics") or {}).get("estimated_cost_usd"),
                publish_status=str(state.get("publish_status") or "draft"),
                created_at=state.get("created_at").isoformat() if hasattr(state.get("created_at"), "isoformat") else str(state.get("created_at") or ""),
                updated_at=state.get("updated_at").isoformat() if hasattr(state.get("updated_at"), "isoformat") else str(state.get("updated_at") or ""),
                error=state.get("error"),
                dlq=str(state.get("status") or "") == "failed",
            )
        )
    return items


def _site_profile_payload(site: dict) -> dict:
    return {
        "site_id": site.get("site_id", ""),
        "site_name": site.get("site_name", ""),
        "url": site.get("url", ""),
        "topic": site.get("topic", ""),
        "primary_color": site.get("primary_color", "#22c55e"),
        "consumer_key": site.get("consumer_key", ""),
        "consumer_secret": site.get("consumer_secret", ""),
        "username": site.get("username", ""),
        "app_password": site.get("app_password", ""),
        "shopee_affiliate_post_type": site.get("shopee_affiliate_post_type", "affiliate_product"),
        "shopee_affiliate_rest_base": site.get("shopee_affiliate_rest_base", ""),
        "shopee_affiliate_query": site.get("shopee_affiliate_query", ""),
    }


async def _resolve_sites(site_ids: list[str]) -> list[dict]:
    sites: list[dict] = []
    for site_id in site_ids:
        site = await asyncio.to_thread(get_site, site_id)
        if not site:
            raise HTTPException(status_code=404, detail=f"Site not found: {site_id}")
        sites.append(site)
    return sites


async def _enqueue_multi_site_batch(
    *,
    urls: list[str],
    sites: list[dict],
    content_mode: str,
    woo_category_id: int,
    focus_keyword: str | None,
    priority: str,
    publish_status: str,
    source_origin: str = "",
    source_seed: dict | None = None,
) -> SubmitBatchResponse:
    batch_id = create_job_id()
    master_job_ids: list[str] = []
    child_job_ids: list[str] = []

    if len(sites) == 1:
        site = sites[0]
        for url in urls:
            payload = PipelineState(
                url=str(url),
                site_id=str(site.get("site_id") or ""),
                content_mode=content_mode,
                site_profile=_site_profile_payload(site),
                source_origin=source_origin,
                source_seed=source_seed or {},
                priority=priority,
                woo_category_id=woo_category_id,
                focus_keyword_override=focus_keyword,
                publish_status=publish_status,
            )
            job_id = create_job_id()
            init_job_state(job_id, payload)
            state = get_job(job_id) or {}
            state["batch_id"] = batch_id
            state["workflow_role"] = "standard"
            state["site_name"] = site.get("site_name", "")
            update_job(job_id, state)
            queue_name = enqueue_saved_state(job_id, state)
            if queue_name == "inline":
                asyncio.create_task(run_pipeline_async(job_id, state))
            child_job_ids.append(job_id)
            jobs_submitted.inc()
        return SubmitBatchResponse(
            batch_id=batch_id,
            status="queued",
            total_jobs=len(child_job_ids),
            master_job_ids=[],
            child_job_ids=child_job_ids,
        )

    if content_mode == "per-site":
        for url in urls:
            for site in sites:
                payload = PipelineState(
                    url=str(url),
                    site_id=str(site.get("site_id") or ""),
                    content_mode="per-site",
                    site_profile=_site_profile_payload(site),
                    source_origin=source_origin,
                    source_seed=source_seed or {},
                    priority=priority,
                    woo_category_id=woo_category_id,
                    focus_keyword_override=focus_keyword,
                    publish_status=publish_status,
                )
                job_id = create_job_id()
                init_job_state(job_id, payload)
                state = get_job(job_id) or {}
                state["batch_id"] = batch_id
                state["workflow_role"] = "standard"
                state["site_name"] = site.get("site_name", "")
                update_job(job_id, state)
                queue_name = enqueue_saved_state(job_id, state)
                if queue_name == "inline":
                    asyncio.create_task(run_pipeline_async(job_id, state))
                child_job_ids.append(job_id)
                jobs_submitted.inc()
        return SubmitBatchResponse(
            batch_id=batch_id,
            status="queued",
            total_jobs=len(child_job_ids),
            master_job_ids=[],
            child_job_ids=child_job_ids,
        )

    for url in urls:
        master_job_id = create_job_id()
        master_payload = PipelineState(
            url=str(url),
            site_id="",
            content_mode="shared",
            site_profile={},
            source_origin=source_origin,
            source_seed=source_seed or {},
            priority=priority,
            woo_category_id=woo_category_id,
            focus_keyword_override=focus_keyword,
            publish_status=publish_status,
        )
        init_job_state(master_job_id, master_payload)
        master_state = get_job(master_job_id) or {}
        master_state["batch_id"] = batch_id
        master_state["workflow_role"] = "shared_master"
        master_state["site_name"] = ""
        master_state["child_job_ids"] = []
        update_job(master_job_id, master_state)
        queue_name = enqueue_saved_state(master_job_id, master_state)
        if queue_name == "inline":
            asyncio.create_task(run_pipeline_async(master_job_id, master_state))
        jobs_submitted.inc()
        master_job_ids.append(master_job_id)

        for site in sites:
            child_job_id = create_job_id()
            child_payload = PipelineState(
                url=str(url),
                site_id=str(site.get("site_id") or ""),
                content_mode="shared",
                site_profile=_site_profile_payload(site),
                source_origin=source_origin,
                source_seed=source_seed or {},
                priority=priority,
                woo_category_id=woo_category_id,
                focus_keyword_override=focus_keyword,
                publish_status=publish_status,
            )
            init_job_state(child_job_id, child_payload)
            child_state = get_job(child_job_id) or {}
            child_state["batch_id"] = batch_id
            child_state["parent_job_id"] = master_job_id
            child_state["workflow_role"] = "shared_publish_child"
            child_state["site_name"] = site.get("site_name", "")
            update_job(child_job_id, child_state)
            child_job_ids.append(child_job_id)
            master_state.setdefault("child_job_ids", []).append(child_job_id)

        update_job(master_job_id, master_state)

    return SubmitBatchResponse(
        batch_id=batch_id,
        status="queued",
        total_jobs=len(master_job_ids) + len(child_job_ids),
        master_job_ids=master_job_ids,
        child_job_ids=child_job_ids,
    )


async def _enqueue_website_keyword_batch(
    *,
    keywords: list[str],
    sites: list[dict],
    content_mode: str,
    category_id: int,
    priority: str,
    publish_status: str,
    brief: str = "",
) -> SubmitBatchResponse:
    batch_id = create_job_id()
    master_job_ids: list[str] = []
    child_job_ids: list[str] = []

    async def enqueue_standard(keyword: str, site: dict, mode: str) -> str:
        payload = PipelineState(
            url=f"keyword://{keyword}",
            site_id=str(site.get("site_id") or ""),
            content_mode=mode,
            site_profile=_site_profile_payload(site),
            source_origin="website_keyword",
            source_seed={"keyword": keyword, "brief": brief, "source_url": f"keyword://{keyword}"},
            priority=priority,
            woo_category_id=category_id,
            focus_keyword_override=keyword,
            publish_status=publish_status,
        )
        job_id = create_job_id()
        init_job_state(job_id, payload)
        state = get_job(job_id) or {}
        state["batch_id"] = batch_id
        state["workflow_role"] = "standard"
        state["site_name"] = site.get("site_name", "")
        update_job(job_id, state)
        queue_name = enqueue_saved_state(job_id, state)
        if queue_name == "inline":
            asyncio.create_task(run_pipeline_async(job_id, state))
        jobs_submitted.inc()
        return job_id

    if len(sites) == 1 or content_mode == "per-site":
        for keyword in keywords:
            for site in sites:
                child_job_ids.append(await enqueue_standard(keyword, site, "per-site" if content_mode == "per-site" else content_mode))
        return SubmitBatchResponse(batch_id=batch_id, status="queued", total_jobs=len(child_job_ids), child_job_ids=child_job_ids)

    for keyword in keywords:
        master_payload = PipelineState(
            url=f"keyword://{keyword}",
            site_id="",
            content_mode="shared",
            site_profile={},
            source_origin="website_keyword",
            source_seed={"keyword": keyword, "brief": brief, "source_url": f"keyword://{keyword}"},
            priority=priority,
            woo_category_id=category_id,
            focus_keyword_override=keyword,
            publish_status=publish_status,
        )
        master_job_id = create_job_id()
        init_job_state(master_job_id, master_payload)
        master_state = get_job(master_job_id) or {}
        master_state["batch_id"] = batch_id
        master_state["workflow_role"] = "shared_master"
        master_state["site_name"] = ""
        master_state["child_job_ids"] = []
        update_job(master_job_id, master_state)
        queue_name = enqueue_saved_state(master_job_id, master_state)
        if queue_name == "inline":
            asyncio.create_task(run_pipeline_async(master_job_id, master_state))
        jobs_submitted.inc()
        master_job_ids.append(master_job_id)

        for site in sites:
            child_payload = PipelineState(
                url=f"keyword://{keyword}",
                site_id=str(site.get("site_id") or ""),
                content_mode="shared",
                site_profile=_site_profile_payload(site),
                source_origin="website_keyword",
                source_seed={"keyword": keyword, "brief": brief, "source_url": f"keyword://{keyword}"},
                priority=priority,
                woo_category_id=category_id,
                focus_keyword_override=keyword,
                publish_status=publish_status,
            )
            child_job_id = create_job_id()
            init_job_state(child_job_id, child_payload)
            child_state = get_job(child_job_id) or {}
            child_state["batch_id"] = batch_id
            child_state["parent_job_id"] = master_job_id
            child_state["workflow_role"] = "shared_publish_child"
            child_state["site_name"] = site.get("site_name", "")
            update_job(child_job_id, child_state)
            child_job_ids.append(child_job_id)
            master_state.setdefault("child_job_ids", []).append(child_job_id)
        update_job(master_job_id, master_state)

    return SubmitBatchResponse(
        batch_id=batch_id,
        status="queued",
        total_jobs=len(master_job_ids) + len(child_job_ids),
        master_job_ids=master_job_ids,
        child_job_ids=child_job_ids,
    )


@app.on_event("startup")
async def on_startup() -> None:
    init_postgres_schema()
    migrated = migrate_local_postgres_state()
    if any(migrated.values()):
        log.info("postgres_local_state_migrated", **migrated)
    shopee_seed = import_legacy_sample()
    if shopee_seed.get("imported"):
        log.info("shopee_legacy_sample_imported", **shopee_seed)
    if settings.metrics_enabled:
        start_metrics_server_once(settings.metrics_port)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "queue_mode": settings.queue_mode}


@app.get("/login")
async def login_page(request: Request):
    if _is_authenticated_request(request):
        return RedirectResponse(url="/", status_code=307)
    return FileResponse(UI_DIR / "login.html")


@app.get("/favicon.ico")
async def favicon() -> FileResponse:
    return FileResponse(UI_DIR / "logo.png", media_type="image/png")


@app.get(f"{settings.api_prefix}/auth/me", response_model=AuthMeResponse)
async def auth_me(request: Request) -> AuthMeResponse:
    username = ""
    authenticated = _is_authenticated_request(request)
    if authenticated:
        username = getattr(request.state, "auth_user", "")
    return AuthMeResponse(authenticated=authenticated, username=username)


@app.post(f"{settings.api_prefix}/auth/login", response_model=LoginResponse)
async def auth_login(request: LoginRequest, response: Response) -> LoginResponse:
    if not authenticate_credentials(request.username, request.password):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    token, max_age = create_session_token(request.username, request.remember)
    response.set_cookie(
        key=settings.auth_cookie_name,
        value=token,
        httponly=True,
        samesite="lax",
        secure=False,
        max_age=max_age,
        path="/",
    )
    return LoginResponse(authenticated=True, username=request.username.strip(), redirect_url="/")


@app.post(f"{settings.api_prefix}/auth/logout")
async def auth_logout(response: Response) -> dict:
    response.delete_cookie(settings.auth_cookie_name, path="/")
    return {"authenticated": False}


@app.get(f"{settings.api_prefix}/settings/tokens", response_model=ApiTokenListResponse)
async def settings_tokens() -> ApiTokenListResponse:
    items = await asyncio.to_thread(list_api_tokens)
    return ApiTokenListResponse(total=len(items), tokens=[ApiTokenListItem(**{
        "token_id": item.get("token_id", ""),
        "name": item.get("name", ""),
        "token_prefix": item.get("token_prefix", ""),
        "created_at": item.get("created_at", ""),
        "updated_at": item.get("updated_at", ""),
        "last_used_at": item.get("last_used_at", ""),
        "status": item.get("status", "active"),
    }) for item in items])


@app.post(f"{settings.api_prefix}/settings/tokens", response_model=ApiTokenCreateResponse)
async def settings_create_token(request: ApiTokenCreateRequest) -> ApiTokenCreateResponse:
    item, raw_token = await asyncio.to_thread(create_api_token, request.name)
    log.info("api_token_created", token_id=item.get("token_id"), name=item.get("name"))
    return ApiTokenCreateResponse(
        token=raw_token,
        token_item=ApiTokenListItem(
            token_id=item.get("token_id", ""),
            name=item.get("name", ""),
            token_prefix=item.get("token_prefix", ""),
            created_at=item.get("created_at", ""),
            updated_at=item.get("updated_at", ""),
            last_used_at=item.get("last_used_at", ""),
            status=item.get("status", "active"),
        ),
    )


@app.delete(f"{settings.api_prefix}/settings/tokens/{{token_id}}")
async def settings_delete_token(token_id: str) -> dict:
    deleted = await asyncio.to_thread(delete_api_token, token_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="API token not found")
    log.info("api_token_deleted", token_id=token_id)
    return {"deleted": True, "token_id": token_id}


@app.get(f"{settings.api_prefix}/facebook/pages", response_model=FacebookPageListResponse)
async def get_facebook_pages() -> FacebookPageListResponse:
    pages = await asyncio.to_thread(list_facebook_pages)
    return FacebookPageListResponse(total=len(pages), pages=pages)


@app.get(f"{settings.api_prefix}/facebook/pages/{{page_id}}/{{asset}}")
async def get_facebook_page_asset_endpoint(page_id: str, asset: str) -> Response:
    try:
        content, media_type = await asyncio.to_thread(get_facebook_page_asset, page_id, asset)
    except httpx.HTTPStatusError as error:
        status = error.response.status_code if error.response is not None else 502
        raise HTTPException(status_code=502 if status >= 500 else status, detail="Facebook page image could not be fetched.") from error
    except RuntimeError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Cache-Control": "public, max-age=86400",
        },
    )


@app.post(f"{settings.api_prefix}/facebook/pages/connect", response_model=FacebookConnectResponse)
async def connect_facebook_pages_endpoint(request: FacebookConnectRequest) -> FacebookConnectResponse:
    try:
        result = await asyncio.to_thread(connect_facebook_pages, request.short_lived_token)
    except httpx.HTTPStatusError as error:
        body = error.response.text[:500] if error.response is not None else str(error)
        raise HTTPException(status_code=400, detail=f"Facebook Graph API failed: {body}") from error
    except RuntimeError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    log.info("facebook_pages_connected", total=result.get("total"), batch_id=result.get("batch_id"))
    return FacebookConnectResponse(**result)


@app.post(f"{settings.api_prefix}/facebook/pages/resubscribe-webhooks")
async def resubscribe_facebook_page_webhooks_endpoint() -> dict:
    return await asyncio.to_thread(resubscribe_facebook_page_webhooks)


@app.get(f"{settings.api_prefix}/facebook/page-groups", response_model=FacebookPageGroupListResponse)
async def get_facebook_page_groups() -> FacebookPageGroupListResponse:
    groups = await asyncio.to_thread(list_facebook_page_groups)
    return FacebookPageGroupListResponse(total=len(groups), groups=groups)


@app.post(f"{settings.api_prefix}/facebook/page-groups")
async def create_facebook_page_group_endpoint(request: FacebookPageGroupCreateRequest) -> dict:
    try:
        return await asyncio.to_thread(create_facebook_page_group, request.name)
    except RuntimeError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.patch(f"{settings.api_prefix}/facebook/pages/{{page_id}}/group")
async def update_facebook_page_group_endpoint(page_id: str, request: FacebookPageGroupUpdateRequest) -> dict:
    try:
        page = await asyncio.to_thread(update_facebook_page_group, page_id, request.group)
    except RuntimeError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return page


@app.get(f"{settings.api_prefix}/facebook/stats", response_model=FacebookStatsResponse)
async def get_facebook_stats(days: int = 7) -> FacebookStatsResponse:
    result = await asyncio.to_thread(facebook_aggregate_stats, max(1, min(days, 30)))
    return FacebookStatsResponse(**result)


@app.post(f"{settings.api_prefix}/facebook/stats/sync", response_model=FacebookStatsResponse)
async def sync_facebook_stats_endpoint(days: int = 7) -> FacebookStatsResponse:
    result = await asyncio.to_thread(sync_facebook_aggregate_stats, max(1, min(days, 30)))
    return FacebookStatsResponse(**result)


@app.get(f"{settings.api_prefix}/facebook/posts", response_model=FacebookPostListResponse)
async def get_facebook_posts(limit: int = 50, offset: int = 0) -> FacebookPostListResponse:
    result = await asyncio.to_thread(facebook_posts, max(1, min(limit, 100)), max(0, offset))
    return FacebookPostListResponse(**result)


@app.post(f"{settings.api_prefix}/facebook/posts/sync", response_model=FacebookPostListResponse)
async def sync_facebook_posts_endpoint(limit: int = 50) -> FacebookPostListResponse:
    result = await asyncio.to_thread(sync_facebook_posts, max(1, min(limit, 100)))
    return FacebookPostListResponse(**result)


@app.get(f"{settings.api_prefix}/facebook/comments", response_model=FacebookCommentListResponse)
async def get_facebook_comments(limit: int = 50) -> FacebookCommentListResponse:
    result = await asyncio.to_thread(facebook_comments, max(1, min(limit, 100)))
    return FacebookCommentListResponse(**result)


@app.post(f"{settings.api_prefix}/facebook/comments/sync", response_model=FacebookCommentListResponse)
async def sync_facebook_comments_endpoint(limit: int = 50) -> FacebookCommentListResponse:
    result = await asyncio.to_thread(sync_facebook_comments, max(1, min(limit, 100)))
    return FacebookCommentListResponse(**result)


@app.get(f"{settings.api_prefix}/facebook/conversations", response_model=FacebookConversationListResponse)
async def get_facebook_conversations(limit: int = 25, message_limit: int = 1) -> FacebookConversationListResponse:
    result = await asyncio.to_thread(
        facebook_conversations,
        max(1, min(limit, 100)),
        500,
        max(0, min(message_limit, 200)),
    )
    return FacebookConversationListResponse(**result)


@app.get(f"{settings.api_prefix}/facebook/conversations/{{conversation_id}}")
async def get_facebook_conversation_detail(conversation_id: str, message_limit: int = 100) -> dict:
    try:
        return await asyncio.to_thread(facebook_conversation_detail, conversation_id, max(1, min(message_limit, 200)))
    except RuntimeError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.post(f"{settings.api_prefix}/facebook/conversations/sync")
async def sync_facebook_conversations_endpoint(limit: int = 50) -> dict:
    job = await asyncio.to_thread(enqueue_facebook_conversation_sync, max(1, min(limit, 100)))
    return {"queued": job.get("status") in {"queued", "running", "completed"}, "job": job}


@app.get(f"{settings.api_prefix}/facebook/conversations/sync/{{job_id}}")
async def get_facebook_conversations_sync_job_endpoint(job_id: str) -> dict:
    job = await asyncio.to_thread(get_facebook_sync_job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Facebook sync job not found.")
    return {"job": job}


@app.get(f"{settings.api_prefix}/facebook/realtime/debug")
async def facebook_realtime_debug_endpoint() -> dict:
    redis_conn = _realtime_redis_client()
    redis_ok = redis_conn is not None
    if redis_conn is not None:
        with suppress(Exception):
            redis_conn.close()
    return {
        "redis_ok": redis_ok,
        "queue_mode": settings.queue_mode,
        "latest_sync_jobs": await asyncio.to_thread(latest_facebook_sync_jobs, 5),
        "webhook_path": f"{settings.api_prefix}/facebook/webhook",
    }


@app.post(f"{settings.api_prefix}/facebook/messages/send", response_model=FacebookMessageSendResponse)
async def send_facebook_message_endpoint(request: FacebookMessageSendRequest) -> FacebookMessageSendResponse:
    try:
        result = await asyncio.to_thread(
            send_facebook_message,
            request.conversation_id,
            request.message,
            request.attachment_url,
            request.attachment_type,
            request.attachment_name,
            request.attachments,
        )
    except httpx.HTTPStatusError as error:
        detail = error.response.text[:500] if error.response is not None else str(error)
        raise HTTPException(status_code=400, detail=f"Facebook message send failed: {detail}") from error
    except RuntimeError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return FacebookMessageSendResponse(**result)


@app.post(f"{settings.api_prefix}/facebook/conversations/{{conversation_id}}/read")
async def mark_facebook_conversation_read_endpoint(conversation_id: str) -> dict:
    try:
        conversation = await asyncio.to_thread(mark_facebook_conversation_read, conversation_id)
    except RuntimeError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"ok": True, "conversation": conversation}


@app.get(f"{settings.api_prefix}/facebook/slash-commands")
async def get_facebook_slash_commands_endpoint() -> dict:
    commands = await asyncio.to_thread(list_facebook_slash_commands)
    return {"total": len(commands), "commands": commands}


@app.post(f"{settings.api_prefix}/facebook/slash-commands")
async def upsert_facebook_slash_command_endpoint(request: Request) -> dict:
    payload = await request.json()
    try:
        command = await asyncio.to_thread(
            upsert_facebook_slash_command,
            {
                "command": payload.get("command", ""),
                "label": payload.get("label", ""),
                "text": payload.get("text", ""),
            },
            str(payload.get("original_command") or ""),
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    commands = await asyncio.to_thread(list_facebook_slash_commands)
    return {"ok": True, "command": command, "commands": commands}


@app.delete(f"{settings.api_prefix}/facebook/slash-commands")
async def delete_facebook_slash_command_endpoint(command: str = Query(...)) -> dict:
    try:
        deleted = await asyncio.to_thread(delete_facebook_slash_command, command)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    commands = await asyncio.to_thread(list_facebook_slash_commands)
    return {"ok": True, "deleted": deleted, "commands": commands}


@app.post(f"{settings.api_prefix}/facebook/messages/media")
async def upload_facebook_message_media_endpoint(request: Request, file: UploadFile = File(...)) -> dict:
    content_type = str(file.content_type or "application/octet-stream")
    allowed = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "video/mp4": ".mp4",
        "audio/mpeg": ".mp3",
        "audio/mp4": ".m4a",
        "application/pdf": ".pdf",
    }
    extension = allowed.get(content_type) or mimetypes.guess_extension(content_type) or ""
    if content_type.startswith("image/") and not extension:
        extension = ".jpg"
    if not (content_type.startswith("image/") or content_type.startswith("video/") or content_type.startswith("audio/") or content_type == "application/pdf"):
        raise HTTPException(status_code=400, detail="Unsupported media type.")
    max_bytes = 25 * 1024 * 1024
    media_id = secrets.token_hex(16)
    target = FACEBOOK_MESSAGE_MEDIA_DIR / f"{media_id}{extension}"
    size = 0
    with target.open("wb") as handle:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > max_bytes:
                target.unlink(missing_ok=True)
                raise HTTPException(status_code=400, detail="Media file is too large.")
            handle.write(chunk)
    proto = str(request.headers.get("x-forwarded-proto") or request.url.scheme or "https")
    host = str(request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc)
    public_url = f"{proto}://{host}/public/facebook-message-media/{target.name}"
    media_type = "image" if content_type.startswith("image/") else "video" if content_type.startswith("video/") else "audio" if content_type.startswith("audio/") else "file"
    return {
        "media_id": media_id,
        "url": public_url,
        "type": media_type,
        "name": file.filename or target.name,
        "mime_type": content_type,
        "size": size,
    }


@app.get(f"{settings.api_prefix}/facebook/messages/debug")
async def debug_facebook_messages_endpoint(conversation_id: str = "", message_id: str = "") -> JSONResponse:
    try:
        result = await asyncio.to_thread(debug_facebook_messages, conversation_id, message_id)
    except RuntimeError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return JSONResponse(result)


@app.get(f"{settings.api_prefix}/facebook/webhook")
async def verify_facebook_webhook(
    hub_mode: str = Query("", alias="hub.mode"),
    hub_verify_token: str = Query("", alias="hub.verify_token"),
    hub_challenge: str = Query("", alias="hub.challenge"),
) -> Response:
    expected_token = settings.facebook_webhook_verify_token or settings.auth_secret
    if hub_mode == "subscribe" and hub_verify_token == expected_token:
        return Response(content=hub_challenge, media_type="text/plain")
    raise HTTPException(status_code=403, detail="Invalid Facebook webhook verification token.")


@app.post(f"{settings.api_prefix}/facebook/webhook")
async def receive_facebook_webhook(request: Request) -> JSONResponse:
    body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")
    if not verify_facebook_webhook_signature(body, signature):
        raise HTTPException(status_code=403, detail="Invalid Facebook webhook signature.")
    try:
        payload = json.loads(body.decode("utf-8") or "{}")
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Invalid webhook payload.") from error
    result = await asyncio.to_thread(process_facebook_webhook, payload)
    return JSONResponse({"ok": True, **result})


@app.post(f"{settings.api_prefix}/rag/ingest")
async def rag_ingest(request: RAGIngestRequest, background_tasks: BackgroundTasks) -> dict:
    job = await asyncio.to_thread(
        create_rag_job,
        "url",
        {
            "url": str(request.url),
            "manual_categories": request.manual_categories,
            "manual_tags": request.manual_tags,
            "note": request.note,
            "force_reingest": request.force_reingest,
        },
    )
    background_tasks.add_task(run_rag_job, job["job_id"])
    return {"job_id": job["job_id"], "status": job["status"], "job": public_rag_job(job)}


@app.post(f"{settings.api_prefix}/rag/ingest-text")
async def rag_ingest_text(request: RAGTextIngestRequest, background_tasks: BackgroundTasks) -> dict:
    job = await asyncio.to_thread(
        create_rag_job,
        "text",
        {
            "title": request.title,
            "content": request.content,
            "manual_categories": request.manual_categories,
            "manual_tags": request.manual_tags,
            "note": request.note,
            "source_id": request.source_id,
            "force_reingest": request.force_reingest,
        },
    )
    background_tasks.add_task(run_rag_job, job["job_id"])
    return {"job_id": job["job_id"], "status": job["status"], "job": public_rag_job(job)}


@app.post(f"{settings.api_prefix}/rag/ingest-file")
async def rag_ingest_file(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    title: str = Form(""),
    manual_category: str = Form(""),
    note: str = Form(""),
    force_reingest: bool = Form(True),
) -> dict:
    filename = file.filename or "knowledge.txt"
    suffix = Path(filename).suffix.lower()
    allowed_suffixes = {".txt", ".md", ".markdown", ".csv"}
    if suffix not in allowed_suffixes:
        raise HTTPException(status_code=400, detail="Only .txt, .md, .markdown and .csv files are supported.")

    max_bytes = int(getattr(settings, "rag_file_max_mb", 10) or 10) * 1024 * 1024
    payload = await file.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise HTTPException(status_code=400, detail=f"Knowledge file is too large. Maximum size is {max_bytes // 1024 // 1024}MB.")
    cleaned_title = title.strip() or Path(filename).stem.replace("_", " ").replace("-", " ").strip() or filename
    categories = [manual_category.strip()] if manual_category.strip() else []
    upload_dir = Path("data/rag_uploads")
    upload_dir.mkdir(parents=True, exist_ok=True)
    upload_path = upload_dir / f"{secrets.token_hex(12)}{suffix}"
    upload_path.write_bytes(payload)

    job = await asyncio.to_thread(
        create_rag_job,
        "file",
        {
            "filename": filename,
            "title": cleaned_title,
            "upload_path": str(upload_path),
            "manual_categories": categories,
            "manual_tags": [],
            "note": note.strip() or None,
            "source_id": f"file-{filename}",
            "force_reingest": force_reingest,
        },
    )
    background_tasks.add_task(run_rag_job, job["job_id"])
    return {"job_id": job["job_id"], "status": job["status"], "job": public_rag_job(job)}


@app.get(f"{settings.api_prefix}/rag/jobs")
async def rag_jobs(limit: int = 20) -> dict:
    jobs = await asyncio.to_thread(list_rag_jobs, max(1, min(limit, 100)))
    return {"total": len(jobs), "jobs": [public_rag_job(job) for job in jobs]}


@app.get(f"{settings.api_prefix}/rag/jobs/{{job_id}}")
async def rag_job(job_id: str) -> dict:
    job = await asyncio.to_thread(get_rag_job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="RAG job not found")
    return public_rag_job(job)


@app.get(f"{settings.api_prefix}/rag/categories", response_model=RAGCategoryListResponse)
async def rag_categories() -> RAGCategoryListResponse:
    categories = await asyncio.to_thread(list_categories)
    return RAGCategoryListResponse(total=len(categories), categories=categories)


@app.post(f"{settings.api_prefix}/rag/categories", response_model=RAGCategoryListResponse)
async def rag_create_category(request: RAGCategoryCreate) -> RAGCategoryListResponse:
    try:
        await asyncio.to_thread(create_category, request.name)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    categories = await asyncio.to_thread(list_categories)
    return RAGCategoryListResponse(total=len(categories), categories=categories)


@app.get(f"{settings.api_prefix}/rag/search", response_model=RAGSearchResponse)
async def rag_search(q: str, limit: int = 5, category: str | None = None) -> RAGSearchResponse:
    result = await asyncio.to_thread(search_knowledge, q, max(1, min(limit, 20)), category)
    return RAGSearchResponse(**result)


@app.get(f"{settings.api_prefix}/rag/source", response_model=RAGSourceResponse)
async def rag_source(url: str) -> RAGSourceResponse:
    result = await asyncio.to_thread(get_source_documents, url)
    return RAGSourceResponse(**result)


@app.get(f"{settings.api_prefix}/rag/sources", response_model=RAGSourceListResponse)
async def rag_sources(category: str | None = None, search: str | None = None, limit: int = 100) -> RAGSourceListResponse:
    result = await asyncio.to_thread(list_rag_sources, category, search, max(1, min(limit, 200)))
    return RAGSourceListResponse(**result)


@app.get(f"{settings.api_prefix}/rag/taxonomy", response_model=RAGTaxonomyResponse)
async def rag_taxonomy(category: str | None = None) -> RAGTaxonomyResponse:
    result = await asyncio.to_thread(get_taxonomy_summary, category)
    return RAGTaxonomyResponse(**result)


@app.delete(f"{settings.api_prefix}/rag/source")
async def rag_delete_source(url: str) -> dict:
    result = await asyncio.to_thread(delete_source_documents, url)
    log.info("rag_source_deleted", source_url=url, deleted_count=result.get("deleted_count", 0))
    return result


@app.post(f"{settings.api_prefix}/submit", response_model=SubmitResponse)
async def submit_job(request: SubmitRequest) -> SubmitResponse:
    if queue_is_full():
        raise HTTPException(status_code=429, detail="Queue day (> 100 jobs dang cho)")

    site_profile = {}
    if request.site_id:
        site = await asyncio.to_thread(get_site, request.site_id)
        if not site:
            raise HTTPException(status_code=404, detail="Site not found")
        site_profile = _site_profile_payload(site)

    job_id = create_job_id()
    payload = PipelineState(
        url=str(request.url),
        site_id=request.site_id,
        content_mode=request.content_mode,
        site_profile=site_profile,
        priority=request.priority,
        woo_category_id=request.woo_category_id,
        focus_keyword_override=request.focus_keyword,
        publish_status=request.publish_status,
    )
    init_job_state(job_id, payload)
    queue_name = enqueue_job(job_id, payload)
    jobs_submitted.inc()
    log.info(
        "job_submitted",
        job_id=job_id,
        url=str(request.url),
        site_id=request.site_id,
        content_mode=request.content_mode,
        priority=request.priority,
        queue=queue_name,
        publish_status=request.publish_status,
    )
    if queue_name == "inline":
        asyncio.create_task(run_pipeline_async(job_id, payload.model_dump(by_alias=True)))
        queue_name = "content_pipeline"

    return SubmitResponse(
        job_id=job_id,
        status="queued",
        queue=queue_name,
        estimated_wait_sec=120,
        check_url=f"{settings.api_prefix}/job/{job_id}",
    )


@app.post(f"{settings.api_prefix}/submit-batch", response_model=SubmitBatchResponse)
async def submit_batch(request: SubmitBatchRequest) -> SubmitBatchResponse:
    if queue_is_full():
        raise HTTPException(status_code=429, detail="Queue day (> 100 jobs dang cho)")
    if not request.urls:
        raise HTTPException(status_code=400, detail="At least one URL is required")
    if not request.site_ids:
        raise HTTPException(status_code=400, detail="At least one site is required")
    sites = await _resolve_sites(request.site_ids)
    return await _enqueue_multi_site_batch(
        urls=[str(url) for url in request.urls],
        sites=sites,
        content_mode=request.content_mode,
        woo_category_id=request.woo_category_id,
        focus_keyword=request.focus_keyword,
        priority=request.priority,
        publish_status=request.publish_status,
    )


@app.post(f"{settings.api_prefix}/website/posts/submit", response_model=SubmitBatchResponse)
async def submit_website_posts(request: WebsitePostSubmitRequest) -> SubmitBatchResponse:
    if queue_is_full():
        raise HTTPException(status_code=429, detail="Queue day (> 100 jobs dang cho)")
    if not request.site_ids:
        raise HTTPException(status_code=400, detail="At least one site is required")
    sites = await _resolve_sites(request.site_ids)
    if request.mode == "urls":
        if not request.urls:
            raise HTTPException(status_code=400, detail="At least one URL is required")
        return await _enqueue_multi_site_batch(
            urls=[str(url) for url in request.urls],
            sites=sites,
            content_mode=request.content_mode,
            woo_category_id=request.category_id,
            focus_keyword=None,
            priority=request.priority,
            publish_status=request.publish_status,
            source_origin="website_article_url",
        )

    keywords = []
    seen: set[str] = set()
    for keyword in request.keywords:
        text = re.sub(r"\s+", " ", str(keyword or "").strip())
        key = text.lower()
        if text and key not in seen:
            seen.add(key)
            keywords.append(text)
    if not keywords:
        raise HTTPException(status_code=400, detail="At least one keyword is required")
    return await _enqueue_website_keyword_batch(
        keywords=keywords[:100],
        sites=sites,
        content_mode=request.content_mode,
        category_id=request.category_id,
        priority=request.priority,
        publish_status=request.publish_status,
        brief=request.brief,
    )


@app.get(f"{settings.api_prefix}/shopee/products", response_model=ShopeeProductListResponse)
async def shopee_products(search: str | None = None, limit: int = 100) -> ShopeeProductListResponse:
    payload = await asyncio.to_thread(list_shopee_products, search, max(1, min(limit, 200)))
    return ShopeeProductListResponse(
        source_url=str(payload.get("source_url") or ""),
        category_label=str(payload.get("category_label") or ""),
        total=int(payload.get("total") or 0),
        items=[ShopeeProductListItem(**item) for item in (payload.get("items") or [])],
    )


@app.get(f"{settings.api_prefix}/shopee/products/{{item_id}}", response_model=ShopeeProductDetailResponse)
async def shopee_product_detail(item_id: str) -> ShopeeProductDetailResponse:
    payload = await asyncio.to_thread(get_shopee_product, item_id)
    if not payload:
        raise HTTPException(status_code=404, detail="Shopee product not found")
    return ShopeeProductDetailResponse(**payload)


@app.delete(f"{settings.api_prefix}/shopee/products/{{item_id}}")
async def shopee_product_delete(item_id: str) -> dict:
    deleted = await asyncio.to_thread(delete_shopee_product, item_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Shopee product not found")
    log.info("shopee_product_deleted", item_id=item_id)
    return {"ok": True, "item_id": item_id}


@app.post(f"{settings.api_prefix}/shopee/products", response_model=ShopeeProductDetailResponse)
async def shopee_product_upsert(request: ShopeeUpsertRequest, http_request: Request) -> ShopeeProductDetailResponse:
    if not _extension_authorized(http_request):
        raise HTTPException(status_code=401, detail="Valid session or API token required")
    try:
        payload = await asyncio.to_thread(upsert_shopee_product, request.product)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    normalized = payload.get("normalized") or {}
    raw_images = request.product.get("images") or request.product.get("imageUrls") or request.product.get("image_urls") or []
    log.info(
        "shopee_product_upserted",
        item_id=payload.get("item_id"),
        title=normalized.get("product_title"),
        raw_images_type=type(raw_images).__name__,
        normalized_image_count=len(normalized.get("images") or []),
        normalized_image_preview=str((normalized.get("images") or [""])[0] or "")[:96],
    )
    return ShopeeProductDetailResponse(
        item_id=str(payload.get("item_id") or ""),
        raw=payload.get("raw") or {},
        normalized=normalized,
    )


@app.post(f"{settings.api_prefix}/shopee/products/{{item_id}}/enqueue", response_model=SubmitBatchResponse)
async def shopee_enqueue(item_id: str, request: ShopeeEnqueueRequest) -> SubmitBatchResponse:
    if queue_is_full():
        raise HTTPException(status_code=429, detail="Queue day (> 100 jobs dang cho)")
    if not request.site_ids:
        raise HTTPException(status_code=400, detail="At least one site is required")
    payload = await asyncio.to_thread(get_shopee_product, item_id)
    if not payload:
        raise HTTPException(status_code=404, detail="Shopee product not found")
    sites = await _resolve_sites(request.site_ids)
    normalized = payload.get("normalized") or {}
    title = str(normalized.get("product_title") or "").strip()
    return await _enqueue_multi_site_batch(
        urls=[str(normalized.get("source_url") or "")],
        sites=sites,
        content_mode=request.content_mode,
        woo_category_id=request.woo_category_id,
        focus_keyword=title,
        priority=request.priority,
        publish_status=request.publish_status,
        source_origin="shopee",
        source_seed=payload,
    )


@app.get(f"{settings.api_prefix}/chatbot/products")
async def chatbot_products(search: str | None = None, category_id: str = "", status: str = "", limit: int = 100) -> dict:
    return await asyncio.to_thread(list_chatbot_products, search, category_id, status, max(1, min(limit, 500)))


@app.get(f"{settings.api_prefix}/chatbot/labels")
async def chatbot_product_labels(search: str | None = None, limit: int = 100) -> dict:
    return await asyncio.to_thread(list_chatbot_product_labels, search, max(1, min(limit, 300)))


@app.get(f"{settings.api_prefix}/chatbot/products/rag/status")
async def chatbot_products_rag_status() -> dict:
    return await asyncio.to_thread(chatbot_catalog_rag_status)


@app.post(f"{settings.api_prefix}/chatbot/products/reindex")
async def chatbot_products_reindex(request: Request) -> dict:
    content_type = str(request.headers.get("content-type") or "").lower()
    payload = await request.json() if content_type.startswith("application/json") else {}
    return await asyncio.to_thread(
        reindex_chatbot_catalog,
        str(payload.get("product_id") or "").strip() or None,
        bool(payload.get("dirty_only", False)),
    )


@app.post(f"{settings.api_prefix}/chatbot/products/enrich-vision")
async def chatbot_products_enrich_vision(request: Request, background_tasks: BackgroundTasks) -> dict:
    content_type = str(request.headers.get("content-type") or "").lower()
    payload = await request.json() if content_type.startswith("application/json") else {}
    product_id = str(payload.get("product_id") or "").strip() or None
    limit = max(1, min(int(payload.get("limit") or 50), 200))
    if product_id and bool(payload.get("wait", False)):
        return await _enrich_product_vision_sync(product_id)
    background_tasks.add_task(_enrich_catalog_vision_and_reindex, product_id, limit)
    return {"queued": True, "product_id": product_id, "limit": limit}


@app.get(f"{settings.api_prefix}/chatbot/products/search")
async def chatbot_products_search(q: str, limit: int = 8, available_only: bool = False) -> dict:
    if not q.strip():
        raise HTTPException(status_code=400, detail="Search query is required")
    return await asyncio.to_thread(search_chatbot_catalog, q, max(1, min(limit, 30)), available_only)


@app.post(f"{settings.api_prefix}/chatbot/uploads/images")
async def chatbot_product_image_upload(request: Request, files: list[UploadFile] = File(default=[])) -> dict:
    if not files:
        raise HTTPException(status_code=400, detail="At least one image is required")
    proto = request.headers.get("X-Forwarded-Proto", request.url.scheme)
    host = request.headers.get("X-Forwarded-Host", request.headers.get("host", ""))
    uploaded: list[dict[str, str]] = []
    for file in files[:20]:
        content_type = str(file.content_type or "").lower()
        suffix = Path(file.filename or "").suffix.lower()
        if content_type not in {"image/jpeg", "image/png", "image/webp", "image/gif"} and suffix not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
            raise HTTPException(status_code=400, detail=f"Unsupported image type: {file.filename}")
        safe_suffix = suffix if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif"} else ".jpg"
        payload = await file.read(10 * 1024 * 1024 + 1)
        if len(payload) > 10 * 1024 * 1024:
            raise HTTPException(status_code=400, detail=f"Image too large: {file.filename}")
        target = CHATBOT_PRODUCT_MEDIA_DIR / f"{secrets.token_hex(16)}{safe_suffix}"
        target.write_bytes(payload)
        url = f"{proto}://{host}/public/chatbot-product-media/{target.name}"
        uploaded.append({"url": url, "filename": file.filename or target.name})
    return {"total": len(uploaded), "images": uploaded}


@app.post(f"{settings.api_prefix}/chatbot/products")
async def chatbot_product_create(request: Request, background_tasks: BackgroundTasks) -> dict:
    payload = await request.json()
    try:
        product = await asyncio.to_thread(upsert_chatbot_product, payload)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    background_tasks.add_task(_enrich_product_vision_and_reindex, product["product_id"])
    return product


@app.get(f"{settings.api_prefix}/chatbot/products/{{product_id}}")
async def chatbot_product_detail(product_id: str) -> dict:
    product = await asyncio.to_thread(get_chatbot_product, product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    return product


@app.put(f"{settings.api_prefix}/chatbot/products/{{product_id}}")
async def chatbot_product_update(product_id: str, request: Request, background_tasks: BackgroundTasks) -> dict:
    payload = await request.json()
    try:
        product = await asyncio.to_thread(upsert_chatbot_product, payload, product_id)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    background_tasks.add_task(_enrich_product_vision_and_reindex, product["product_id"])
    return product


@app.delete(f"{settings.api_prefix}/chatbot/products/{{product_id}}")
async def chatbot_product_delete(product_id: str, background_tasks: BackgroundTasks) -> dict:
    deleted = await asyncio.to_thread(delete_chatbot_product, product_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Product not found")
    background_tasks.add_task(delete_chatbot_catalog_product_vectors, product_id)
    return {"ok": True, "product_id": product_id}


@app.post(f"{settings.api_prefix}/chatbot/products/{{product_id}}/toggle")
async def chatbot_product_toggle(product_id: str, request: Request, background_tasks: BackgroundTasks) -> dict:
    payload = await request.json()
    try:
        product = await asyncio.to_thread(toggle_chatbot_product, product_id, bool(payload.get("is_active")))
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    background_tasks.add_task(_enrich_product_vision_and_reindex, product["product_id"])
    return product


@app.post(f"{settings.api_prefix}/chatbot/products/{{product_id}}/variants/{{variant_id}}/toggle")
async def chatbot_product_variant_toggle(product_id: str, variant_id: str, request: Request, background_tasks: BackgroundTasks) -> dict:
    payload = await request.json()
    try:
        product = await asyncio.to_thread(toggle_chatbot_product_variant, product_id, variant_id, bool(payload.get("is_active")))
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    background_tasks.add_task(_enrich_product_vision_and_reindex, product["product_id"])
    return product


@app.get(f"{settings.api_prefix}/chatbot/categories")
async def chatbot_product_categories(search: str | None = None) -> dict:
    return await asyncio.to_thread(list_chatbot_product_categories, search)


@app.post(f"{settings.api_prefix}/chatbot/categories")
async def chatbot_product_category_create(request: Request) -> dict:
    payload = await request.json()
    try:
        return await asyncio.to_thread(upsert_chatbot_product_category, payload)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.put(f"{settings.api_prefix}/chatbot/categories/{{category_id}}")
async def chatbot_product_category_update(category_id: str, request: Request) -> dict:
    payload = await request.json()
    try:
        return await asyncio.to_thread(upsert_chatbot_product_category, payload, category_id)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.delete(f"{settings.api_prefix}/chatbot/categories/{{category_id}}")
async def chatbot_product_category_delete(category_id: str) -> dict:
    deleted = await asyncio.to_thread(delete_chatbot_product_category, category_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Category not found")
    return {"ok": True, "category_id": category_id}


@app.get(f"{settings.api_prefix}/sites", response_model=SiteListResponse)
async def get_sites(search: str | None = None) -> SiteListResponse:
    items = await asyncio.to_thread(list_sites, search)
    return SiteListResponse(total=len(items), sites=[SiteConfigResponse(**item) for item in items])


@app.post(f"{settings.api_prefix}/sites", response_model=SiteConfigResponse)
async def post_site(request: SiteConfigCreate) -> SiteConfigResponse:
    payload = request.model_dump()
    site = await asyncio.to_thread(create_site, payload)
    log.info("site_created", site_id=site.get("site_id"), url=site.get("url"), site_name=site.get("site_name"))
    return SiteConfigResponse(**site)


@app.get(f"{settings.api_prefix}/sites/{{site_id}}", response_model=SiteConfigResponse)
async def get_site_detail(site_id: str) -> SiteConfigResponse:
    site = await asyncio.to_thread(get_site, site_id)
    if not site:
        raise HTTPException(status_code=404, detail="Site not found")
    return SiteConfigResponse(**site)


@app.put(f"{settings.api_prefix}/sites/{{site_id}}", response_model=SiteConfigResponse)
async def put_site(site_id: str, request: SiteConfigUpdate) -> SiteConfigResponse:
    site = await asyncio.to_thread(update_site, site_id, request.model_dump())
    if not site:
        raise HTTPException(status_code=404, detail="Site not found")
    log.info("site_updated", site_id=site_id, url=site.get("url"), site_name=site.get("site_name"))
    return SiteConfigResponse(**site)


@app.delete(f"{settings.api_prefix}/sites/{{site_id}}")
async def remove_site(site_id: str) -> dict:
    deleted = await asyncio.to_thread(delete_site, site_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Site not found")
    log.info("site_deleted", site_id=site_id)
    return {"site_id": site_id, "deleted": True}


@app.post(f"{settings.api_prefix}/sites/{{site_id}}/test", response_model=SiteTestResponse)
async def test_site(site_id: str) -> SiteTestResponse:
    result = await asyncio.to_thread(test_site_connection, site_id)
    if not result:
        raise HTTPException(status_code=404, detail="Site not found")
    log.info("site_tested", site_id=site_id, status=result.get("status"))
    return SiteTestResponse(**result)


@app.get(f"{settings.api_prefix}/jobs", response_model=JobListResponse)
async def get_jobs(status: str | None = None, priority: str | None = None, search: str | None = None, limit: int = 50) -> JobListResponse:
    jobs = await asyncio.to_thread(list_jobs, status, priority, search, max(1, min(limit, 200)))
    items = _job_list_items(jobs)
    return JobListResponse(total=len(items), jobs=items)


def _realtime_redis_client() -> Redis | None:
    if Redis is None:
        return None
    try:
        client = Redis.from_url(settings.redis_url, decode_responses=True)
        client.ping()
        return client
    except Exception:
        return None


def _stats_payload() -> dict:
    snapshot = stats_snapshot()
    total = snapshot["total"]
    completed = snapshot["completed"]
    duplicate = snapshot["duplicate"]
    return {
        "total_processed": total,
        "success_rate": round((completed / total), 2) if total else 0.0,
        "avg_processing_time_sec": round(snapshot["avg_time"], 2),
        "avg_qa_score": round(snapshot["avg_score"], 2),
        "avg_cost_per_article_usd": round(snapshot["avg_cost"], 4),
        "dlq_size": snapshot["dlq_size"],
        "duplicate_rate": round((duplicate / total), 2) if total else 0.0,
    }


async def _jobs_realtime_snapshot(limit: int) -> dict:
    jobs = await asyncio.to_thread(list_jobs, None, None, None, max(1, min(limit, 200)))
    items = [item.model_dump() for item in _job_list_items(jobs)]
    stats = await asyncio.to_thread(_stats_payload)
    return {"type": "jobs.snapshot", "channel": "jobs", "jobs": items, "stats": stats}


async def _job_realtime_snapshot(job_id: str) -> dict:
    state = await asyncio.to_thread(get_job, job_id)
    return {"type": "job.snapshot", "channel": f"job:{job_id}", "job_id": job_id, "job": state}


@app.websocket(f"{settings.api_prefix}/realtime/ws")
async def realtime_ws(websocket: WebSocket) -> None:
    payload = verify_session_token(websocket.cookies.get(settings.auth_cookie_name))
    if not payload:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    redis_conn = _realtime_redis_client()
    pubsub = redis_conn.pubsub(ignore_subscribe_messages=True) if redis_conn is not None else None
    subscribed_channels: set[str] = set()
    limit = 50
    try:
        await websocket.send_text(json.dumps({"type": "realtime.ready"}, ensure_ascii=False, default=str))
        while True:
            try:
                raw_message = await asyncio.wait_for(websocket.receive_text(), timeout=0.2)
                try:
                    client_message = json.loads(raw_message)
                except json.JSONDecodeError:
                    client_message = {}
                message_type = str(client_message.get("type") or "")
                if message_type == "ping":
                    await websocket.send_text(json.dumps({"type": "pong"}, ensure_ascii=False, default=str))
                    continue
                if message_type == "subscribe":
                    limit = max(1, min(int(client_message.get("limit") or limit), 200))
                    requested = {str(channel) for channel in (client_message.get("channels") or [])}
                    for channel in requested:
                        if channel in subscribed_channels:
                            continue
                        subscribed_channels.add(channel)
                        if pubsub is not None:
                            await asyncio.to_thread(pubsub.subscribe, f"content_forge:realtime:{channel}")
                    if "jobs" in requested:
                        await websocket.send_text(json.dumps(await _jobs_realtime_snapshot(limit), ensure_ascii=False, default=str))
                    for channel in requested:
                        if channel.startswith("job:"):
                            await websocket.send_text(json.dumps(await _job_realtime_snapshot(channel.removeprefix("job:")), ensure_ascii=False, default=str))
            except asyncio.TimeoutError:
                pass

            if pubsub is None or not subscribed_channels:
                continue

            message = await asyncio.to_thread(pubsub.get_message, timeout=0.2)
            if not message or message.get("type") != "message":
                continue
            try:
                event = json.loads(message.get("data") or "{}")
            except json.JSONDecodeError:
                continue
            channel = str(event.get("channel") or "")
            if channel == "jobs" and channel in subscribed_channels:
                await websocket.send_text(json.dumps(await _jobs_realtime_snapshot(limit), ensure_ascii=False, default=str))
            elif channel.startswith("job:") and channel in subscribed_channels:
                await websocket.send_text(json.dumps(await _job_realtime_snapshot(channel.removeprefix("job:")), ensure_ascii=False, default=str))
            elif channel in subscribed_channels:
                await websocket.send_text(json.dumps(event, ensure_ascii=False, default=str))
    except WebSocketDisconnect:
        return
    finally:
        if pubsub is not None:
            with suppress(Exception):
                pubsub.close()


@app.websocket(f"{settings.api_prefix}/jobs/ws")
async def jobs_ws(websocket: WebSocket, limit: int = 50) -> None:
    payload = verify_session_token(websocket.cookies.get(settings.auth_cookie_name))
    if not payload:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    last_version = await asyncio.to_thread(get_jobs_version)
    try:
        jobs = await asyncio.to_thread(list_jobs, None, None, None, max(1, min(limit, 200)))
        items = [item.model_dump() for item in _job_list_items(jobs)]
        stats = await asyncio.to_thread(stats_snapshot)
        await websocket.send_text(json.dumps({"jobs": items, "stats": stats}, ensure_ascii=False))
        while True:
            last_version = await asyncio.to_thread(wait_for_jobs_version, last_version, 20.0)
            jobs = await asyncio.to_thread(list_jobs, None, None, None, max(1, min(limit, 200)))
            items = [item.model_dump() for item in _job_list_items(jobs)]
            stats = await asyncio.to_thread(stats_snapshot)
            await websocket.send_text(json.dumps({"jobs": items, "stats": stats}, ensure_ascii=False))
    except WebSocketDisconnect:
        return


@app.get(f"{settings.api_prefix}/job/{{job_id}}", response_model=JobProgressResponse)
async def get_job_status(job_id: str) -> JobProgressResponse:
    state = get_job(job_id)
    if not state:
        raise HTTPException(status_code=404, detail="Job not found")

    steps = [
        "deduplicator",
        "fetcher",
        "extractor",
        "knowledge",
        "enricher",
        "planner",
        "image_selector",
        "media_uploader",
        "writer",
        "humanizer",
        "internal_linker",
        "qa",
        "seo_adjuster",
        "publisher",
    ]
    current_step = state.get("current_step")
    progress = int(((steps.index(current_step) + 1) / len(steps)) * 100) if current_step in steps else 0
    dlq = state.get("status") == "failed"
    return JobProgressResponse(
        job_id=job_id,
        status=state.get("status", "pending"),
        current_step=current_step,
        progress_percent=progress,
        woo_post_id=state.get("woo_post_id"),
        woo_link=state.get("woo_link"),
        qa_score=state.get("qa_result", {}).get("overall_score"),
        processing_time_sec=state.get("metrics", {}).get("processing_time_sec"),
        tokens_used=state.get("metrics", {}).get("total_tokens_used"),
        estimated_cost_usd=state.get("metrics", {}).get("estimated_cost_usd"),
        error=state.get("error"),
        dlq=dlq,
        dlq_review_url=f"{settings.api_prefix}/dlq/{job_id}" if dlq else None,
    )


@app.get(f"{settings.api_prefix}/job/{{job_id}}/detail")
async def get_job_detail(job_id: str) -> dict:
    state = get_job(job_id)
    if not state:
        raise HTTPException(status_code=404, detail="Job not found")
    return state


@app.get(f"{settings.api_prefix}/dlq")
async def get_dlq() -> dict:
    jobs = list_dlq()
    dlq_size.set(len(jobs))
    return {
        "total": len(jobs),
        "jobs": [
            {
                "job_id": item["job_id"],
                "url": item["url"],
                "failed_at": item["failed_at"].isoformat(),
                "reason": item["reason"],
                "qa_score": item.get("qa_score", 0.0),
                "review_url": f"{settings.api_prefix}/dlq/{item['job_id']}",
            }
            for item in jobs
        ],
    }


@app.get(f"{settings.api_prefix}/dlq/{{job_id}}")
async def get_dlq_job(job_id: str) -> dict:
    item = get_dlq_entry(job_id)
    if not item:
        raise HTTPException(status_code=404, detail="DLQ job not found")
    return item


@app.post(f"{settings.api_prefix}/dlq/{{job_id}}/retry")
async def retry_dlq(job_id: str) -> dict:
    result = await asyncio.to_thread(retry_from_dlq, job_id)
    if not result:
        raise HTTPException(status_code=404, detail="DLQ job not found")
    log.info("dlq_retry_requested", job_id=job_id, status=result.get("status"))
    return {"job_id": job_id, "status": result.get("status")}


@app.post(f"{settings.api_prefix}/dlq/{{job_id}}/publish-anyway")
async def publish_dlq_anyway(job_id: str) -> dict:
    result = await asyncio.to_thread(publish_anyway, job_id)
    if not result:
        raise HTTPException(status_code=404, detail="DLQ job not found")
    log.info(
        "dlq_publish_anyway",
        job_id=job_id,
        status=result.get("status"),
        woo_post_id=result.get("woo_post_id"),
    )
    return {
        "job_id": job_id,
        "status": result.get("status"),
        "woo_post_id": result.get("woo_post_id"),
        "woo_link": result.get("woo_link"),
        "forced_publish": True,
    }


@app.delete(f"{settings.api_prefix}/dlq/{{job_id}}")
async def delete_dlq(job_id: str) -> dict:
    deleted = delete_dlq_entry(job_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="DLQ job not found")
    return {"job_id": job_id, "deleted": True}


@app.get(f"{settings.api_prefix}/stats", response_model=StatsResponse)
async def get_stats() -> StatsResponse:
    return StatsResponse(**_stats_payload())


@app.get(f"{settings.api_prefix}/cliproxy/quota")
async def cliproxy_quota(force: int = Query(0, ge=0, le=1)) -> dict:
    quota_url = os.getenv("CLIPROXY_QUOTA_URL", "http://127.0.0.1:8320").rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            quota_response = await client.get(f"{quota_url}/quota", params={"force": int(bool(force))})
            quota_response.raise_for_status()
            quota_payload = quota_response.json()
            best_payload = {}
            with suppress(Exception):
                best_response = await client.get(f"{quota_url}/quota/best")
                if best_response.status_code == 200:
                    best_payload = best_response.json()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=502, detail=f"Cliproxy quota API failed: {exc.response.text[:300]}") from exc
    except Exception as exc:
        return {
            "available": False,
            "error": str(exc),
            "summary": {
                "total": 0,
                "healthy": 0,
                "safe_to_use": False,
                "stale": True,
                "avg_remaining_5h": 0,
                "avg_remaining_7d": 0,
            },
            "accounts": [],
            "best": {},
        }
    return {
        "available": True,
        "summary": quota_payload.get("summary") or {},
        "accounts": quota_payload.get("accounts") or [],
        "best": best_payload,
        "fetched_at": quota_payload.get("fetched_at", ""),
    }


@app.get("/")
async def root() -> FileResponse:
    with suppress(Exception):
        start_metrics_server_once(settings.metrics_port)
    return FileResponse(
        UI_DIR / "content_forge.html",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )
