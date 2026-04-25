from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, HttpUrl


class SubmitRequest(BaseModel):
    url: HttpUrl
    site_id: str = ""
    content_mode: Literal["shared", "per-site"] = "shared"
    woo_category_id: int
    focus_keyword: Optional[str] = None
    priority: Literal["normal", "high"] = "normal"
    publish_status: Literal["draft", "publish"] = "draft"


class LoginRequest(BaseModel):
    username: str
    password: str
    remember: bool = False


class LoginResponse(BaseModel):
    authenticated: bool
    username: str = ""
    redirect_url: str = "/"


class AuthMeResponse(BaseModel):
    authenticated: bool
    username: str = ""


class ApiTokenCreateRequest(BaseModel):
    name: str


class ApiTokenListItem(BaseModel):
    token_id: str
    name: str
    token_prefix: str = ""
    created_at: str = ""
    updated_at: str = ""
    last_used_at: str = ""
    status: str = "active"


class ApiTokenCreateResponse(BaseModel):
    token: str
    token_item: ApiTokenListItem


class ApiTokenListResponse(BaseModel):
    total: int
    tokens: List[ApiTokenListItem] = Field(default_factory=list)


class FacebookConnectRequest(BaseModel):
    short_lived_token: str


class FacebookPageItem(BaseModel):
    page_id: str
    name: str = ""
    category: str = ""
    picture_url: str = ""
    cover_url: str = ""
    tasks: List[str] = Field(default_factory=list)
    status: str = "connected"
    token_prefix: str = ""
    connected_at: str = ""
    updated_at: str = ""
    expires_in: Optional[int] = None


class FacebookPageListResponse(BaseModel):
    total: int
    pages: List[FacebookPageItem] = Field(default_factory=list)


class FacebookConnectResponse(BaseModel):
    status: str
    total: int
    pages: List[FacebookPageItem] = Field(default_factory=list)
    batch_id: str = ""
    expires_in: Optional[int] = None


class FacebookStatsResponse(BaseModel):
    days: int
    page_count: int = 0
    totals: Dict[str, Any] = Field(default_factory=dict)
    series: List[Dict[str, Any]] = Field(default_factory=list)
    top_posts: List[Dict[str, Any]] = Field(default_factory=list)
    best_posting_time: str = ""
    content_performance: List[Dict[str, Any]] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    cached: bool = False


class SubmitResponse(BaseModel):
    job_id: str
    status: str
    queue: str
    estimated_wait_sec: int
    check_url: str


class SubmitBatchRequest(BaseModel):
    urls: List[HttpUrl]
    site_ids: List[str]
    content_mode: Literal["shared", "per-site"] = "shared"
    woo_category_id: int
    focus_keyword: Optional[str] = None
    priority: Literal["normal", "high"] = "normal"
    publish_status: Literal["draft", "publish"] = "draft"


class SubmitBatchResponse(BaseModel):
    batch_id: str
    status: str
    total_jobs: int
    master_job_ids: List[str] = Field(default_factory=list)
    child_job_ids: List[str] = Field(default_factory=list)


class ShopeeEnqueueRequest(BaseModel):
    site_ids: List[str]
    content_mode: Literal["shared", "per-site"] = "shared"
    publish_status: Literal["draft", "publish"] = "draft"
    woo_category_id: int = 1
    priority: Literal["normal", "high"] = "normal"


class ShopeeUpsertRequest(BaseModel):
    product: Dict[str, Any]


class ShopeeProductListItem(BaseModel):
    item_id: str
    shop_id: str = ""
    title: str
    type: Literal["simple", "variable"] = "simple"
    regular_price: int = 0
    sale_price: Optional[int] = None
    variant_count: int = 0
    image_count: int = 0
    image_url: str = ""
    url: str = ""
    updated_at: str = ""


class ShopeeProductListResponse(BaseModel):
    source_url: str = ""
    category_label: str = ""
    total: int = 0
    items: List[ShopeeProductListItem] = Field(default_factory=list)


class ShopeeProductDetailResponse(BaseModel):
    item_id: str
    raw: Dict[str, Any] = Field(default_factory=dict)
    normalized: Dict[str, Any] = Field(default_factory=dict)


class SiteConfigBase(BaseModel):
    url: HttpUrl
    site_name: str
    topic: str = ""
    primary_color: str = "#22c55e"
    consumer_key: str = ""
    consumer_secret: str = ""
    username: str = ""
    app_password: str = ""


class SiteConfigCreate(SiteConfigBase):
    pass


class SiteConfigUpdate(SiteConfigBase):
    pass


class SiteConfigResponse(SiteConfigBase):
    site_id: str
    created_at: str = ""
    updated_at: str = ""
    last_test_status: str = "untested"
    last_test_message: str = ""
    last_tested_at: str = ""


