from __future__ import annotations

import logging
import uuid
import tempfile
import os
import base64
import hmac
import shutil
import time
from contextlib import asynccontextmanager, nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, File, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator

from hot_sources.service import HotTrendService
from hot_sources.classifier import CATEGORIES
from modules.config_store import load_settings
from modules.app_paths import data_root, exports_root, model_test_root, research_root, tasks_root

# ── API 日志初始化：写入 data_root/logs/api.log ──
# 桌面环境（desktop_host.py 设置 HOTSPOT_DESKTOP=1）下初始化完整日志；
# 测试/手动启动时不设置以避免干扰 pytest。
if os.environ.get("HOTSPOT_DESKTOP") == "1":
    _log_dir = data_root() / "logs"
    _log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        handlers=[
            logging.FileHandler(_log_dir / "api.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
else:
    logging.getLogger().addHandler(logging.NullHandler())
_logger = logging.getLogger(__name__)
from modules.database import get_store
from modules.security import redact_sensitive_text, sanitize_sensitive_data
from generation.executor import get_executor
from generation.image_budget import count_body_chinese_chars, recommended_word_count
from generation.batch_executor import BatchExecutor, get_batch_executor
from generation.angle_planner import plan_angles
from generation.inline_images import get_inline_images
from generation.selected_images import generate_selected_images
from generation.editor import discard_article_draft, get_article, restore_article_version, save_article, save_article_draft
from export.docx_exporter import ARTICLE_NOT_READY_MESSAGE, export_article
from export.zip_exporter import export_article_bundle, export_batch_bundle, safe_filename
from export.layout_pipeline import ensure_article_layout, prepare_article_layout
from generation.recovery import recover_interrupted_tasks
from generation.single_task import prepare_generation_state
from modules.generation_store import generation_task_dir, load_generation_task, save_generation_task
from modules.device_identity import device_status
from modules.license_service import check_license, import_license, require_generation_license
from modules.config_store import save_settings
from providers.errors import user_facing_error_message
from research.service import ResearchService, load_research_bundle
from providers.contracts import ModelTestResult
from providers.errors import is_retryable_error
from providers.image_provider import OpenAIImageProvider
from providers.model_discovery import discover_models
from providers.text_provider import OpenAITextProvider, ProviderError


app = FastAPI(title="热点图文工作台 API", version="0.1.0", docs_url=None, redoc_url=None, openapi_url=None)
store = get_store()
service = HotTrendService(load_settings(), store=store)
executor = get_executor()
batch_executor = get_batch_executor()

EXPORTABLE_ARTICLE_STATUSES = {"completed", "completed_with_warning", "warning", "partial_success", "review_required"}

# ── R1.2.1 临时防重复（进程内弱幂等，非可靠持久幂等）──
# 10s内同 client_request_id 返回已有批次。
# 注意：进程重启后丢失；client_request_id 为分钟级粒度（%Y%m%d%H%M）。
_BATCH_DEDUP_STORE: dict[str, dict[str, Any]] = {}

def _cleanup_stale_dedup_entries(now_ts: float, max_age: float = 15.0) -> None:
    """移除超过 max_age 秒的旧幂等记录，防止内存泄漏。"""
    stale = [k for k, v in _BATCH_DEDUP_STORE.items() if now_ts - float(v.get("_ts") or 0) > max_age]
    for k in stale:
        _BATCH_DEDUP_STORE.pop(k, None)


@asynccontextmanager
async def app_lifespan(application: FastAPI):
    recover_interrupted_tasks(store=store, executor=executor)
    batch_executor.recover_batches()
    yield


app.router.lifespan_context = app_lifespan


@app.middleware("http")
async def local_api_auth_middleware(request: Request, call_next):
    if not request.url.path.startswith("/api/"):
        return await call_next(request)
    host = str(request.headers.get("host") or "").split(":")[0].lower()
    if host and host not in {"127.0.0.1", "localhost", "testserver"}:
        return _error("LOCAL_API_HOST_FORBIDDEN", "本地接口只允许本机访问", retryable=False, status_code=403)
    expected_token = os.environ.get("HOTSPOT_LOCAL_API_TOKEN", "").strip()
    allow_test_bypass = os.environ.get("HOTSPOT_ALLOW_" + "UNAUTHENTICATED_TEST_API") == "1"
    if not expected_token and allow_test_bypass:
        return await call_next(request)
    if not _valid_local_api_token(expected_token):
        return _error("LOCAL_API_AUTH_REQUIRED", "本地接口授权未初始化，请通过启动器打开软件", retryable=True, status_code=503)
    if not hmac.compare_digest(str(request.headers.get("X-Hotspot-Token") or ""), expected_token):
        return _error("LOCAL_API_AUTH_REQUIRED", "本地接口需要启动器授权", retryable=False, status_code=401)
    return await call_next(request)


def _valid_local_api_token(value: str) -> bool:
    if not value:
        return False
    if len(value.encode("utf-8")) >= 32:
        return True
    try:
        return len(base64.b64decode(value, validate=True)) >= 32
    except Exception:
        return False


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return _error("VALIDATION_ERROR", "请求参数校验失败", exc.errors(), retryable=False, status_code=422)


class ManualTopicRequest(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    summary: str = Field(default="", max_length=5000)
    reference_url: str = Field(default="", max_length=2048)


class UrlFetchRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)


class SelectTopicsRequest(BaseModel):
    topic_ids: list[str] = Field(min_length=1, max_length=5)


class GenerationOptions(BaseModel):
    article_type: Literal["热点资讯", "社会民生", "观点评论", "科普解读"] = "热点资讯"
    style: Literal["客观通俗", "犀利评论", "专业分析"] = "客观通俗"
    image_style: Literal["动漫化新闻插画", "二维国漫新闻插画", "国风 3D 新闻插画"] = "动漫化新闻插画"
    word_count: Literal[1200, 1500, 1600] = 1200
    image_plan_mode: Literal["none", "economy", "standard"] | None = None
    image_call_budget_per_article: int | None = Field(default=None, ge=0, le=20)
    image_call_budget_per_batch: int | None = Field(default=None, ge=0, le=100)
    image_retry_limit: int | None = Field(default=None, ge=0, le=1)
    image_unit_price: float | None = Field(default=None, ge=0)
    confirm_paid: bool = Field(default=False, exclude=True)

    @field_validator("word_count", mode="before")
    @classmethod
    def _migrate_legacy_word_count(cls, value: Any) -> int:
        return recommended_word_count(value)


class CreateTaskRequest(BaseModel):
    task_name: str = Field(default="未命名热点任务", max_length=100)
    mode: Literal["multi_topic", "single_topic_multi_angle"]
    topic_ids: list[str] = Field(min_length=1, max_length=5)
    article_count: int = Field(ge=1, le=5)
    generation_options: GenerationOptions = Field(default_factory=GenerationOptions)


class CreateBatchRequest(BaseModel):
    batch_name: str = Field(min_length=1, max_length=100)
    mode: Literal["multi_topic", "single_topic_multi_angle"] = "multi_topic"
    topic_ids: list[str] | None = Field(default=None, min_length=1, max_length=5)
    topics: list[dict[str, Any]] | None = Field(default=None, min_length=1, max_length=5)
    article_count: int = Field(default=1, ge=1, le=5)
    angles: list[str] | None = Field(default=None, min_length=1, max_length=5)
    generation_options: GenerationOptions = Field(default_factory=GenerationOptions)
    concurrency: int = Field(default=2, ge=1, le=5)
    client_request_id: str | None = Field(default=None, max_length=64, description="幂等键，同一ID重复提交返回已有批次")


class BasketRequest(BaseModel):
    topic_ids: list[str] = Field(min_length=1, max_length=5)


class DeleteRequest(BaseModel):
    confirm: bool = False
    delete_exports: bool = False


class BasketOrderRequest(BaseModel):
    topic_ids: list[str] = Field(default_factory=list, max_length=5)


class ModelTestRequest(BaseModel):
    timeout_override: int | None = Field(default=None, ge=3, le=300)
    confirm_paid_test: bool = False
    profile: dict[str, Any] | None = None


class ModelDiscoverRequest(BaseModel):
    profile: dict[str, Any] | None = None
    profile_kind: Literal["text", "image"] = "text"
    use_for_both: bool = False


class ResearchRequest(BaseModel):
    reference_urls: list[str] = Field(default_factory=list, max_length=10)
    supplemental_text: str = Field(default="", max_length=12000)


class InlineImageRetryRequest(BaseModel):
    image_ids: list[str] = Field(default_factory=list, max_length=4)
    confirm_paid: bool = False


class ImageSelectionRequest(BaseModel):
    confirm_paid: bool = False
    include_cover: bool = True
    inline_count: int = Field(default=0, ge=0, le=1)


class ArticleEditRequest(BaseModel):
    title: str | None = Field(default=None, max_length=300)
    intro: str | None = Field(default=None, max_length=5000)
    summary: str | None = Field(default=None, max_length=5000)
    sections: list[dict[str, Any]] | None = Field(default=None, max_length=30)


class ArticleVersionRequest(BaseModel):
    version_id: str = Field(min_length=1, max_length=80, pattern=r"^version-[0-9]{4,}$")


def _response(success: bool, data: Any = None, error: dict[str, Any] | None = None, status_code: int = 200) -> JSONResponse:
    body = {"success": success, "data": data, "error": error, "request_id": uuid.uuid4().hex, "timestamp": datetime.now(timezone.utc).isoformat()}
    body["data"] = sanitize_sensitive_data(data)
    body["error"] = sanitize_sensitive_data(error)
    return JSONResponse(status_code=status_code, content=body)


def _error(code: str, message: str, detail: Any = None, retryable: bool = False, status_code: int = 400) -> JSONResponse:
    safe_message = redact_sensitive_text(message)
    safe_detail = sanitize_sensitive_data(detail)
    return _response(False, None, {"code": code, "message": safe_message, "detail": safe_detail, "retryable": retryable}, status_code)


def _license_gate(feature: str | None = None) -> JSONResponse | None:
    from modules.license_service import license_allows_generation

    valid, status = license_allows_generation(feature)
    if status.get("valid"):
        return None
    return _error("LICENSE_REQUIRED", str(status.get("message") or "当前授权不可用"), None, retryable=False, status_code=403)


def _edition_block(detail: str = "当前交付版本每次只能创作1个热点、1篇文章。") -> JSONResponse:
    return _error("FEATURE_NOT_AVAILABLE_IN_CURRENT_EDITION", detail, detail, retryable=False, status_code=403)


@app.get("/api/license/status")
def license_status() -> JSONResponse:
    status = check_license()
    device = device_status()
    return _response(True, {"valid": bool(status.get("valid")), "code": status.get("code"), "message": status.get("message"), "license": status.get("license"), "device_code": device.get("device_code"), "installation_id_missing": device.get("installation_id_missing", False)})


@app.post("/api/license/import")
async def license_import(file: UploadFile = File(...)) -> JSONResponse:
    temporary: Path | None = None
    try:
        suffix = Path(file.filename or "license").suffix or ".license"
        with tempfile.NamedTemporaryFile(prefix="license-", suffix=suffix, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(await file.read())
        status = import_license(temporary)
        return _response(bool(status.get("valid")), status, None if status.get("valid") else {"code": status.get("code"), "message": status.get("message"), "detail": None, "retryable": False}, 200 if status.get("valid") else 400)
    except Exception as exc:
        return _error("LICENSE_IMPORT_FAILED", "许可证文件无效", type(exc).__name__, retryable=False, status_code=400)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


@app.get("/api/health")
def health() -> JSONResponse:
    try:
        store.init_schema()
        return _response(True, {"service": "hotspot-workbench", "database": str(store.db_path), "status": "ok"})
    except Exception as exc:
        return _error("HEALTH_CHECK_FAILED", "服务健康检查失败", str(exc), retryable=True, status_code=503)


@app.post("/api/hotspots/refresh")
def refresh_hotspots() -> JSONResponse:
    try:
        result = service.refresh()
        data = dict(result)
        data["topics"] = [topic.to_dict() for topic in result["topics"]]
        return _response(result["status"] != "offline", data, None if result["status"] != "offline" else {"code": "NO_SOURCE", "message": "在线来源和缓存均不可用", "detail": result["errors"], "retryable": True}, 200 if result["status"] != "offline" else 503)
    except Exception as exc:
        return _error("HOTSPOT_REFRESH_FAILED", "热点刷新失败", str(exc), retryable=True, status_code=502)


@app.get("/api/hotspots")
def list_hotspots(keyword: str = "", category: str = "全部", source: str = "全部", sort: str = Query("captured_at_desc", pattern="^(captured_at_desc|hot_desc|rank_asc)$"), time_range: str = Query("全部时间", pattern="^(全部时间|最近1小时|最近6小时|最近24小时)$")) -> JSONResponse:
    try:
        topics = service.list_topics(keyword, category, source, sort, time_range)
        return _response(True, {"items": [topic.to_dict() for topic in topics], "count": len(topics), "filters": {"keyword": keyword, "category": category, "source": source, "sort": sort, "time_range": time_range}})
    except Exception as exc:
        return _error("HOTSPOT_LIST_FAILED", "热点列表读取失败", str(exc), retryable=True, status_code=500)


@app.get("/api/providers/status")
def provider_status() -> JSONResponse:
    items = store.list_provider_status()
    for item in items:
        if not item.get("last_error"):
            item["last_error"] = ""
        if not item.get("last_success_at"):
            item["last_success_at"] = ""
    return _response(True, {"items": items})


def _model_test_error(result: ModelTestResult) -> dict[str, Any] | None:
    if result.success:
        return None
    code = result.error_code or "PROVIDER_ERROR"
    return {"code": code, "message": user_facing_error_message(code, result.error_message or "网络连接异常"), "detail": result.error_message, "retryable": result.retryable}


def _current_test_profile(saved: dict[str, Any], payload: ModelTestRequest | None) -> dict[str, Any]:
    """Use visible form values for a test without persisting them."""
    profile = dict(saved)
    visible = dict((payload.profile if payload else None) or {})
    for key in ("name", "model", "base_url", "endpoint", "auth_type", "api_format", "size", "auth_header", "headers"):
        if key in visible and visible[key] is not None:
            profile[key] = visible[key]
    if "api_key" in visible and str(visible.get("api_key") or "").strip() and str(visible.get("api_key")) != "***":
        profile["api_key"] = str(visible["api_key"])
    return profile


def _normalized_text_value(value: Any) -> str:
    return str(value or "").strip()


def _persist_verified_text_profile(settings: dict[str, Any], profile: dict[str, Any], tested_at: str) -> None:
    settings["verified_text_model"] = _normalized_text_value(profile.get("model"))
    settings["verified_text_base_url"] = _normalized_text_value(profile.get("base_url")).rstrip("/")
    settings["verified_text_endpoint"] = "/" + _normalized_text_value(profile.get("endpoint") or "/chat/completions").lstrip("/")
    settings["verified_at"] = tested_at
    settings["last_text_model_test_at"] = tested_at
    text_profile = dict(settings.get("text_profile") or {})
    text_profile["last_text_model_test_at"] = tested_at
    settings["text_profile"] = text_profile
    save_settings(settings)


def _text_profile_is_verified(settings: dict[str, Any], profile: dict[str, Any]) -> bool:
    current_model = _normalized_text_value(profile.get("model"))
    current_base_url = _normalized_text_value(profile.get("base_url")).rstrip("/")
    current_endpoint = "/" + _normalized_text_value(profile.get("endpoint") or "/chat/completions").lstrip("/")
    return (
        current_model
        and current_model == _normalized_text_value(settings.get("verified_text_model"))
        and current_base_url == _normalized_text_value(settings.get("verified_text_base_url")).rstrip("/")
        and current_endpoint == ("/" + _normalized_text_value(settings.get("verified_text_endpoint") or "/chat/completions").lstrip("/"))
    )


@app.post("/api/models/text/test")
def test_text_model(payload: ModelTestRequest | None = None) -> JSONResponse:
    blocked = _license_gate()
    if blocked:
        return blocked
    settings = load_settings()
    profile = _current_test_profile(dict(settings.get("text_profile") or {}), payload)
    if payload and payload.timeout_override:
        profile["timeout_seconds"] = payload.timeout_override
    result = OpenAITextProvider(profile, network_settings=settings.get("network")).test_connection()
    if result.success:
        _persist_verified_text_profile(settings, profile, datetime.now(timezone.utc).isoformat())
    return _response(result.success, result.to_dict(), _model_test_error(result), 200)


@app.post("/api/models/text/compatibility-test")
def compatibility_test_text_model(payload: ModelTestRequest | None = None) -> JSONResponse:
    blocked = _license_gate()
    if blocked:
        return blocked
    settings = load_settings()
    profile = _current_test_profile(dict(settings.get("text_profile") or {}), payload)
    if payload and payload.timeout_override:
        profile["timeout_seconds"] = payload.timeout_override
    result = OpenAITextProvider(profile, network_settings=settings.get("network")).basic_connection_test()
    if result.success:
        _persist_verified_text_profile(settings, profile, datetime.now(timezone.utc).isoformat())
    return _response(result.success, result.to_dict(), _model_test_error(result), 200)


@app.post("/api/models/text/article-capability-test")
def article_capability_test_text_model(payload: ModelTestRequest | None = None) -> JSONResponse:
    blocked = _license_gate()
    if blocked:
        return blocked
    if not payload or not payload.confirm_paid_test:
        return _error("TEXT_LONG_TEST_CONFIRMATION_REQUIRED", "本操作将真实调用文本模型生成一段约300字的结构化内容，可能产生少量费用。请确认后继续。", {"text_generation_calls": 0, "charged": False}, retryable=False, status_code=400)
    settings = load_settings()
    profile = _current_test_profile(dict(settings.get("text_profile") or {}), payload)
    if payload and payload.timeout_override:
        profile["timeout_seconds"] = payload.timeout_override
    result = OpenAITextProvider(profile, network_settings=settings.get("network")).article_capability_test()
    if result.success:
        _persist_verified_text_profile(settings, profile, datetime.now(timezone.utc).isoformat())
    return _response(result.success, result.to_dict(), _model_test_error(result), 200)


@app.post("/api/models/discover")
def discover_available_models(payload: ModelDiscoverRequest | None = None) -> JSONResponse:
    return _discover_available_models("text", payload)


@app.post("/api/models/text/discover")
def discover_text_models(payload: ModelDiscoverRequest | None = None) -> JSONResponse:
    return _discover_available_models("text", payload)


@app.post("/api/models/image/discover")
def discover_image_models(payload: ModelDiscoverRequest | None = None) -> JSONResponse:
    return _discover_available_models("image", payload)


def _discover_available_models(default_kind: Literal["text", "image"], payload: ModelDiscoverRequest | None = None) -> JSONResponse:
    blocked = _license_gate()
    if blocked:
        return blocked
    settings = load_settings()
    profile_kind = str((payload.profile_kind if payload else default_kind) or default_kind)
    if profile_kind not in {"text", "image"}:
        profile_kind = default_kind
    profile_name = "image_profile" if profile_kind == "image" else "text_profile"
    saved_profile = dict(settings.get(profile_name) or {})
    visible_profile = dict((payload.profile if payload else None) or {})
    use_for_both = bool(payload and payload.use_for_both)
    if use_for_both and not str(visible_profile.get("api_key") or "").strip() and not str(saved_profile.get("api_key") or "").strip():
        other_profile_name = "text_profile" if profile_kind == "image" else "image_profile"
        other_profile = dict(settings.get(other_profile_name) or {})
        if str(other_profile.get("api_key") or "").strip():
            saved_profile["api_key"] = str(other_profile.get("api_key") or "")
    profile = _current_test_profile(saved_profile, ModelTestRequest(profile=visible_profile))
    result = discover_models(profile, settings.get("network"))
    return _response(bool(result.get("success")), result, None if result.get("success") else {"code": result.get("error_code") or "MODEL_DISCOVERY_FAILED", "message": result.get("message") or "模型列表读取失败", "detail": result.get("detail") or "", "retryable": result.get("error_code") in {"MODEL_LIST_UNSUPPORTED", "RATE_LIMITED", "TIMEOUT", "NETWORK_ERROR"}}, 200)


@app.post("/api/models/image/test")
def test_image_model(payload: ModelTestRequest | None = None) -> JSONResponse:
    blocked = _license_gate("image_generation")
    if blocked:
        return blocked
    settings = load_settings()
    profile = _current_test_profile(dict(settings.get("image_profile") or {}), payload)
    if not payload or not payload.confirm_paid_test:
        return _error("PAID_TEST_CONFIRMATION_REQUIRED", "本操作会真实调用图片模型，可能产生费用。请确认后继续。", {"generation_calls": 0, "charged": False}, retryable=False, status_code=400)
    if payload and payload.timeout_override:
        profile["timeout_seconds"] = payload.timeout_override
    output_root = model_test_root()
    output_root.mkdir(parents=True, exist_ok=True)
    output_path = output_root / "image-test.png"
    output_path.unlink(missing_ok=True)
    result = OpenAIImageProvider(profile, network_settings=settings.get("network")).test_connection(output_path)
    tested_at = datetime.now(timezone.utc).isoformat()
    result.details["last_test_at"] = tested_at
    if result.success:
        result.details["test_image_available"] = True
        result.details["test_image_name"] = output_path.name
        image_profile = dict(settings.get("image_profile") or {})
        image_profile["last_image_test_at"] = tested_at
        settings["image_profile"] = image_profile
        save_settings(settings)
    return _response(result.success, result.to_dict(), _model_test_error(result), 200)


@app.get("/api/models/image/test-artifact", response_model=None)
def get_image_test_artifact() -> FileResponse | JSONResponse:
    path = model_test_root() / "image-test.png"
    if not path.is_file():
        return _error("TEST_IMAGE_NOT_FOUND", "暂无测试图片", status_code=404)
    return FileResponse(path, media_type="image/png", filename="image-test.png")


@app.delete("/api/models/image/test-artifact")
def delete_image_test_artifact() -> JSONResponse:
    (model_test_root() / "image-test.png").unlink(missing_ok=True)
    return _response(True, {"deleted": True})


@app.post("/api/models/image/check-config")
def check_image_model_config(payload: ModelTestRequest | None = None) -> JSONResponse:
    settings = load_settings()
    profile = _current_test_profile(dict(settings.get("image_profile") or {}), payload)
    result = OpenAIImageProvider(profile, network_settings=settings.get("network")).check_configuration()
    return _response(result.success, result.to_dict(), _model_test_error(result), 200)


@app.post("/api/topics/{topic_id}/research")
def research_topic(topic_id: str, payload: ResearchRequest | None = None) -> JSONResponse:
    topic = next((item for item in store.list_topics(limit=500) if item.id == topic_id), None)
    if topic is None:
        return _error("TOPIC_NOT_FOUND", "热点不存在", topic_id, status_code=404)
    request = payload or ResearchRequest()
    bundle = ResearchService().collect(topic, request.reference_urls, request.supplemental_text)
    return _response(True, bundle)


@app.get("/api/topics/{topic_id}/research")
def get_topic_research(topic_id: str) -> JSONResponse:
    bundle = load_research_bundle(topic_id)
    if bundle is None:
        return _response(True, {"research_status": "not_collected", "information_sufficiency_score": 0, "sources": [], "verified_facts": []})
    return _response(True, bundle)


VALID_CATEGORIES = set(CATEGORIES)


@app.patch("/api/hotspots/{topic_id}")
def update_hotspot(topic_id: str, payload: dict[str, Any]) -> JSONResponse:
    category = str(payload.get("category") or "").strip()
    if category not in VALID_CATEGORIES:
        return _error("INVALID_CATEGORY", "不支持的热点分类", category)
    topic = store.update_topic_category(topic_id, category)
    if not topic:
        return _error("TOPIC_NOT_FOUND", "热点不存在", topic_id, status_code=404)
    return _response(True, topic.to_dict())


@app.post("/api/topics/url-fetch")
def url_fetch_topic(payload: UrlFetchRequest) -> JSONResponse:
    try:
        fetched = ResearchService().fetcher(payload.url)
        if not fetched.get("fetch_success"):
            return _error("URL_FETCH_FAILED", "无法读取该链接，请输入标题或话题名称。", fetched, retryable=True, status_code=502)
        title = str(fetched.get("title") or "").strip()[:300]
        content = str(fetched.get("content") or fetched.get("summary") or "").strip()[:5000]
        if not title and not content:
            return _error("URL_FETCH_FAILED", "无法读取该链接，请输入标题或话题名称。", fetched, retryable=True, status_code=502)
        return _response(True, {"url": payload.url, "title": title, "content": content, "content_length": len(content), "summary": str(fetched.get("summary") or "")[:5000]})
    except Exception as exc:
        return _error("URL_FETCH_FAILED", "无法读取该链接，请输入标题或话题名称。", str(exc)[:200], retryable=True, status_code=502)


@app.post("/api/topics/manual")
def add_manual_topic(payload: ManualTopicRequest) -> JSONResponse:
    try:
        topic = service.add_manual_topic(payload.title, payload.summary, payload.reference_url)
        return _response(True, topic.to_dict())
    except Exception as exc:
        return _error("MANUAL_TOPIC_FAILED", "手动话题创建失败", str(exc))


@app.post("/api/topics/select")
def select_topics(payload: SelectTopicsRequest) -> JSONResponse:
    try:
        topics = service.select_topics(payload.topic_ids)
        return _response(True, {"items": [topic.to_dict() for topic in topics], "count": len(topics)})
    except Exception as exc:
        return _error("TOPIC_SELECTION_FAILED", "话题选择失败", str(exc))


@app.get("/api/topics/basket")
def get_basket() -> JSONResponse:
    return _response(True, {"items": service.get_basket(), "count": len(service.get_basket())})


@app.post("/api/topics/basket")
def add_basket(payload: BasketRequest) -> JSONResponse:
    try:
        items = service.add_to_basket(payload.topic_ids)
        return _response(True, {"items": items, "count": len(items)})
    except ValueError as exc:
        code = str(exc) if str(exc).startswith("TOPIC-") else "TOPIC-SELECT-STATE"
        message = {
            "TOPIC-SELECT-DUPLICATE": "这个热点已经在选题篮里了。",
            "TOPIC-SELECT-LIMIT": "选题篮最多只能选择 5 个热点。",
            "TOPIC-SELECT-STATE": "选题篮状态异常，请刷新后再试。",
        }.get(code, "选题篮状态异常，请刷新后再试。")
        return _error(code, message, str(exc), retryable=False, status_code=409)
    except Exception as exc:
        return _error("TOPIC-SELECT-STATE", "选题篮状态异常，请刷新后再试。", str(exc))


@app.delete("/api/topics/basket/{topic_id}")
def remove_basket(topic_id: str) -> JSONResponse:
    try:
        return _response(True, {"items": service.remove_from_basket(topic_id)})
    except Exception as exc:
        return _error("TOPIC-REMOVE-FAILED", "热点移除失败，请刷新后再试。", str(exc), retryable=False, status_code=409)


@app.delete("/api/topics/basket")
def clear_basket() -> JSONResponse:
    return _response(True, {"items": service.clear_basket()})


@app.post("/api/topics/basket/order")
def reorder_basket(payload: BasketOrderRequest) -> JSONResponse:
    try:
        items = service.reorder_basket(payload.topic_ids)
        return _response(True, {"items": items, "count": len(items)})
    except Exception as exc:
        return _error("BASKET_REORDER_FAILED", "选题篮排序更新失败", str(exc))


@app.post("/api/tasks")
def create_task(payload: CreateTaskRequest) -> JSONResponse:
    if payload.mode == "single_topic_multi_angle" and len(payload.topic_ids) != 1:
        return _error("TOPIC-SELECT-LIMIT", "单热点生成多篇只能选择1个热点。", None, retryable=False, status_code=400)
    if payload.mode == "multi_topic" and payload.article_count != 1:
        return _error("TOTAL_ARTICLE_LIMIT", "多热点模式每个热点只能生成1篇。", None, retryable=False, status_code=400)
    total_articles = payload.article_count if payload.mode == "single_topic_multi_angle" else len(payload.topic_ids)
    if total_articles > 5:
        return _error("TOTAL_ARTICLE_LIMIT", "一次最多生成5篇文章。", None, retryable=False, status_code=400)
    blocked = _license_gate()
    if blocked:
        return blocked
    try:
        task = service.create_task(payload.task_name, payload.mode, payload.topic_ids, payload.article_count, payload.generation_options.model_dump(exclude_none=True))
        return _response(True, task, None, 201)
    except Exception as exc:
        return _error("TASK_CREATE_FAILED", "生成任务创建失败", str(exc))


@app.get("/api/tasks")
def list_tasks(limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0), unbatched: bool = Query(False)) -> JSONResponse:
    items = store.list_tasks(limit=limit, offset=offset, unbatched=unbatched)
    return _response(True, {"items": items, "count": len(items), "limit": limit, "offset": offset, "unbatched": unbatched})

def _load_phase2a_task(task_id: str) -> dict[str, Any]:
    task = store.get_task(task_id)
    if not task:
        raise ProviderError("TASK_NOT_FOUND", "task not found")
    if len(task.get("selected_topics") or []) != 1 or int(task.get("article_count") or 0) != 1:
        raise ProviderError("PHASE2A_SINGLE_ONLY", "2A only accepts one selected topic and one planned article")
    return task


def _task_error_response(error: ProviderError, status_code: int = 400) -> JSONResponse:
    return _error(error.code, error.detail, error.detail, retryable=is_retryable_error(error.code), status_code=status_code)


def _run_task_response(task_id: str, retry_step: str | None = None) -> JSONResponse:
    blocked = _license_gate()
    if blocked:
        return blocked
    try:
        task = _load_phase2a_task(task_id)
        settings = load_settings()
        recover_interrupted_tasks(store=store, executor=executor)
        lock_context = executor.task_lock(task_id) if hasattr(executor, "task_lock") else nullcontext()
        with lock_context:
            state = load_generation_task(task_id)
            if executor.is_running(task_id) or (state and state.get("status") == "running"):
                return _error("TASK_ALREADY_RUNNING", "task is already running", task_id, retryable=False, status_code=409)
            if state and state.get("status") == "completed":
                return _error("TASK_ALREADY_COMPLETED", "completed task cannot run again", task_id, retryable=False, status_code=409)
            if state and state.get("status") == "cancelled":
                return _error("TASK_CANCELLED", "cancelled task cannot run again", task_id, retryable=False, status_code=409)
            if retry_step == "retry-cover" and state and not state.get("article"):
                return _error("ARTICLE_NOT_AVAILABLE", "article result is missing", task_id, retryable=False, status_code=409)
            if retry_step != "retry-cover" and not _text_profile_is_verified(settings, settings.get("text_profile", {})):
                current_profile = dict(settings.get("text_profile") or {})
                return _error(
                    "TEXT_MODEL_NOT_VERIFIED",
                    "当前文本模型尚未测试。请先在“模型设置”中完成测试，再重新写文章。",
                    {
                        "model": _normalized_text_value(current_profile.get("model")),
                        "base_url": _normalized_text_value(current_profile.get("base_url")),
                        "endpoint": _normalized_text_value(current_profile.get("endpoint")),
                        "verified_text_model": _normalized_text_value(settings.get("verified_text_model")),
                        "verified_at": _normalized_text_value(settings.get("verified_at")),
                    },
                    retryable=False,
                    status_code=409,
                )
            prepared = prepare_generation_state(task, settings.get("text_profile", {}), settings.get("image_profile", {}), store=store)
            executor.submit(task_id, lambda: executor.execute_with_retry(task, settings.get("text_profile", {}), settings.get("image_profile", {}), settings, store, retry_step))
            return _response(True, prepared, None, 202)
    except ProviderError as exc:
        status_code = 404 if exc.code == "TASK_NOT_FOUND" else 409 if exc.code in {"TASK_ALREADY_RUNNING", "TASK_ALREADY_COMPLETED", "TASK_CANCELLED", "PHASE2A_SINGLE_ONLY", "ARTICLE_NOT_AVAILABLE"} else 400
        return _task_error_response(exc, status_code)
    except RuntimeError as exc:
        if str(exc) == "TASK_ALREADY_RUNNING":
            return _error("TASK_ALREADY_RUNNING", "task is already running", task_id, retryable=False, status_code=409)
        return _error("TASK_RUN_FAILED", "task run failed", str(exc), retryable=False, status_code=400)
    except Exception as exc:
        return _error("TASK_RUN_FAILED", "task run failed", str(exc), retryable=False, status_code=400)


@app.post("/api/tasks/{task_id}/run")
def run_task(task_id: str) -> JSONResponse:
    return _run_task_response(task_id)


@app.post("/api/tasks/{task_id}/retry-article")
def retry_article(task_id: str) -> JSONResponse:
    return _run_task_response(task_id, "retry-article")


@app.post("/api/tasks/{task_id}/retry-cover")
def retry_cover(task_id: str) -> JSONResponse:
    return _run_task_response(task_id, "retry-cover")


def ensure_article_allows_paid_image_generation(state: dict[str, Any] | None) -> None:
    gate = (state or {}).get("quality_gate") or {}
    if gate.get("status") == "failed" or int(gate.get("hard_error_count") or 0) > 0:
        raise ProviderError("QUALITY_GATE_FAILED", "article quality gate failed")


@app.post("/api/tasks/{task_id}/images/generate")
def generate_selected_task_images(task_id: str, payload: ImageSelectionRequest | None = None) -> JSONResponse:
    blocked = _license_gate("image_generation")
    if blocked:
        return blocked
    request = payload or ImageSelectionRequest()
    if not request.confirm_paid:
        return _error("PAID_IMAGE_CONFIRMATION_REQUIRED", "本次图片生成会真实调用图片模型并可能产生费用，请确认后继续。", {"generation_calls": 0, "charged": False}, retryable=False, status_code=400)
    try:
        task = store.get_task(task_id)
        if not task:
            raise ProviderError("TASK_NOT_FOUND", "task not found")
        state = load_generation_task(task_id)
        if not state or not state.get("article"):
            raise ProviderError("ARTICLE_NOT_AVAILABLE", "article result is missing")
        ensure_article_allows_paid_image_generation(state)
        if executor.is_running(task_id):
            raise ProviderError("TASK_ALREADY_RUNNING", "task is already running")
        settings = load_settings()
        future = executor.submit(task_id, lambda: generate_selected_images(task_id, settings.get("image_profile", {}), settings, store, include_cover=request.include_cover, inline_count=request.inline_count))
        return _response(True, {"status": "queued", "include_cover": request.include_cover, "inline_count": request.inline_count, "estimated_calls": (1 if request.include_cover else 0) + request.inline_count, "future_active": not future.done()}, None, 202)
    except ProviderError as exc:
        status_code = 404 if exc.code == "TASK_NOT_FOUND" else 409 if exc.code in {"TASK_ALREADY_RUNNING", "ARTICLE_NOT_AVAILABLE", "TASK_NOT_READY"} else 400
        return _task_error_response(exc, status_code)
    except RuntimeError as exc:
        return _error("IMAGE_GENERATION_SUBMIT_FAILED", "图片生成任务提交失败", str(exc), retryable=False, status_code=409)


@app.post("/api/tasks/{task_id}/cancel")
def cancel_task(task_id: str) -> JSONResponse:
    try:
        result = executor.cancel(task_id, store=store)
        return _response(True, result)
    except ProviderError as exc:
        status_code = 404 if exc.code == "TASK_NOT_FOUND" else 409 if exc.code in {"TASK_ALREADY_COMPLETED", "TASK_CANCELLED"} else 400
        return _task_error_response(exc, status_code)
    except Exception as exc:
        return _error("TASK_CANCEL_FAILED", "task cancellation failed", str(exc), retryable=False, status_code=400)


def _safe_remove_task_dir(task_id: str) -> bool:
    path = generation_task_dir(task_id).resolve()
    root = tasks_root().resolve()
    if path.parent != root or not path.name or path.name in {".", ".."}:
        raise ProviderError("TASK_DELETE_UNSAFE", "task delete target is unsafe")
    if path.exists():
        shutil.rmtree(path)
        return True
    return False


def _clear_research_bundle_for_task(task: dict[str, Any]) -> None:
    topics = task.get("selected_topics") or []
    if not topics:
        return
    topic_id = str((topics[0] or {}).get("id") or "").strip()
    if not topic_id:
        return
    path = (research_root() / topic_id / "research_bundle.json").resolve()
    root = research_root().resolve()
    if path.parent.parent == root:
        path.unlink(missing_ok=True)


def _reset_for_research_regenerate(task_id: str, payload: ResearchRequest | None = None) -> dict[str, Any]:
    task = store.get_task(task_id)
    if not task:
        raise ProviderError("TASK_NOT_FOUND", "task not found")
    state = load_generation_task(task_id) or prepare_generation_state(task, load_settings().get("text_profile", {}), load_settings().get("image_profile", {}), store=store)
    current_version = int(state.get("state_version") or 0)
    _clear_research_bundle_for_task(task)
    state.update({
        "status": "queued", "stage": "queued", "progress": 0, "completed_at": None,
        "failed_step": None, "error_code": "", "safe_error_message": "", "retryable": False,
        "article": None, "cover": None, "inline_images": [], "inline_image_summary": {"total": 0, "completed": 0, "failed": 0, "pending": 0, "status": "not_started"},
        "research_bundle": None, "research_status": "not_collected", "quality_gate": {"status": "not_checked", "metrics": {}, "reasons": []},
        "quality_rewrite_count": 0, "image_usage": {"generation_calls": 0, "paid_calls": 0, "retry_calls": 0, "budget_exceeded": False},
        "rewrite_requested": False, "previous_result": None, "fallback_notice": "", "state_version": current_version + 1,
    })
    if payload and (payload.reference_urls or payload.supplemental_text.strip()):
        state["manual_research_payload"] = {"reference_urls": payload.reference_urls, "supplemental_text": payload.supplemental_text}
    else:
        state.pop("manual_research_payload", None)
    save_generation_task(state, expected_version=current_version if current_version else None, allow_terminal_recovery=True)
    store.update_task_status(task_id, "queued")
    return state


@app.post("/api/tasks/{task_id}/research-regenerate")
def research_regenerate_task(task_id: str, payload: ResearchRequest | None = None) -> JSONResponse:
    blocked = _license_gate()
    if blocked:
        return blocked
    try:
        if executor.is_running(task_id) or batch_executor.is_task_active(task_id):
            return _error("TASK_ALREADY_RUNNING", "task is already running", task_id, retryable=False, status_code=409)
        _reset_for_research_regenerate(task_id, payload)
        return _run_task_response(task_id, "retry-article")
    except ProviderError as exc:
        return _task_error_response(exc, 404 if exc.code == "TASK_NOT_FOUND" else 409)
    except Exception as exc:
        return _error("TASK_RESEARCH_REGENERATE_FAILED", "重新搜索资料并生成失败", str(exc), retryable=False, status_code=400)


@app.post("/api/batches/{batch_id}/items/{task_id}/research-regenerate")
def research_regenerate_batch_item(batch_id: str, task_id: str, payload: ResearchRequest | None = None) -> JSONResponse:
    blocked = _license_gate()
    if blocked:
        return blocked
    try:
        if not batch_executor.store.get_batch_item(batch_id, task_id):
            raise ProviderError("BATCH_ITEM_NOT_FOUND", "task does not belong to batch")
        if batch_executor.is_task_active(task_id):
            raise ProviderError("TASK_ALREADY_RUNNING", "task is already running")
        _reset_for_research_regenerate(task_id, payload)
        return _response(True, batch_executor.retry_task(batch_id, task_id), None, 202)
    except Exception as exc:
        return _batch_error_response(exc)


@app.delete("/api/tasks/{task_id}")
def delete_task_api(task_id: str, payload: DeleteRequest | None = None) -> JSONResponse:
    request = payload or DeleteRequest()
    if not request.confirm:
        return _error("TASK_DELETE_CONFIRM_REQUIRED", "删除前请确认。已导出的 Word/ZIP 默认不会删除。", retryable=False, status_code=400)
    try:
        if executor.is_running(task_id) or batch_executor.is_task_active(task_id):
            raise ProviderError("TASK_ALREADY_RUNNING", "task is already running")
        if not store.get_task(task_id):
            raise ProviderError("TASK_NOT_FOUND", "task not found")
        batch_ids = store.get_task_batch_ids(task_id)
        removed_files = _safe_remove_task_dir(task_id)
        deleted = store.delete_task(task_id)
        refreshed_batches = [store.refresh_batch(batch_id) for batch_id in batch_ids]
        return _response(True, {"task_id": task_id, "deleted": deleted, "removed_work_files": removed_files, "deleted_exports": False, "batches": [item for item in refreshed_batches if item]})
    except ProviderError as exc:
        return _task_error_response(exc, 404 if exc.code == "TASK_NOT_FOUND" else 409)
    except Exception as exc:
        return _error("TASK_DELETE_FAILED", "任务删除失败", str(exc), retryable=False, status_code=400)


@app.delete("/api/batches/{batch_id}")
def delete_batch_api(batch_id: str, payload: DeleteRequest | None = None) -> JSONResponse:
    request = payload or DeleteRequest()
    if not request.confirm:
        return _error("BATCH_DELETE_CONFIRM_REQUIRED", "删除本次创作前请确认。已导出的 Word/ZIP 默认不会删除。", retryable=False, status_code=400)
    try:
        batch = batch_executor.store.get_batch(batch_id)
        if not batch:
            raise ProviderError("BATCH_NOT_FOUND", "batch not found")
        active = [str((item.get("task") or {}).get("task_id") or "") for item in batch.get("items") or [] if batch_executor.is_task_active(str((item.get("task") or {}).get("task_id") or ""))]
        if active:
            raise ProviderError("TASK_ALREADY_RUNNING", "task is already running")
        task_ids = [str((item.get("task") or {}).get("task_id") or "") for item in batch.get("items") or []]
        for task_id in task_ids:
            if task_id:
                _safe_remove_task_dir(task_id)
        deleted_task_ids = batch_executor.store.delete_batch(batch_id)
        return _response(True, {"batch_id": batch_id, "deleted": True, "task_ids": deleted_task_ids, "deleted_exports": False})
    except Exception as exc:
        return _batch_error_response(exc)


@app.post("/api/tasks/clear-failed")
def clear_failed_tasks(payload: DeleteRequest | None = None) -> JSONResponse:
    request = payload or DeleteRequest()
    if not request.confirm:
        return _error("TASK_DELETE_CONFIRM_REQUIRED", "清空失败任务前请确认。已导出的 Word/ZIP 默认不会删除。", retryable=False, status_code=400)
    try:
        candidates = [task for task in store.list_tasks() if str(task.get("status") or "") in {"failed", "partial_success"} and not batch_executor.is_task_active(str(task.get("task_id") or ""))]
        task_ids = [str(task.get("task_id") or "") for task in candidates]
        for task_id in task_ids:
            if task_id:
                _safe_remove_task_dir(task_id)
        deleted_task_ids = store.delete_failed_tasks()
        return _response(True, {"deleted_count": len(deleted_task_ids), "task_ids": deleted_task_ids, "deleted_exports": False})
    except Exception as exc:
        return _error("TASK_DELETE_FAILED", "失败任务清理失败", str(exc), retryable=False, status_code=400)


def _submit_inline_image_operation(
    task_id: str,
    target_ids: list[str] | None = None,
    regenerate_all: bool = False,
    confirm_paid: bool = False,
) -> JSONResponse:
    blocked = _license_gate("image_generation")
    if blocked:
        return blocked
    if not confirm_paid:
        return _error("PAID_IMAGE_CONFIRMATION_REQUIRED", "本次图片操作会真实调用图片模型并可能产生费用，请确认后继续。", {"generation_calls": 0, "charged": False}, retryable=False, status_code=400)
    try:
        task = store.get_task(task_id)
        if not task:
            raise ProviderError("TASK_NOT_FOUND", "task not found")
        state = load_generation_task(task_id)
        if not state or not state.get("article"):
            raise ProviderError("ARTICLE_NOT_AVAILABLE", "article result is missing")
        ensure_article_allows_paid_image_generation(state)
        if executor.is_running(task_id):
            raise ProviderError("TASK_ALREADY_RUNNING", "task is already running")
        settings = load_settings()
        executor.submit_inline_images(
            task_id,
            settings.get("image_profile", {}),
            settings,
            store,
            target_ids=target_ids,
            regenerate_all=regenerate_all,
        )
        return _response(True, {"status": "queued", "image_ids": target_ids or [], "regenerate_all": regenerate_all}, None, 202)
    except ProviderError as exc:
        status_code = 404 if exc.code == "TASK_NOT_FOUND" else 409 if exc.code in {"TASK_ALREADY_RUNNING", "INLINE_IMAGE_ALREADY_RUNNING", "ARTICLE_NOT_AVAILABLE"} else 400
        return _task_error_response(exc, status_code)
    except RuntimeError as exc:
        if str(exc) == "TASK_ALREADY_RUNNING":
            return _error("TASK_ALREADY_RUNNING", "task is already running", retryable=False, status_code=409)
        return _error("INLINE_IMAGE_RETRY_FAILED", "正文图片操作失败", str(exc), retryable=False, status_code=400)
    except Exception as exc:
        return _error("INLINE_IMAGE_RETRY_FAILED", "正文图片操作失败", str(exc), retryable=False, status_code=400)


@app.get("/api/tasks/{task_id}/inline-images")
def list_inline_images(task_id: str) -> JSONResponse:
    try:
        return _response(True, get_inline_images(task_id))
    except ProviderError as exc:
        return _task_error_response(exc, 404 if exc.code == "TASK_NOT_FOUND" else 400)
    except Exception as exc:
        return _error("INLINE_IMAGE_LIST_FAILED", "正文图片列表读取失败", str(exc), retryable=True, status_code=500)


@app.post("/api/tasks/{task_id}/inline-images/retry-failed")
def retry_failed_inline_images(task_id: str, payload: InlineImageRetryRequest | None = None) -> JSONResponse:
    return _submit_inline_image_operation(task_id, confirm_paid=bool(payload and payload.confirm_paid))


@app.post("/api/tasks/{task_id}/inline-images/regenerate")
def regenerate_inline_images(task_id: str, payload: InlineImageRetryRequest | None = None) -> JSONResponse:
    return _submit_inline_image_operation(task_id, regenerate_all=True, confirm_paid=bool(payload and payload.confirm_paid))


@app.post("/api/tasks/{task_id}/inline-images/{image_id}/retry")
def retry_inline_image(task_id: str, image_id: str, payload: InlineImageRetryRequest | None = None) -> JSONResponse:
    return _submit_inline_image_operation(task_id, [image_id], confirm_paid=bool(payload and payload.confirm_paid))


@app.get("/api/tasks/{task_id}/result")
def task_result(task_id: str) -> JSONResponse:
    if not store.get_task(task_id):
        return _error("TASK_NOT_FOUND", "task not found", task_id, status_code=404)
    result = load_generation_task(task_id)
    if not result:
        return _error("RESULT_NOT_FOUND", "generation result not found", task_id, retryable=False, status_code=404)
    return _response(True, result)


@app.get("/api/tasks/{task_id}")
def get_task(task_id: str) -> JSONResponse:
    task = store.get_task(task_id)
    if not task:
        return _error("TASK_NOT_FOUND", "任务不存在", task_id, status_code=404)
    return _response(True, task)


@app.get("/api/tasks/{task_id}/article")
def get_article_for_edit(task_id: str) -> JSONResponse:
    try:
        if not store.get_task(task_id):
            raise ProviderError("TASK_NOT_FOUND", "task not found")
        return _response(True, get_article(task_id))
    except ProviderError as exc:
        return _task_error_response(exc, 404 if exc.code == "TASK_NOT_FOUND" else 400)
    except Exception as exc:
        return _error("ARTICLE_READ_FAILED", "文章读取失败", str(exc), retryable=True, status_code=500)


@app.put("/api/tasks/{task_id}/article/draft")
def save_article_draft_api(task_id: str, payload: ArticleEditRequest) -> JSONResponse:
    try:
        return _response(True, save_article_draft(task_id, payload.model_dump(exclude_none=True), store))
    except ProviderError as exc:
        return _task_error_response(exc, 404 if exc.code == "TASK_NOT_FOUND" else 400)
    except Exception as exc:
        return _error("ARTICLE_DRAFT_FAILED", "草稿保存失败", str(exc), retryable=True, status_code=400)


@app.post("/api/tasks/{task_id}/article/save")
def save_article_api(task_id: str, payload: ArticleEditRequest | None = None) -> JSONResponse:
    try:
        changes = payload.model_dump(exclude_none=True) if payload else None
        return _response(True, save_article(task_id, changes, store))
    except ProviderError as exc:
        return _task_error_response(exc, 404 if exc.code == "TASK_NOT_FOUND" else 400)
    except Exception as exc:
        return _error("ARTICLE_SAVE_FAILED", "文章保存失败，当前版本未被覆盖", str(exc), retryable=True, status_code=400)


@app.post("/api/tasks/{task_id}/article/discard")
def discard_article_draft_api(task_id: str) -> JSONResponse:
    try:
        return _response(True, discard_article_draft(task_id, store))
    except ProviderError as exc:
        return _task_error_response(exc, 404 if exc.code == "TASK_NOT_FOUND" else 400)
    except Exception as exc:
        return _error("ARTICLE_DISCARD_FAILED", "本次修改已无法放弃", str(exc), retryable=False, status_code=400)


@app.get("/api/tasks/{task_id}/article/versions")
def article_versions(task_id: str) -> JSONResponse:
    try:
        return _response(True, get_article(task_id).get("versions") or [])
    except ProviderError as exc:
        return _task_error_response(exc, 404 if exc.code == "TASK_NOT_FOUND" else 400)
    except Exception as exc:
        return _error("ARTICLE_VERSION_LIST_FAILED", "文章版本读取失败", str(exc), retryable=True, status_code=500)


@app.post("/api/tasks/{task_id}/article/restore")
def restore_article_api(task_id: str, payload: ArticleVersionRequest) -> JSONResponse:
    try:
        return _response(True, restore_article_version(task_id, payload.version_id, store))
    except ProviderError as exc:
        return _task_error_response(exc, 404 if exc.code in {"TASK_NOT_FOUND", "ARTICLE_VERSION_NOT_FOUND"} else 400)
    except Exception as exc:
        return _error("ARTICLE_RESTORE_FAILED", "文章版本恢复失败", str(exc), retryable=True, status_code=400)


def _article_body_markdown_for_export(article: dict[str, Any]) -> str:
    body = str(article.get("body_markdown") or "").strip()
    if body:
        return body
    parts: list[str] = []
    for section in article.get("sections") or []:
        if not isinstance(section, dict):
            continue
        value = str(section.get("body") or "").strip()
        if value:
            parts.append(value)
    return "\n\n".join(parts).strip()


def _ensure_state_article_ready_for_export(state: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(state, dict):
        raise ProviderError("ARTICLE_NOT_READY", ARTICLE_NOT_READY_MESSAGE)
    if state.get("status") not in EXPORTABLE_ARTICLE_STATUSES or state.get("rewrite_requested"):
        raise ProviderError("ARTICLE_NOT_READY", ARTICLE_NOT_READY_MESSAGE)
    article = state.get("article")
    if not isinstance(article, dict):
        raise ProviderError("ARTICLE_NOT_READY", ARTICLE_NOT_READY_MESSAGE)
    title = str(article.get("title") or "").strip()
    body_markdown = _article_body_markdown_for_export(article)
    body_char_count = int(article.get("body_char_count") or count_body_chinese_chars(article) or 0)
    gate = state.get("quality_gate") or {}
    gate_status = str(gate.get("status") or "").strip()
    hard_error_count = int(gate.get("hard_error_count") or 0)
    if (
        not title
        or not body_markdown
        or body_char_count <= 0
        or gate_status in {"", "not_checked", "failed"}
        or hard_error_count > 0
    ):
        raise ProviderError("ARTICLE_NOT_READY", ARTICLE_NOT_READY_MESSAGE)
    return article


def _article_export(task_id: str, kind: str) -> FileResponse:
    task = store.get_task(task_id)
    if not task:
        raise ProviderError("TASK_NOT_FOUND", "task not found")
    state = load_generation_task(task_id)
    article = prepare_article_layout(_ensure_state_article_ready_for_export(state))
    if article.get("layout_status") != "passed" or not (article.get("layout_check") or {}).get("passed"):
        raise ProviderError("ARTICLE_LAYOUT_REQUIRED", "article layout and product check must pass before export")
        raise ProviderError("ARTICLE_NOT_FINAL", "内容仍在进行差异检查或自动优化，完成后即可导出。")
    root = generation_task_dir(task_id)
    export_root = exports_root()
    export_root.mkdir(parents=True, exist_ok=True)
    title = safe_filename(str(state["article"].get("title") or "文章"))
    if kind == "word":
        path = export_root / f"{task_id}_{title}.docx"
        export_article(article, path, root)
        return FileResponse(path, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document", filename=f"{title}.docx")
    path = export_root / f"{task_id}_{title}.zip"
    export_article_bundle(article, root, path)
    return FileResponse(path, media_type="application/zip", filename=f"{title}.zip")


def _exportable_state_article(task_id: str, *, absolute_image_paths: bool = False) -> tuple[dict[str, Any], Path]:
    state = load_generation_task(task_id) if task_id else None
    article = prepare_article_layout(_ensure_state_article_ready_for_export(state))
    if article.get("layout_status") != "passed" or not (article.get("layout_check") or {}).get("passed"):
        raise ProviderError("ARTICLE_LAYOUT_REQUIRED", "article layout and product check must pass before export")
    root = generation_task_dir(task_id)
    if absolute_image_paths:
        article = sanitize_sensitive_data(article)
        article["images"] = [{**image, "path": str(root / str(image.get("path") or ""))} for image in article.get("images") or []]
    return article, root


@app.get("/api/tasks/{task_id}/export/word")
def export_task_word(task_id: str):
    try:
        return _article_export(task_id, "word")
    except ProviderError as exc:
        return _task_error_response(exc, 404 if exc.code in {"TASK_NOT_FOUND", "ARTICLE_NOT_AVAILABLE"} else 409 if exc.code in {"ARTICLE_NOT_FINAL", "ARTICLE_LAYOUT_REQUIRED", "ARTICLE_NOT_READY"} else 400)
    except Exception as exc:
        return _error("WORD_EXPORT_FAILED", "Word 导出失败", str(exc), retryable=True, status_code=400)


@app.get("/api/tasks/{task_id}/export/zip")
def export_task_zip(task_id: str):
    try:
        return _article_export(task_id, "zip")
    except ProviderError as exc:
        return _task_error_response(exc, 404 if exc.code in {"TASK_NOT_FOUND", "ARTICLE_NOT_AVAILABLE"} else 409 if exc.code in {"ARTICLE_NOT_FINAL", "ARTICLE_LAYOUT_REQUIRED", "ARTICLE_NOT_READY"} else 400)
    except Exception as exc:
        return _error("ZIP_EXPORT_FAILED", "ZIP 导出失败", str(exc), retryable=True, status_code=400)


@app.get("/api/batches/{batch_id}/export/zip")
def export_batch_zip(batch_id: str):
    try:
        batch = batch_executor.store.get_batch(batch_id)
        if not batch:
            raise ProviderError("BATCH_NOT_FOUND", "batch not found")
        batch = batch_executor.store.refresh_batch(batch_id) or batch
        if not batch.get("final_ready"):
            raise ProviderError("BATCH_NOT_FINAL", "内容仍在进行差异检查或自动优化，完成后即可导出。")
        articles: list[tuple[dict[str, Any], Path]] = []
        for item in batch.get("items") or []:
            task_id = str((item.get("task") or {}).get("task_id") or "")
            task_status = str((item.get("task") or {}).get("status") or "")
            if task_status == "cancelled":
                continue
            articles.append(_exportable_state_article(task_id))
        if not articles:
            raise ProviderError("ARTICLE_NOT_AVAILABLE", "没有可导出的文章")
        export_root = exports_root()
        export_root.mkdir(parents=True, exist_ok=True)
        path = export_root / f"{batch_id}_{safe_filename(str(batch.get('batch_name') or '本次创作'))}.zip"
        export_batch_bundle(articles, path, str(batch.get("batch_name") or "本次创作"))
        return FileResponse(path, media_type="application/zip", filename=f"{safe_filename(str(batch.get('batch_name') or '本次创作'))}.zip")
    except ProviderError as exc:
        return _batch_error_response(exc, 404 if exc.code in {"BATCH_NOT_FOUND", "ARTICLE_NOT_AVAILABLE"} else 409 if exc.code in {"BATCH_NOT_FINAL", "ARTICLE_NOT_READY"} else 400)
    except Exception as exc:
        return _error("ZIP_EXPORT_FAILED", "ZIP 导出失败", str(exc), retryable=True, status_code=400)


@app.get("/api/batches/{batch_id}/export/word")
def export_batch_word(batch_id: str):
    try:
        batch = batch_executor.store.get_batch(batch_id)
        if not batch:
            raise ProviderError("BATCH_NOT_FOUND", "batch not found")
        batch = batch_executor.store.refresh_batch(batch_id) or batch
        if not batch.get("final_ready"):
            raise ProviderError("BATCH_NOT_FINAL", "内容仍在进行差异检查或自动优化，完成后即可导出。")
        articles: list[dict[str, Any]] = []
        for item in batch.get("items") or []:
            task_id = str((item.get("task") or {}).get("task_id") or "")
            task_status = str((item.get("task") or {}).get("status") or "")
            if task_status == "cancelled":
                continue
            article, _ = _exportable_state_article(task_id, absolute_image_paths=True)
            articles.append(article)
        if not articles:
            raise ProviderError("ARTICLE_NOT_AVAILABLE", "没有可导出的文章")
        export_root = exports_root()
        export_root.mkdir(parents=True, exist_ok=True)
        path = export_root / f"{batch_id}_{safe_filename(str(batch.get('batch_name') or '本次创作'))}.docx"
        from export.docx_exporter import export_combined
        export_combined(articles, path)
        return FileResponse(path, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document", filename=f"{safe_filename(str(batch.get('batch_name') or '本次创作'))}.docx")
    except ProviderError as exc:
        return _batch_error_response(exc, 404 if exc.code in {"BATCH_NOT_FOUND", "ARTICLE_NOT_AVAILABLE"} else 409 if exc.code in {"BATCH_NOT_FINAL", "ARTICLE_NOT_READY"} else 400)
    except Exception as exc:
        return _error("WORD_EXPORT_FAILED", "Word 导出失败", str(exc), retryable=True, status_code=400)


def _batch_error_response(error: ProviderError | Exception, status_code: int = 400) -> JSONResponse:
    code = str(getattr(error, "code", "BATCH_OPERATION_FAILED"))
    detail = str(getattr(error, "detail", error))
    if code in {"BATCH_NOT_FOUND", "BATCH_ITEM_NOT_FOUND", "TASK_NOT_FOUND"}:
        status_code = 404
    elif code in {"TASK_ALREADY_RUNNING", "TASK_ALREADY_COMPLETED", "TASK_CANCELLED", "BATCH_NOT_FINAL"}:
        status_code = 409
    return _error(code, redact_sensitive_text(detail), detail, retryable=is_retryable_error(code), status_code=status_code)


def _write_batch_submit_failure(batch_id: str, error_code: str, safe_message: str) -> None:
    """将批次提交失败原因写入每个 task 的 generation state 和数据库。

    目标：绝不让失败原因静默消失——要么在数据库/state 里，要么在 api.log 里。
    """
    store = batch_executor.store
    try:
        batch = store.get_batch(batch_id)
    except Exception:
        _logger.exception("_write_batch_submit_failure: get_batch failed batch_id=%s", batch_id)
        return
    if not batch:
        return
    for item in batch.get("items") or []:
        task = item.get("task") or {}
        task_id = str(task.get("task_id") or "")
        if not task_id:
            continue
        try:
            state = load_generation_task(task_id) or {}
            current_version = int(state.get("state_version") or 0)
            state.update({
                "status": "failed",
                "stage": "failed",
                "progress": 0,
                "failed_step": "batch_submit",
                "error_code": error_code,
                "safe_error_message": safe_message,
                "retryable": True,
                "state_version": current_version + 1,
            })
            save_generation_task(state, expected_version=current_version if current_version else None,
                                 allow_terminal_recovery=True)
        except Exception:
            _logger.exception("_write_batch_submit_failure: task state failed task_id=%s", task_id)
        try:
            store.update_task_status(task_id, "failed")
        except Exception:
            _logger.exception("_write_batch_submit_failure: update_task_status failed task_id=%s", task_id)


@app.post("/api/batches")
def create_batch(payload: CreateBatchRequest) -> JSONResponse:
    # ── 临时防重复（进程内弱幂等）──
    client_req_id = str(payload.client_request_id or "").strip()
    if client_req_id:
        import time as _time
        now_ts = _time.time()
        _cleanup_stale_dedup_entries(now_ts)
        cached = _BATCH_DEDUP_STORE.get(client_req_id)
        if cached and (now_ts - float(cached.get("_ts") or 0)) < 10:
            return _response(True, {**cached, "dedup": True, "message": "任务已创建，请到我的内容查看"}, None, 200)
    requested_topics = payload.topic_ids or payload.topics or []
    if payload.mode == "single_topic_multi_angle" and len(requested_topics) != 1:
        return _error("TOPIC-SELECT-LIMIT", "单热点生成多篇只能选择1个热点。", None, retryable=False, status_code=400)
    if payload.mode == "multi_topic" and payload.article_count != 1:
        return _error("TOTAL_ARTICLE_LIMIT", "多热点模式每个热点只能生成1篇。", None, retryable=False, status_code=400)
    total_articles = payload.article_count if payload.mode == "single_topic_multi_angle" else len(requested_topics)
    if total_articles > 5:
        return _error("TOTAL_ARTICLE_LIMIT", "一次最多生成5篇文章。", None, retryable=False, status_code=400)
    blocked = _license_gate()
    if blocked:
        return blocked
    try:
        if payload.topic_ids:
            topics = [topic.to_dict() for topic in service.select_topics(payload.topic_ids)]
        elif payload.topics:
            topics = sanitize_sensitive_data(payload.topics)
        else:
            raise ValueError("batch requires 1 to 5 topics")
        confirmed_paid = bool(payload.generation_options.confirm_paid)
        options = payload.generation_options.model_dump(exclude_none=True)
        options["confirm_paid"] = confirmed_paid
        options["article_count"] = payload.article_count
        image_mode = str(options.get("image_plan_mode") or load_settings().get("image_plan_mode") or "standard")
        if image_mode == "none":
            options["confirm_paid"] = False
        # RC1.3.3-Lite does not perform automatic image retries; a retry is always a new user-confirmed action.
        options["image_retry_limit"] = 0
        angle_plans = plan_angles(payload.article_count, payload.angles) if payload.mode == "single_topic_multi_angle" else None
        if payload.mode == "single_topic_multi_angle":
            concurrency = max(1, min(3, payload.article_count))
        else:
            concurrency = max(1, min(3, len(topics)))
        batch = batch_executor.store.create_batch(payload.batch_name, payload.mode, topics, options, concurrency, angle_plans)
        batch_id = str(batch.get("batch_id") or "")
        try:
            batch = batch_executor.start_batch(batch_id) or batch
        except Exception:
            _logger.exception("create_batch: start_batch failed batch_id=%s", batch_id)
            # 把每个 task 的失败原因写回 generation state
            _write_batch_submit_failure(batch_id, "BATCH_START_FAILED",
                                        "批次启动失败，请查看 api.log 或稍后重试。")
            batch = batch_executor.store.refresh_batch(batch_id) or batch
        # ── 存储幂等记录 ──
        if client_req_id:
            _BATCH_DEDUP_STORE[client_req_id] = {**batch, "_ts": time.time()}
        return _response(True, batch, None, 201)
    except Exception as exc:
        return _batch_error_response(exc)


@app.get("/api/batches")
def list_batches(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0), refresh: bool = Query(True)) -> JSONResponse:
    try:
        items = []
        item_errors: list[dict[str, Any]] = []
        for batch in batch_executor.store.list_batches(limit=limit, offset=offset):
            batch_id = str((batch or {}).get("batch_id") or "")
            if not batch_id:
                continue
            try:
                refreshed = batch_executor.store.refresh_batch(batch_id) if refresh else batch
            except Exception as exc:
                _logger.exception("list_batches: refresh failed batch_id=%s", batch_id)
                item_errors.append({"batch_id": batch_id, "error": redact_sensitive_text(str(exc))})
                refreshed = batch
            if refreshed:
                items.append(refreshed)
        return _response(True, {"items": items, "count": len(items), "limit": limit, "offset": offset, "item_errors": item_errors})
    except Exception as exc:
        return _batch_error_response(exc, 500)

@app.get("/api/batches/{batch_id}")
def get_batch(batch_id: str) -> JSONResponse:
    try:
        batch = batch_executor.store.refresh_batch(batch_id)
        if not batch:
            raise ProviderError("BATCH_NOT_FOUND", "batch not found")
        return _response(True, batch)
    except Exception as exc:
        return _batch_error_response(exc)


@app.get("/api/batches/{batch_id}/items")
def list_batch_items(batch_id: str) -> JSONResponse:
    try:
        if not batch_executor.store.get_batch(batch_id):
            raise ProviderError("BATCH_NOT_FOUND", "batch not found")
        items = batch_executor.store.list_batch_items(batch_id)
        return _response(True, {"items": items, "count": len(items)})
    except Exception as exc:
        return _batch_error_response(exc)


@app.post("/api/batches/{batch_id}/start")
def start_batch(batch_id: str) -> JSONResponse:
    blocked = _license_gate()
    if blocked:
        return blocked
    try:
        batch = batch_executor.store.get_batch(batch_id)
        if not batch:
            raise ProviderError("BATCH_NOT_FOUND", "batch not found")
        options = batch.get("generation_options") or {}
        image_mode = str(options.get("image_plan_mode") or load_settings().get("image_plan_mode") or "standard")
        if "image_plan_mode" in options and image_mode != "none" and not bool(options.get("confirm_paid")):
            return _error("PAID_BATCH_IMAGE_CONFIRMATION_REQUIRED", "我确认本次会真实调用图片模型，并可能产生费用。请先勾选确认后再开始生成。", {"generation_calls": 0, "charged": False}, retryable=False, status_code=400)
        return _response(True, batch_executor.start_batch(batch_id), None, 202)
    except Exception as exc:
        return _batch_error_response(exc)


@app.post("/api/batches/{batch_id}/cancel")
def cancel_batch(batch_id: str) -> JSONResponse:
    try:
        return _response(True, batch_executor.cancel_batch(batch_id))
    except Exception as exc:
        return _batch_error_response(exc)


@app.post("/api/batches/{batch_id}/retry-failed")
def retry_failed_batch(batch_id: str) -> JSONResponse:
    blocked = _license_gate()
    if blocked:
        return blocked
    try:
        return _response(True, batch_executor.retry_failed(batch_id), None, 202)
    except Exception as exc:
        return _batch_error_response(exc)


@app.post("/api/batches/{batch_id}/quality/retry")
def retry_batch_quality(batch_id: str) -> JSONResponse:
    blocked = _license_gate()
    if blocked:
        return blocked
    try:
        return _response(True, batch_executor.retry_quality_check(batch_id), None, 202)
    except Exception as exc:
        return _batch_error_response(exc)


@app.post("/api/batches/{batch_id}/items/{task_id}/cancel")
def cancel_batch_item(batch_id: str, task_id: str) -> JSONResponse:
    try:
        return _response(True, batch_executor.cancel_task(batch_id, task_id))
    except Exception as exc:
        return _batch_error_response(exc)


@app.post("/api/batches/{batch_id}/items/{task_id}/retry")
def retry_batch_item(batch_id: str, task_id: str) -> JSONResponse:
    blocked = _license_gate()
    if blocked:
        return blocked
    try:
        return _response(True, batch_executor.retry_task(batch_id, task_id), None, 202)
    except Exception as exc:
        return _batch_error_response(exc)
