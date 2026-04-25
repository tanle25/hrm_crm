from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.api_tokens import create_api_token, delete_api_token, list_api_tokens, verify_api_token
from app.auth import authenticate_credentials, create_session_token, verify_session_token
from app.config import get_settings
from app.dlq import publish_anyway
from app.facebook_pages import (
    connect_facebook_pages,
    debug_facebook_messages,
    facebook_aggregate_stats,
    facebook_comments,
    facebook_conversations,
    facebook_posts,
    list_facebook_pages,
    process_facebook_webhook,
    send_facebook_message,
    sync_facebook_comments,
    sync_facebook_aggregate_stats,
    sync_facebook_conversations,
    sync_facebook_posts,
    verify_facebook_webhook_signature,
)
from app.graph import retry_from_dlq, run_pipeline_async
from app.job_store import delete_dlq_entry, get_dlq_entry, get_job, get_jobs_version, list_dlq, list_jobs, stats_snapshot, wait_for_jobs_version
from app.logging import get_logger
from app.metrics import dlq_size, jobs_submitted, start_metrics_server_once
from app.postgres import init_schema as init_postgres_schema, migrate_local_state as migrate_local_postgres_state
from app.queue import create_job_id, enqueue_job, enqueue_saved_state, init_job_state, queue_is_full, update_job
from app.rag_categories import create_category, list_categories
from app.rag import delete_source_documents, get_source_documents, get_taxonomy_summary, ingest_url, list_rag_sources, search_knowledge
from app.shopee import get_shopee_product, import_legacy_sample, list_shopee_products, upsert_shopee_product
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
    RAGIngestResponse,
    RAGSearchResponse,
    RAGSourceListResponse,
    RAGSourceResponse,
    RAGTaxonomyResponse,
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

if UI_DIR.exists():
    app.mount("/ui", StaticFiles(directory=UI_DIR), name="ui")


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
async def get_facebook_conversations(limit: int = 50) -> FacebookConversationListResponse:
    result = await asyncio.to_thread(facebook_conversations, max(1, min(limit, 100)))
    return FacebookConversationListResponse(**result)


@app.post(f"{settings.api_prefix}/facebook/conversations/sync", response_model=FacebookConversationListResponse)
async def sync_facebook_conversations_endpoint(limit: int = 50) -> FacebookConversationListResponse:
    result = await asyncio.to_thread(sync_facebook_conversations, max(1, min(limit, 100)))
    return FacebookConversationListResponse(**result)


@app.post(f"{settings.api_prefix}/facebook/messages/send", response_model=FacebookMessageSendResponse)
async def send_facebook_message_endpoint(request: FacebookMessageSendRequest) -> FacebookMessageSendResponse:
    try:
        result = await asyncio.to_thread(send_facebook_message, request.conversation_id, request.message)
    except httpx.HTTPStatusError as error:
        detail = error.response.text[:500] if error.response is not None else str(error)
        raise HTTPException(status_code=400, detail=f"Facebook message send failed: {detail}") from error
    except RuntimeError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return FacebookMessageSendResponse(**result)


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


@app.post(f"{settings.api_prefix}/rag/ingest", response_model=RAGIngestResponse)
async def rag_ingest(request: RAGIngestRequest) -> RAGIngestResponse:
    result = await asyncio.to_thread(
        ingest_url,
        str(request.url),
        request.manual_categories,
        request.manual_tags,
        request.note,
        request.force_reingest,
    )
    log.info(
        "rag_ingested",
        source_url=str(request.url),
        status=result.get("status"),
        documents_count=result.get("documents_count", 0),
        categories=result.get("categories", []),
    )
    return RAGIngestResponse(**result)


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


@app.post(f"{settings.api_prefix}/shopee/products", response_model=ShopeeProductDetailResponse)
async def shopee_product_upsert(request: ShopeeUpsertRequest, http_request: Request) -> ShopeeProductDetailResponse:
    if not _extension_authorized(http_request):
        raise HTTPException(status_code=401, detail="Valid session or API token required")
    try:
        payload = await asyncio.to_thread(upsert_shopee_product, request.product)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    log.info("shopee_product_upserted", item_id=payload.get("item_id"), title=(payload.get("normalized") or {}).get("product_title"))
    return ShopeeProductDetailResponse(
        item_id=str(payload.get("item_id") or ""),
        raw=payload.get("raw") or {},
        normalized=payload.get("normalized") or {},
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


@app.get("/")
async def root() -> FileResponse:
    with suppress(Exception):
        start_metrics_server_once(settings.metrics_port)
    return FileResponse(UI_DIR / "content_forge.html")