class SiteListResponse(BaseModel):
    total: int
    sites: List[SiteConfigResponse] = Field(default_factory=list)


class SiteTestResponse(BaseModel):
    site_id: str
    status: str
    message: str
    details: Dict[str, Any] = Field(default_factory=dict)


class RAGIngestRequest(BaseModel):
    url: HttpUrl
    manual_categories: List[str] = Field(default_factory=list)
    manual_tags: List[str] = Field(default_factory=list)
    note: Optional[str] = None
    force_reingest: bool = True


class RAGIngestResponse(BaseModel):
    status: str
    source_id: str
    source_url: str
    title: Optional[str] = None
    source_type: Optional[str] = None
    product_kind: Optional[str] = None
    primary_category: str = ""
    categories: List[str] = Field(default_factory=list)
    subcategories: List[str] = Field(default_factory=list)
    knowledge_types: List[str] = Field(default_factory=list)
    usage_intents: List[str] = Field(default_factory=list)
    tags: List[str] = Field(default_factory=list)
    documents_count: int = 0
    document_ids: List[str] = Field(default_factory=list)
    preview_chunks: List[Dict[str, Any]] = Field(default_factory=list)


class RAGSearchResponse(BaseModel):
    query: str
    results: List[Dict[str, Any]] = Field(default_factory=list)


class RAGCategoryCreate(BaseModel):
    name: str


class RAGCategoryListResponse(BaseModel):
    total: int
    categories: List[str] = Field(default_factory=list)


class RAGSourceResponse(BaseModel):
    source_id: str
    source_url: str
    documents_count: int = 0
    documents: List[Dict[str, Any]] = Field(default_factory=list)


class RAGTaxonomyResponse(BaseModel):
    primary_category: str = ""
    available_primary_categories: List[str] = Field(default_factory=list)
    subcategories: List[str] = Field(default_factory=list)
    knowledge_types: List[str] = Field(default_factory=list)
    usage_intents: List[str] = Field(default_factory=list)
    tags: List[str] = Field(default_factory=list)
    source_count: int = 0
    document_count: int = 0
    source_urls: List[str] = Field(default_factory=list)


class JobListItem(BaseModel):
    job_id: str
    url: str = ""
    title: str = ""
    site_id: str = ""
    site_name: str = ""
    content_mode: str = "shared"
    batch_id: str = ""
    parent_job_id: str = ""
    workflow_role: str = "standard"
    priority: str = "normal"
    status: str = "pending"
    current_step: str = ""
    progress_percent: int = 0
    woo_post_id: Optional[int] = None
    woo_link: Optional[str] = None
    qa_score: Optional[float] = None
    processing_time_sec: Optional[float] = None
    estimated_cost_usd: Optional[float] = None
    publish_status: str = "draft"
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    error: Optional[str] = None
    dlq: bool = False


class JobListResponse(BaseModel):
    total: int
    jobs: List[JobListItem] = Field(default_factory=list)


class RAGSourceListItem(BaseModel):
    source_id: str
    source_url: str
    title: str = ""
    primary_category: str = ""
    source_type: str = ""
    product_kind: str = ""
    subcategories: List[str] = Field(default_factory=list)
    knowledge_types: List[str] = Field(default_factory=list)
    usage_intents: List[str] = Field(default_factory=list)
    tags: List[str] = Field(default_factory=list)
    document_count: int = 0
    last_ingested_at: str = ""


class RAGSourceListResponse(BaseModel):
    total: int
    sources: List[RAGSourceListItem] = Field(default_factory=list)


class JobProgressResponse(BaseModel):
    job_id: str
    status: str
    current_step: Optional[str] = None
    progress_percent: int = 0
    woo_post_id: Optional[int] = None
    woo_link: Optional[str] = None
    qa_score: Optional[float] = None
    processing_time_sec: Optional[float] = None
    tokens_used: Optional[int] = None
    estimated_cost_usd: Optional[float] = None
    error: Optional[str] = None
    dlq: bool = False
    dlq_review_url: Optional[str] = None


class DedupResult(BaseModel):
    is_duplicate: bool = False
    url_hash: str = ""
    existing_post_id: Optional[int] = None


class FetchMetadata(BaseModel):
    author: str = ""
    publish_date: str = ""
    url: str = ""
    sitename: str = ""
    language: str = "vi"


class FetchResult(BaseModel):
    title: str = ""
    clean_content: str = ""
    html: str = ""
    metadata: FetchMetadata = Field(default_factory=FetchMetadata)


class ExtractedEntities(BaseModel):
    people: List[str] = Field(default_factory=list)
    places: List[str] = Field(default_factory=list)
    organizations: List[str] = Field(default_factory=list)
    products: List[str] = Field(default_factory=list)


class FaqItem(BaseModel):
    question: str
    answer: str


class ExtractedData(BaseModel):
    key_points: List[str] = Field(default_factory=list)
    important_facts: List[str] = Field(default_factory=list)
    entities: ExtractedEntities = Field(default_factory=ExtractedEntities)
    original_intent: str = "informational"
    tone: str = "professional"
    faq_items: List[FaqItem] = Field(default_factory=list)
    steps: List[str] = Field(default_factory=list)


class KnowledgeFact(BaseModel):
    fact: str
    source: str
    source_url: str = ""
    knowledge_type: str = ""
    subcategories: List[str] = Field(default_factory=list)
    usage_intent: str = ""
    integration_hint: str = ""
    reason: str = ""


class AdditionalSource(BaseModel):
    url: str
    summary: str


class PlanData(BaseModel):
    title: str = ""
    meta_title: str = ""
    article_type: str = "Comprehensive Guide"
    target_intent: str = "informational"
    tone: str = "professional"
    seo_geo_keywords: List[str] = Field(default_factory=list)
    tags: List[str] = Field(default_factory=list)
    focus_keyword: str = ""
    meta_description: str = ""
    outline: Dict[str, Any] = Field(default_factory=dict)
    e_e_a_t_elements: Dict[str, Any] = Field(default_factory=dict)
    schema_type: str = "Article"


class DraftContent(BaseModel):
    html: str = ""


class ImageData(BaseModel):
    url: str = ""
    media_id: int = 0
    alt_text: str = ""
    photographer: str = ""
    gallery: List[str] = Field(default_factory=list)
    unsplash_link: str = ""


class QAScores(BaseModel):
    plagiarism_similarity: float = 0.0
    eeat_score: int = 0
    geo_structure_score: int = 0
    readability_score: int = 0
    rank_math_readiness: int = 0


class QAFeedback(BaseModel):
    strengths: List[str] = Field(default_factory=list)
    improvements: List[str] = Field(default_factory=list)
    retry_target: str = "humanizer"
    issue_category: Optional[str] = None


class QAResult(BaseModel):
    scores: QAScores = Field(default_factory=QAScores)
    overall_score: float = 0.0
    pass_: bool = Field(default=False, alias="pass")
    feedback: QAFeedback = Field(default_factory=QAFeedback)
    retry_count: int = 0

    model_config = {"populate_by_name": True}


class FinalArticle(BaseModel):
    title: str = ""
    html: str = ""
    schema_data: Any = Field(default_factory=dict, alias="schema")

    model_config = {"populate_by_name": True}


class MetricsData(BaseModel):
    total_tokens_used: int = 0
    estimated_cost_usd: float = 0.0
    processing_time_sec: float = 0.0
    agents_used: List[str] = Field(default_factory=list)


class PipelineState(BaseModel):
    url: str
    site_id: str = ""
    content_mode: Literal["shared", "per-site"] = "shared"
    site_profile: Dict[str, Any] = Field(default_factory=dict)
    source_origin: str = ""
    source_seed: Dict[str, Any] = Field(default_factory=dict)
    priority: Literal["normal", "high"] = "normal"
    woo_category_id: int
    focus_keyword_override: Optional[str] = None
    publish_status: Literal["draft", "publish"] = "draft"
    dedup_result: DedupResult = Field(default_factory=DedupResult)
    fetch_result: FetchResult = Field(default_factory=FetchResult)
    extracted: ExtractedData = Field(default_factory=ExtractedData)
    knowledge_facts: List[KnowledgeFact] = Field(default_factory=list)
    additional_sources: List[AdditionalSource] = Field(default_factory=list)
    plan: PlanData = Field(default_factory=PlanData)
    draft: DraftContent = Field(default_factory=DraftContent)
    humanized: DraftContent = Field(default_factory=DraftContent)
    linked_html: str = ""
    image_data: ImageData = Field(default_factory=ImageData)
    qa_result: QAResult = Field(default_factory=QAResult)
    final_article: FinalArticle = Field(default_factory=FinalArticle)
    woo_post_id: Optional[int] = None
    woo_link: Optional[str] = None
    metrics: MetricsData = Field(default_factory=MetricsData)
    error: Optional[str] = None
    status: Literal["pending", "processing", "completed", "failed", "duplicate"] = "pending"
    current_step: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class DLQEntry(BaseModel):
    job_id: str
    url: str
    failed_at: datetime
    reason: str
    qa_score: float = 0.0
    state: Dict[str, Any] = Field(default_factory=dict)


class DLQListResponse(BaseModel):
    total: int
    jobs: List[Dict[str, Any]]


class StatsResponse(BaseModel):
    total_processed: int
    success_rate: float
    avg_processing_time_sec: float
    avg_qa_score: float
    avg_cost_per_article_usd: float
    dlq_size: int
    duplicate_rate: float
