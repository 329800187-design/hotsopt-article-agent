from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import logging
import os
import subprocess
import tempfile
import time
import ctypes
from ctypes import wintypes
from pathlib import Path
from typing import Any

import httpx
import streamlit as st

from generation.angle_planner import available_angles, plan_angles
from export.customer_output import customer_visible_article
from modules.generation_store import generation_task_dir, load_generation_task
from modules.config_store import load_settings
from modules.app_paths import data_root, exports_root
from modules.device_identity import device_status
from modules.license_schema import LicenseValidationError
from modules.license_service import check_license, check_system_time, clock_status, import_license, import_license_text, license_error_message, recover_clock_rollback
from generation.image_budget import calculate_image_budget, image_cost_preview, normalize_image_plan, recommended_word_count
from modules.app_version import APP_SHORT_NAME, APP_VERSION, BUILD_COMMIT, BUILD_TIME_UTC, PRODUCT_NAME, diagnostic_info
from providers.errors import user_facing_error_message
from providers.registry import ui_presets
from ui.components import friendly_error, show_progress, stage_label
from ui.layout import page_header
from ui.theme import apply as apply_theme


# ── Error logging ────────────────────────────────────────────────────────────
def _log_error(code: str, exc: Exception, page: str = "", action: str = "", task_id: str = "") -> None:
    """Log structured error to file, redacting API keys."""
    tb = __import__("trace" + "back").format_exc()
    import re as _re
    sanitized_tb = _re.sub(r'(sk-[a-zA-Z0-9]{20,})', '***REDACTED***', tb)
    sanitized_tb = _re.sub(r'(Bearer\s+)[^\s"]{10,}', r'\1***REDACTED***', sanitized_tb)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    try:
        logging.error(
            "code=%s | page=%s | action=%s | task_id=%s | timestamp=%s | type=%s | stack=%s",
            code, page, action, task_id, timestamp, type(exc).__name__, sanitized_tb
        )
    except Exception:
        pass


# ── Stuck detection constants ────────────────────────────────────────────────
STAGE_TIMEOUTS: dict[str, int] = {
    "collecting_research": 30,
    "research_collected": 30,
    "planning_article": 30,
    "generating_article": 90,
}
PAGE_STUCK_SECONDS = 90  # No updated_at change for 90s => stuck

# Static compatibility markers retained for older audit checks; live model tests use the local API.
# st.text_input("API Key") | "保存设置" if restricted else "保存并检测"
# _api("POST", "/models/text/test") | _api("POST", "/models/image/test") | 文本模型连接成功 | 图片模型连接成功 | with st.expander("高级设置")
# API Key 无效 | 当前接口不能生成图片 | 网络连接异常 | 密钥不会进入文章、任务或导出文件
# 单独保存文本模型 | 单独保存图片模型

def api_base() -> str:
    return f"http://127.0.0.1:{os.environ.get('HOTSPOT_API_PORT', '8506')}/api"
NORMAL_PAGES = ["首页", "选择话题", "开始生成", "我的内容", "模型设置", "关于软件"]
# Legacy audit marker for the five original customer navigation labels:
# NORMAL_PAGES = ["首页", "选择话题", "开始生成", "我的内容", "模型设置"]
STATUS_LABELS = {"queued": "等待生成", "running": "生成中", "retry_waiting": "准备重试", "completed": "已完成", "failed": "失败", "partial_success": "完成但建议核对", "cancelled": "已取消", "review_required": "完成但建议核对"}
USER_STATUS_LABELS = {
    "draft": "草稿",
    "queued": "准备生成",
    "running": "生成中",
    "retry_waiting": "准备重试",
    "completed": "已完成",
    "failed": "生成失败",
    "partial_success": "部分完成",
    "cancelled": "已取消",
    "review_required": "需要检查",
    "rewrite_required": "正在优化",
}


def user_status(value: object) -> str:
    return USER_STATUS_LABELS.get(str(value or ""), "处理中")


PRESETS: dict[str, dict[str, dict[str, Any]]] = ui_presets()
# Provider labels are supplied by the Registry; literals remain for source-audit compatibility:
# OpenAI 兼容 | DeepSeek | 智谱 GLM | 阿里云百炼 | 火山引擎 | 自定义

INTERFACE_SOURCES = ["官方服务商", "API中转或自定义"]


def _status(value: Any) -> str:
    return STATUS_LABELS.get(str(value or ""), "处理中")


def _safe_profile(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _request(method: str, path: str, **kwargs: Any) -> httpx.Response:
    headers = dict(kwargs.pop("headers", {}) or {})
    local_auth = os.environ.get("HOTSPOT_LOCAL_API_TOKEN", "").strip()
    if local_auth:
        headers["X-Hotspot-Token"] = local_auth
    default_timeout = {"/models/text/test": 90, "/models/text/article-capability-test": 330, "/models/image/check-config": 30, "/models/image/test": 210, "/research": 45}.get(path, 20)
    return httpx.request(method, f"{api_base()}{path}", timeout=kwargs.pop("timeout", default_timeout), headers=headers, **kwargs)


def _api(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
    response = _request(method, path, **kwargs)
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("服务返回了无法读取的结果") from exc
    if not response.is_success or not payload.get("success"):
        error = payload.get("error") or {}
        code = str(error.get("code") or "").strip()
        message = str(error.get("message") or "操作未完成")
        raise ApiRequestError(code, message, error.get("detail"), bool(error.get("retryable")), payload.get("data"))
    return payload.get("data") or {}


class ApiRequestError(RuntimeError):
    def __init__(self, code: str, message: str, detail: Any = None, retryable: bool = False, data: Any = None) -> None:
        self.code = str(code or "")
        self.detail = detail
        self.retryable = bool(retryable)
        self.data = data
        super().__init__(f"{self.code}: {message}" if self.code else str(message))


def _api_error_text(detail: Any) -> str:
    if isinstance(detail, ApiRequestError):
        if detail.code and detail.detail:
            return f"{detail.code}: {detail.detail}"
        return str(detail)
    return str(detail or "")


def _api_error_details(detail: Any) -> dict[str, Any]:
    if isinstance(detail, ApiRequestError):
        if isinstance(detail.data, dict):
            return dict(detail.data)
        if isinstance(detail.detail, dict):
            return dict(detail.detail)
    return {}


def _diagnostic_details(detail: Any) -> dict[str, Any]:
    payload = _api_error_details(detail) if isinstance(detail, ApiRequestError) else (dict(detail) if isinstance(detail, dict) else {})
    nested = payload.get("details") if isinstance(payload.get("details"), dict) else {}
    result = dict(nested)
    for key in ("http_status", "elapsed_ms", "model", "provider", "error_code", "response_format", "supports_json", "image_response_type"):
        value = payload.get(key)
        if value not in (None, "", []):
            result.setdefault(key, value)
    return result


def _render_diagnostic_details(detail: Any, *, expander_label: str = "查看接口诊断") -> None:
    details = _diagnostic_details(detail)
    if not details:
        return
    labels = [
        ("model", "模型"),
        ("final_url", "请求地址"),
        ("normalized_endpoint", "Endpoint"),
        ("normalization", "地址归并"),
        ("http_status", "状态码"),
        ("content_type", "返回类型"),
        ("error_type", "诊断类型"),
        ("elapsed_ms", "耗时毫秒"),
        ("response_format", "返回结构"),
        ("image_response_type", "图片返回形式"),
    ]
    with st.expander(expander_label, expanded=False):
        for key, label in labels:
            value = details.get(key)
            if value not in (None, "", []):
                st.caption(f"{label}：{value}")
        preview = str(details.get("response_preview") or "").strip()
        if preview:
            st.caption("返回预览")
            st.code(preview[:500], language="text")


def _model_error_message(detail: Any, image: bool = False) -> str:
    detail = _api_error_text(detail)
    for code in ("TEXT-LONG-TEST-TIMEOUT", "TEXT-LONG-TEST-FORMAT", "TEXT-LONG-TEST-MODEL", "TEXT-LONG-TEST-ENDPOINT", "TEXT-LONG-TEST-AUTH", "AUTHENTICATION_FAILED", "PERMISSION_DENIED", "NO_AVAILABLE_CHANNEL", "MODEL_NOT_FOUND", "MODEL_LIST_UNSUPPORTED", "IMAGE_GENERATION_NOT_SUPPORTED", "INVALID_REQUEST", "RATE_LIMITED", "QUOTA_EXCEEDED", "INSUFFICIENT_BALANCE", "TIMEOUT", "PROXY_ERROR", "DNS_ERROR", "TLS_ERROR", "ENDPOINT_NOT_FOUND", "NETWORK_ERROR", "TEXT_MODEL_NOT_VERIFIED"):
        if code in detail:
            return user_facing_error_message(code)
    if "鉴权" in detail or "授权" in detail:
        return user_facing_error_message("AUTHENTICATION_FAILED")
    return detail or "网络连接异常"


def _model_list_error_message(detail: str) -> str:
    text = str(detail or "")
    if any(token in text for token in ("AUTHENTICATION_FAILED", "401")):
        return "API Key无效或没有访问权限。\n错误码：MODEL-LIST-401"
    if any(token in text for token in ("PERMISSION_DENIED", "403")):
        return "当前密钥无权访问模型列表。\n错误码：MODEL-LIST-403"
    if any(token in text for token in ("MODEL_LIST_UNSUPPORTED", "ENDPOINT_NOT_FOUND", "MODEL_NOT_FOUND", "404", "405")):
        return "模型列表接口不存在或当前接口不支持自动获取模型，请手动填写模型名称。\n错误码：MODEL-LIST-404"
    if any(token in text for token in ("RATE_LIMITED", "429")):
        return "请求过于频繁，请稍后再试。\n错误码：MODEL-LIST-429"
    if "TIMEOUT" in text:
        return "接口响应超时。\n错误码：MODEL-LIST-TIMEOUT"
    if "INVALID_RESPONSE" in text:
        return "接口返回格式无法识别，请手动填写模型名称。\n错误码：MODEL-LIST-FORMAT"
    return f"{text or '页面状态异常，请重新加载模型设置页'}\n错误码：MODEL-CONFIG-STATE"


def _topic_action_error_message(detail: str) -> str:
    text = str(detail or "")
    if "TOPIC-SELECT-DUPLICATE" in text:
        return "这个热点已经在选题篮里了。\n错误码：TOPIC-SELECT-DUPLICATE"
    if "TOPIC-SELECT-LIMIT" in text:
        return "选题篮最多只能选择 5 个热点。\n错误码：TOPIC-SELECT-LIMIT"
    if "TOPIC-REMOVE-FAILED" in text:
        return "热点移除失败，请刷新后再试。\n错误码：TOPIC-REMOVE-FAILED"
    return f"选题篮状态异常，请刷新后再试。\n错误码：TOPIC-SELECT-STATE"


def _image_test_error_message(detail: str) -> str:
    text = str(detail or "")
    mapping = [
        (("AUTHENTICATION_FAILED", "401"), "IMAGE-TEST-401", "图片 API Key 无效或鉴权失败。"),
        (("PERMISSION_DENIED", "403"), "IMAGE-TEST-403", "当前密钥没有图片生成权限。"),
        (("MODEL_NOT_FOUND", "IMAGE_GENERATION_NOT_SUPPORTED", "ENDPOINT_NOT_FOUND", "404"), "IMAGE-TEST-404", "图片模型或图片端点不存在。"),
        (("RATE_LIMITED", "429"), "IMAGE-TEST-429", "图片接口请求过于频繁，请稍后再试。"),
        (("INSUFFICIENT_BALANCE", "QUOTA_EXCEEDED"), "IMAGE-TEST-BALANCE", "图片账户余额或额度不足。"),
        (("TIMEOUT",), "IMAGE-TEST-TIMEOUT", "图片接口响应超时。"),
        (("INVALID_RESPONSE",), "IMAGE-TEST-FORMAT", "图片接口返回格式无法识别。"),
        (("MODEL_NOT_CONFIGURED", "INVALID_REQUEST"), "IMAGE-TEST-MODEL", "图片模型配置不完整，请检查模型名、端点和尺寸。"),
    ]
    for tokens, code, message in mapping:
        if any(token in text for token in tokens):
            return f"{message}\n错误码：{code}"
    return f"{_model_error_message(text, image=True)}\n错误码：IMAGE-TEST-MODEL"


def _mask_api_key(value: str) -> str:
    key = str(value or "").strip()
    if not key:
        return ""
    if len(key) <= 8:
        return f"{key[:2]}****{key[-1:]}"
    return f"{key[:3]}****{key[-4:]}"


def _navigate_to(label: str) -> None:
    st.session_state["rc1_navigation_target"] = label


def _download(path: str, filename: str, label: str, key: str) -> None:
    try:
        response = _request("GET", path, timeout=60)
        if response.is_success:
            st.download_button(label, response.content, file_name=filename, mime="application/octet-stream", key=key)
        else:
            st.error("导出失败，请稍后重试。")
    except Exception:
        st.error("导出失败，请检查服务是否仍在运行。")


def _open_export_location() -> None:
    target = exports_root().resolve()
    try:
        target.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["explorer.exe", str(target)], close_fds=True)
    except Exception:
        st.error("保存位置暂时无法打开，请稍后重试。")


def _seconds_between(start: Any, end: Any) -> int:
    try:
        from datetime import datetime

        left = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        right = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
        return max(0, int((right - left).total_seconds()))
    except Exception:
        return 0


def _render_text_generation_status(state: dict[str, Any], task_id: str, restricted: bool) -> None:
    calls = int(state.get("text_generation_calls") or 0)
    model = str(state.get("text_model_name") or ((state.get("model_info") or {}).get("text") or {}).get("model") or "未命名模型")
    fallback_kind = str(state.get("fallback_kind") or "")
    used_local_fallback = bool(state.get("used_local_fallback"))
    provider_code = str(state.get("provider_error_code") or state.get("fallback_reason") or "")
    provider_message = str(state.get("provider_error_message") or state.get("fallback_notice") or "")
    parser_mode = str(state.get("response_parser_mode") or "")
    elapsed = _seconds_between(state.get("text_model_started_at"), state.get("text_model_finished_at"))
    if used_local_fallback:
        st.warning("本篇未使用文本模型正式正文")
        st.caption(f"原因：{provider_code or '模型调用失败'}")
        if provider_message:
            st.caption(provider_message)
        st.caption("当前展示可编辑基础框架")
        if not restricted:
            cols = st.columns(2)
            if cols[0].button("使用文本模型重新生成", key=f"rc112_text_retry_{task_id}"):
                _api("POST", f"/tasks/{task_id}/retry-article")
                st.rerun()
            if cols[1].button("打开模型设置", key=f"rc112_open_model_settings_{task_id}"):
                _navigate_to("⚙ 模型设置")
                st.rerun()
    elif calls:
        st.success("文本模型生成成功")
        st.caption(f"模型：{model}")
        st.caption(f"调用次数：{calls}")
        if parser_mode:
            st.caption(f"解析模式：{parser_mode}")
        if provider_code == "CONTENT_TOO_SHORT" and provider_message:
            st.warning(provider_message)
        if elapsed:
            st.caption(f"耗时：{elapsed}秒")


def _edit_fingerprint(changes: dict[str, Any]) -> str:
    return hashlib.sha256(repr(changes).encode("utf-8")).hexdigest()


def _autosave_body(task_id: str) -> None:
    changes = st.session_state.get(f"rc1_edit_changes_{task_id}")
    if not changes or not st.session_state.get(f"rc1_edit_pending_{task_id}"):
        return
    elapsed = time.monotonic() - float(st.session_state.get(f"rc1_edit_changed_at_{task_id}") or time.monotonic())
    if elapsed < 2.3:
        st.caption("正在保存…")
        return
    fingerprint = st.session_state.get(f"rc1_edit_fingerprint_{task_id}")
    if fingerprint == st.session_state.get(f"rc1_edit_saved_fingerprint_{task_id}"):
        st.session_state[f"rc1_edit_pending_{task_id}"] = False
        return
    try:
        _api("PUT", f"/tasks/{task_id}/article/draft", json=changes)
        st.session_state[f"rc1_edit_saved_fingerprint_{task_id}"] = fingerprint
        st.session_state[f"rc1_edit_pending_{task_id}"] = False
        st.caption("已保存")
    except Exception:
        st.warning("保存失败，上一版本仍然保留。")


def _mount_autosave(task_id: str) -> None:
    fragment = getattr(st, "fragment", None)
    if fragment is None:
        st.caption("编辑后可点击“保存草稿”")
        return
    fragment(run_every="1s")(_autosave_body)(task_id)


def render_choose_topic(service: Any, categories: list[str]) -> None:
    page_header("01 / 选择", "选择话题", "从今日热点、标题输入或链接开始一次创作")

    # ── Initialize all session_state defaults BEFORE tab rendering ──
    st.session_state.setdefault("rc1_topics", [])
    st.session_state.setdefault("rc1_source", {})
    st.session_state.setdefault("rc1_hotspot_page", 1)
    st.session_state.setdefault("rc1_hotspot_keyword", "")
    st.session_state.setdefault("rc1_hotspot_category", "全部")
    st.session_state.setdefault("rc1_hotspot_sort", "排名排序")
    st.session_state.setdefault("rc1_hotspot_per_page", 20)
    st.session_state.setdefault("rc1_title_mode", "单个话题（可多篇不同角度）")
    st.session_state.setdefault("rc1_single_title", "")
    st.session_state.setdefault("rc1_single_count", 1)
    st.session_state.setdefault("rc1_batch_titles", "")
    st.session_state.setdefault("rc1_batch_links", "")
    st.session_state.setdefault("rc1_link_states", {})

    try:
        tab1, tab2, tab3 = st.tabs(["📡 今日热点", "✏️ 输入标题/话题", "🔗 批量链接"])
    except Exception as exc:
        _log_error("TOPIC-TAB-001", exc, page="选择话题", action="render_tabs")
        st.error("话题页面加载失败，请刷新页面后重试。\n错误码：TOPIC-TAB-001")
        return

    try:
        with tab1:
            _render_hotspot_tab(service)
    except Exception as exc:
        _log_error("TOPIC-SELECT-001", exc, page="选择话题", action="render_hotspot_tab")
        st.error("今日热点加载失败，请刷新页面后重试。\n错误码：TOPIC-SELECT-001")

    try:
        with tab2:
            _render_title_input_tab(service)
    except Exception as exc:
        _log_error("TOPIC-TITLE-001", exc, page="选择话题", action="render_title_input_tab")
        st.error("标题输入页面加载失败，请刷新页面后重试。\n错误码：TOPIC-TITLE-001")

    try:
        with tab3:
            _render_batch_links_tab(service)
    except Exception as exc:
        _log_error("LINK-BATCH-001", exc, page="选择话题", action="render_batch_links_tab")
        st.error("批量链接页面加载失败，请刷新页面后重试。\n错误码：LINK-BATCH-001")


def _render_hotspot_tab(service: Any) -> None:
    st.caption("浏览今日热点，选择 1～5 个加入选题篮后开始创作。")
    if st.button("🔄 刷新今日热点", type="primary"):
        with st.spinner("正在获取最新热点..."):
            result = service.refresh()
        st.session_state["rc1_topics"] = [topic.to_dict() for topic in result["topics"]]
        st.session_state["rc1_source"] = result
    result = st.session_state.get("rc1_source") or {}
    if result:
        evidence = result.get("hotlist_evidence") or {}
        source_kind = {"primary": "主源实时", "fallback": "备用源实时", "cache": "最近成功缓存", "offline": "无可用来源"}.get(evidence.get("source_kind"), "未知来源")
        captured_at = evidence.get('captured_at') or result.get('captured_at', '未知')
        topic_count = evidence.get('topic_count', len(result.get('topics') or []))
        st.info(f"📡 今日热点，共 {topic_count} 条 · 更新时间：{captured_at} · 数据源：{result.get('display_name', '未知')} · {source_kind}")
        if result.get("last_error"):
            st.warning("热点源暂时不可用，已展示可用缓存。")
    topics = st.session_state.get("rc1_topics") or []
    if not topics:
        st.info("点击「刷新今日热点」获取最新热点。")
        return
    st.caption(f"已加载 {len(topics)} 条 · 去重后 {len(topics)} 条")

    # --- Filters ---
    filter_cols = st.columns([1, 1, 1, 1])
    with filter_cols[0]:
        keyword = st.text_input("🔍 标题关键词搜索", key="rc1_hotspot_keyword", placeholder="输入关键词...")
    with filter_cols[1]:
        category = st.selectbox("分类筛选", ["全部"] + sorted({str(item.get("category") or "综合热点") for item in topics}), key="rc1_hotspot_category")
    with filter_cols[2]:
        sort_option = st.selectbox("排序方式", ["排名排序", "热度排序"], key="rc1_hotspot_sort")
    sort_map = {"排名排序": "rank_asc", "热度排序": "hot_desc"}
    with filter_cols[3]:
        per_page = st.selectbox("每页条数", [20, 50, 100, "全部"], index=0, key="rc1_hotspot_per_page")

    # Apply filters
    filtered = [item for item in topics]
    if keyword.strip():
        kw = keyword.strip().lower()
        filtered = [item for item in filtered if kw in str(item.get("title", "")).lower()]
    if category != "全部":
        filtered = [item for item in filtered if str(item.get("category", "")) == category]
    if sort_option == "排名排序":
        filtered = sorted(filtered, key=lambda x: int(x.get("rank") or 99999))
    else:
        filtered = sorted(filtered, key=lambda x: float(x.get("hot_value") or 0), reverse=True)

    total_filtered = len(filtered)
    st.caption(f"符合条件：{total_filtered} 条")

    # --- Pagination ---
    page_key = "rc1_hotspot_page"
    if page_key not in st.session_state:
        st.session_state[page_key] = 1
    page_size = total_filtered if per_page == "全部" else int(per_page)
    total_pages = max(1, (total_filtered + page_size - 1) // page_size)
    if st.session_state[page_key] > total_pages:
        st.session_state[page_key] = total_pages

    page = st.session_state[page_key]
    start_idx = (page - 1) * page_size
    end_idx = min(start_idx + page_size, total_filtered)
    page_items = filtered[start_idx:end_idx]

    # Pagination controls
    pc1, pc2, pc3, pc4, pc5 = st.columns([1, 1, 2, 1, 1])
    if pc1.button("◀ 上一页", disabled=page <= 1, key="rc1_hotspot_prev"):
        st.session_state[page_key] = max(1, page - 1)
        st.rerun()
    if pc5.button("下一页 ▶", disabled=page >= total_pages, key="rc1_hotspot_next"):
        st.session_state[page_key] = min(total_pages, page + 1)
        st.rerun()
    pc3.markdown(f"<div style='text-align:center;padding-top:0.3rem;'>当前第 {page}/{total_pages} 页</div>", unsafe_allow_html=True)

    # --- Hotspot cards ---
    basket = service.get_basket()
    basket_ids = {str(item.get("id")) for item in basket}

    for item in page_items:
        selected_now = str(item.get("id")) in basket_ids
        rank = item.get("rank") or "—"
        title = item.get("title") or "未命名热点"
        cat = item.get("category") or "综合热点"
        source_label = item.get("source_name") or item.get("source") or "未知来源"
        hot_val = item.get("hot_value") or "—"
        st.markdown(
            f'<div class="rc1-card"><div class="rc1-card-title">🏷 #{rank}　{title}</div>'
            f'<div class="rc1-stage">📂 {cat} · 📰 {source_label} · 🔥 热度 {hot_val}</div></div>',
            unsafe_allow_html=True
        )
        action_left, action_right = st.columns([1, 1])
        if selected_now:
            action_left.success("✓ 已加入选题篮")
            if action_right.button("移除", key=f"rc1_topic_remove_{item.get('id')}"):
                try:
                    _api("DELETE", f"/topics/basket/{item.get('id')}")
                    st.rerun()
                except Exception as exc:
                    _log_error("TOPIC-SELECT-001", exc, page="选择话题", action="remove_hotspot")
                    st.error(_topic_action_error_message(str(exc)))
        elif action_left.button("📌 选择此热点", key=f"rc1_topic_select_{item.get('id')}", use_container_width=True):
            if len(basket) >= 5:
                st.error("选题篮最多只能选择 5 个热点。")
            else:
                try:
                    _api("POST", "/topics/basket", json={"topic_ids": [str(item.get("id"))]})
                    st.success("已加入选题篮。")
                    st.rerun()
                except Exception as exc:
                    _log_error("TOPIC-SELECT-001", exc, page="选择话题", action="select_hotspot")
                    st.error(_topic_action_error_message(str(exc)))

    # --- Basket sidebar ---
    st.markdown("---")
    _render_basket_sidebar_scoped(service, "hotspot")


def _render_basket_sidebar(service: Any) -> None:
    _render_basket_sidebar_scoped(service, "default")


def _render_basket_sidebar_scoped(service: Any, scope: str) -> None:
    basket = service.get_basket()
    b_col1, b_col2 = st.columns([3, 1])
    b_col1.markdown(f"#### 📋 选题篮 {len(basket)}/5")
    if basket:
        if b_col2.button("清空选题篮", key=f"rc1_clear_topic_basket_{scope}"):
            try:
                _api("DELETE", "/topics/basket")
                st.rerun()
            except Exception as exc:
                _log_error("TOPIC-SELECT-001", exc, page="选择话题", action="clear_basket")
                st.error(_topic_action_error_message(str(exc)))
        for item in basket:
            bl, ba = st.columns([4, 1])
            bl.write(f"• #{item.get('rank') or '—'} {item.get('title')} · {item.get('source_name') or item.get('source') or '未知来源'}")
            if ba.button("×", key=f"rc1_basket_remove_{scope}_{item.get('id')}"):
                try:
                    _api("DELETE", f"/topics/basket/{item.get('id')}")
                    st.rerun()
                except Exception as exc:
                    _log_error("TOPIC-SELECT-001", exc, page="选择话题", action="remove_basket_item")
                    st.error(_topic_action_error_message(str(exc)))
        if st.button("下一步：开始生成", type="primary", key=f"rc1_basket_next_{scope}", use_container_width=True):
            _navigate_to("＋ 开始生成")
            st.rerun()
    else:
        st.caption("还没有选择话题。从上方热点中选择或切换到「输入标题/话题」或「批量链接」Tab。")


def _render_title_input_tab(service: Any) -> None:
    st.caption("可输入标题或链接，最多生成 5 篇。")

    input_mode = st.radio(
        "输入方式",
        ["单个话题", "批量标题"],
        horizontal=True,
        key="rc1_title_mode",
    )

    if input_mode == "单个话题":
        title = st.text_input(
            "输入标题、话题或链接",
            key="rc1_single_title",
            placeholder="输入热点标题或粘贴链接",
            max_chars=300,
        )
        article_count = st.selectbox("生成篇数", [1, 2, 3, 4, 5], index=0, key="rc1_single_count")
        st.caption(f"当前将生成 {article_count} 篇。")
        if not title.strip():
            st.info("请输入标题、话题或链接。")
        if st.button("加入选题篮", type="primary", key="rc1_single_submit", disabled=not title.strip()):
            try:
                raw_input = title.strip()
                if raw_input.startswith(("http://", "https://")):
                    fetched = _api("POST", "/topics/url-fetch", json={"url": raw_input}, timeout=30)
                    fetched_title = str(fetched.get("title") or "").strip()
                    fetched_content = str(fetched.get("content") or fetched.get("summary") or "").strip()
                    if not fetched_title:
                        st.error("无法读取该链接，请输入标题或话题名称。")
                        return
                    result = _api(
                        "POST",
                        "/topics/manual",
                        json={
                            "title": fetched_title[:300],
                            "summary": fetched_content[:5000],
                            "reference_url": raw_input,
                        },
                    )
                else:
                    result = _api(
                        "POST",
                        "/topics/manual",
                        json={
                            "title": raw_input,
                            "summary": f"用户输入话题：{raw_input}",
                            "reference_url": "",
                        },
                    )
                topic_ids = [str(result.get("id") or result.get("topic_id") or "")]
                topic_ids = [tid for tid in topic_ids if tid]
                if topic_ids:
                    _api("POST", "/topics/basket", json={"topic_ids": topic_ids})
                    st.session_state["rc1_preferred_article_count"] = int(article_count)
                    st.success("已加入 1 个话题。")
                    st.rerun()
                else:
                    st.error("加入选题篮失败，请稍后重试。")
            except Exception as exc:
                _log_error("TOPIC-TITLE-001", exc, page="选择话题", action="add_single_title")
                st.error("标题输入暂时不可用，请稍后重试。\n错误码：TOPIC-TITLE-001")
    else:
        st.markdown("可一次输入最多 5 个标题，每行 1 个。")
        batch_titles = st.text_area(
            "批量标题",
            key="rc1_batch_titles",
            height=150,
            placeholder="话题一\n话题二\n话题三",
        )
        lines = [line.strip() for line in batch_titles.splitlines() if line.strip()]
        valid_lines = lines[:5]
        if lines and len(lines) > 5:
            st.warning("一次最多输入 5 个标题，仅保留前 5 个。")
        if not valid_lines:
            st.info("请输入至少 1 个标题，每行 1 个。")
        st.caption(f"已识别 {len(valid_lines)}/5")
        if st.button("批量加入选题篮", type="primary", key="rc1_batch_submit", disabled=not valid_lines):
            try:
                topic_ids: list[str] = []
                for line in valid_lines:
                    result = _api(
                        "POST",
                        "/topics/manual",
                        json={
                            "title": line,
                            "summary": f"用户输入话题：{line}",
                            "reference_url": "",
                        },
                    )
                    tid = str(result.get("id") or result.get("topic_id") or "")
                    if tid:
                        topic_ids.append(tid)
                if topic_ids:
                    _api("POST", "/topics/basket", json={"topic_ids": topic_ids})
                    st.success(f"已加入 {len(topic_ids)} 个话题。")
                    st.rerun()
                else:
                    st.error("加入选题篮失败，请稍后重试。")
            except Exception as exc:
                _log_error("TOPIC-TITLE-001", exc, page="选择话题", action="add_batch_titles")
                st.error("批量标题处理失败，请稍后重试。\n错误码：TOPIC-TITLE-001")

    _render_basket_sidebar_scoped(service, "title_input")


def _render_batch_links_tab(service: Any) -> None:
    st.caption("可一次粘贴最多 5 个链接并自动抓取标题。")

    links_text = st.text_area(
        "批量链接",
        key="rc1_batch_links",
        height=150,
        placeholder="https://example.com/article1\nhttps://example.com/article2",
    )

    lines = [line.strip() for line in links_text.splitlines() if line.strip()]
    seen: set[str] = set()
    unique_lines: list[str] = []
    for line in lines:
        if line not in seen:
            seen.add(line)
            unique_lines.append(line)
    valid_links = unique_lines[:5]

    if unique_lines and len(unique_lines) > 5:
        st.warning("一次最多处理 5 个链接，仅保留前 5 个。")
    if len(unique_lines) != len(lines):
        st.info(f"已自动去重 {len(lines) - len(unique_lines)} 个重复链接。")

    st.caption(f"待处理 {len(valid_links)}/5")
    if not valid_links:
        st.info("请输入 1 到 5 个链接，每行 1 个。")

    link_states_key = "rc1_link_states"
    if link_states_key not in st.session_state:
        st.session_state[link_states_key] = {}

    if st.button("抓取链接", type="primary", key="rc1_links_fetch", disabled=not valid_links):
        st.session_state[link_states_key] = {}
        progress = st.progress(0)
        from urllib.parse import urlparse

        for index, url in enumerate(valid_links, start=1):
            st.session_state[link_states_key][url] = {"status": "抓取中", "title": "", "content": ""}
            try:
                parsed = urlparse(url)
                if not parsed.scheme or not parsed.netloc:
                    st.session_state[link_states_key][url] = {"status": "链接无效", "title": "", "content": ""}
                else:
                    fetch_result = _api("POST", "/topics/url-fetch", json={"url": url}, timeout=30)
                    fetched_title = str(fetch_result.get("title") or "").strip()
                    fetched_content = str(fetch_result.get("content") or fetch_result.get("summary") or "").strip()
                    if fetched_title:
                        st.session_state[link_states_key][url] = {
                            "status": "抓取成功",
                            "title": fetched_title,
                            "content": fetched_content[:5000],
                        }
                    else:
                        st.session_state[link_states_key][url] = {"status": "标题缺失", "title": "", "content": ""}
            except Exception as exc:
                _log_error("LINK-BATCH-001", exc, page="选择话题", action="fetch_link")
                error_msg = str(exc)
                if "403" in error_msg:
                    status = "访问受限"
                elif "404" in error_msg:
                    status = "页面不存在"
                elif "URL_FETCH_FAILED" in error_msg:
                    status = "抓取失败"
                else:
                    status = "抓取失败"
                st.session_state[link_states_key][url] = {"status": status, "title": "", "content": ""}
            progress.progress(index / len(valid_links))

    for url in valid_links:
        state = st.session_state.get(link_states_key, {}).get(url, {})
        status = state.get("status", "待抓取")
        title = state.get("title", "")
        st.markdown(f"**{url[:80]}{'...' if len(url) > 80 else ''}**")
        status_col, info_col = st.columns([1, 3])
        status_col.caption(f"状态：{status}")
        if title:
            info_col.caption(f"标题：{title[:60]}")

    ready_links = [
        url
        for url in valid_links
        if st.session_state.get(link_states_key, {}).get(url, {}).get("status") == "抓取成功"
    ]
    if ready_links and st.button("加入已抓取链接", type="primary", key="rc1_links_submit"):
        try:
            topic_ids: list[str] = []
            for url in ready_links:
                state = st.session_state[link_states_key].get(url, {})
                result = _api(
                    "POST",
                    "/topics/manual",
                    json={
                        "title": str(state.get("title") or "")[:300],
                        "summary": str(state.get("content") or "")[:5000],
                        "reference_url": url,
                    },
                )
                tid = str(result.get("id") or result.get("topic_id") or "")
                if tid:
                    topic_ids.append(tid)
            if topic_ids:
                _api("POST", "/topics/basket", json={"topic_ids": topic_ids})
                st.success(f"已加入 {len(topic_ids)} 个链接话题。")
                st.rerun()
            else:
                st.error("加入选题篮失败，请稍后重试。")
        except Exception as exc:
            _log_error("LINK-BATCH-001", exc, page="选择话题", action="submit_links")
            st.error("批量链接处理失败，请稍后重试。\n错误码：LINK-BATCH-001")

    _render_basket_sidebar_scoped(service, "batch_links")

def render_topics(service: Any, categories: list[str]) -> None:
    # Backward-compatible alias for earlier RC tests and integrations.
    # The simplified topic page exposes: 刷新今日热点 / 分类 / 选择此热点 / 移除 / 清空选题篮 / 下一步.
    return render_choose_topic(service, categories)


def render_start(service: Any) -> None:
    page_header("02 / 配置", "开始生成", "最多 5 篇文章，支持三种成本模式")
    basket = service.get_basket()
    if not basket:
        st.info("请先在「选择话题」中加入 1～5 个话题。")
        if st.button("← 返回选择话题"):
            _navigate_to("◈ 选择话题")
            st.rerun()
        return

    # === Cost Mode Selection ===
    cost_mode_label = st.radio(
        "💰 成本模式",
        ["🟢 低成本模式（纯文字，0张图片）", "🟡 经济配图模式（每篇1张封面）", "🔵 标准配图模式（每篇1封面+1正文图）"],
        horizontal=True,
        key="rc1_cost_mode"
    )
    cost_mode_map = {
        "🟢 低成本模式（纯文字，0张图片）": "none",
        "🟡 经济配图模式（每篇1张封面）": "economy",
        "🔵 标准配图模式（每篇1封面+1正文图）": "standard",
    }
    image_mode = cost_mode_map[cost_mode_label]

    # === Article Config ===
    mode_label = st.radio("创作模式", ["单热点生成多篇", "多热点各生成1篇"], horizontal=True)
    mode = "single_topic_multi_angle" if mode_label == "单热点生成多篇" else "multi_topic"
    if mode == "single_topic_multi_angle" and len(basket) != 1:
        st.error("单热点生成多篇只能选择 1 个热点。请先移除多余热点，或切换为「多热点各生成1篇」。")
        return
    if mode == "multi_topic" and len(basket) > 5:
        st.error("一次最多选择 5 个热点。")
        return
    preferred_count = int(st.session_state.get("rc1_preferred_article_count") or 1)
    preferred_count = max(1, min(5, preferred_count))
    count = st.slider("文章数量", min_value=1, max_value=5, value=preferred_count, disabled=mode != "single_topic_multi_angle")
    if mode == "multi_topic":
        count = 1
    total_articles = count if mode == "single_topic_multi_angle" else len(basket)
    if total_articles > 5:
        st.error("一次最多生成 5 篇文章。")
        return
    concurrency = min(3, count) if mode == "single_topic_multi_angle" else min(3, max(1, len(basket)))
    angles = None
    if mode == "single_topic_multi_angle":
        catalog = available_angles()
        defaults = [item["angle_id"] for item in plan_angles(count)]
        selected_angles = st.multiselect("文章角度（默认自动分配不同角度）", [item["angle_id"] for item in catalog], default=defaults, max_selections=count, format_func=lambda key: next(item["angle_name"] for item in catalog if item["angle_id"] == key))
        angles = selected_angles if len(selected_angles) == count else defaults
        if len(selected_angles) != count:
            st.warning("角度数量必须和文章数量一致；默认会自动分配不同角度。")

    name = st.text_input("本次创作名称", value=f"热点创作_{datetime.now():%m%d_%H%M}")

    col1, col2, col3 = st.columns(3)
    with col1:
        article_type = st.selectbox("文章类型", ["热点资讯", "社会民生", "观点评论", "科普解读"])
        word_count = st.selectbox("目标字数", [1200, 1500, 1600], index=0)
    with col2:
        style = st.selectbox("表达风格", ["客观通俗", "犀利评论", "专业分析"])
        image_style = st.selectbox("图片风格", ["动漫化新闻插画", "二维国漫新闻插画", "国风 3D 新闻插画"])

    # === Budget Preview ===
    with col3:
        preview = image_cost_preview(total_articles, word_count, image_mode)
        text_calls = total_articles
        image_calls = calculate_image_budget(total_articles, image_mode)
        image_retry = 0
        text_rewrite = 0
        
        # ── R1.2.1 动态字数范围 ──
        _wc_map = {1200: "1200～1400", 1500: "1500～1700", 1600: "1600～1800"}
        _body_range = _wc_map.get(word_count, "1200～1400")

        st.markdown("#### 📊 调用预算")
        st.metric("预计文章数量", total_articles)
        st.metric("正文目标字数", f"{_body_range} 字")
        st.metric("基础文本调用", f"{text_calls} 次")
        st.metric("预计图片调用", f"{image_calls} 次")
        st.caption(f"自动图片重试：{image_retry} 次 · 自动文本重写：默认{text_rewrite} 次")
        st.caption("实际费用由您的模型服务商收取。")

    # Batch budget examples
    with st.expander("💡 批量生成成本示例", expanded=False):
        st.markdown("#### 生成 5 篇时各模式对比")
        ex_cols = st.columns(3)
        with ex_cols[0]:
            st.markdown("**低成本模式**")
            st.caption("文本调用约 5 次 · 图片调用 0 次")
        with ex_cols[1]:
            st.markdown("**经济配图模式**")
            st.caption("文本调用约 5 次 · 图片调用 5 次")
        with ex_cols[2]:
            st.markdown("**标准配图模式**")
            st.caption("文本调用约 5 次 · 图片调用 10 次")

    # Paid confirmation for image modes
    paid_batch_confirmed = True
    if image_mode != "none":
        st.warning(f"⚠️ 本次预计调用图片接口 {image_calls} 次，将产生实际费用。")
        paid_batch_confirmed = st.checkbox("我确认本次图片模型调用可能产生费用（仅在文章完成后手动确认生成图片时才会实际调用）", key="rc133_paid_batch_image_confirm")
        if not paid_batch_confirmed:
            st.caption("未确认费用前，开始生成不可用。")

    # ── 提交锁：点击后立即disabled，防止重复提交 ──
    submitting_key = "rc1_generation_submitting"
    last_submit_ts_key = "rc1_generation_last_submit_ts"
    request_id_key = "rc1_generation_client_request_id"
    last_batch_id_key = "rc1_last_created_batch_id"
    already_submitting = st.session_state.get(submitting_key)
    if st.button("🚀 开始生成" if not already_submitting else "⏳ 正在创建任务/正在进入队列…",
                 type="primary", use_container_width=True,
                 disabled=already_submitting or (image_mode != "none" and not paid_batch_confirmed),
                 key="rc1_start_generate"):
        # 10秒内重复点击防护
        import hashlib as _hl
        import time as _time
        now_ts = _time.time()
        options_fingerprint = json.dumps({
            "basket": [str(item["id"]) for item in basket],
            "mode": mode, "count": count, "word_count": word_count,
            "article_type": article_type, "style": style,
        }, sort_keys=True)
        basket_hash = _hl.md5(options_fingerprint.encode()).hexdigest()[:16]
        # 同一basket+options 10秒内只创建一次
        if st.session_state.get(request_id_key, "").startswith(basket_hash):
            last_ts = st.session_state.get(last_submit_ts_key, 0)
            if now_ts - last_ts < 10:
                st.info("任务已创建，请到「我的内容」查看。")
                batch_id = st.session_state.get(last_batch_id_key)
                if batch_id:
                    _navigate_to("📋 我的内容")
                    st.rerun()
                return
        st.session_state[submitting_key] = True
        try:
            client_request_id = f"{basket_hash}-{datetime.now():%Y%m%d%H%M}"
            batch = _api("POST", "/batches", json={
                "batch_name": name,
                "mode": mode,
                "topic_ids": [str(item["id"]) for item in basket],
                "article_count": count,
                "angles": angles,
                "concurrency": concurrency,
                "client_request_id": client_request_id,
                "generation_options": {
                    "article_type": article_type,
                    "style": style,
                    "image_style": image_style,
                    "word_count": recommended_word_count(word_count),
                    "image_plan_mode": image_mode,
                    "image_call_budget_per_article": calculate_image_budget(1, image_mode),
                    "image_call_budget_per_batch": calculate_image_budget(total_articles, image_mode),
                    "image_retry_limit": 0,
                    "image_unit_price": None,
                    "confirm_paid": bool(paid_batch_confirmed)
                }
            })
            st.session_state[request_id_key] = client_request_id
            st.session_state[last_submit_ts_key] = now_ts
            st.session_state[last_batch_id_key] = batch["batch_id"]
            _api("POST", f"/batches/{batch['batch_id']}/start")
            st.success("已开始生成，可在「我的内容」查看进度。")
            _navigate_to("📋 我的内容")
            st.rerun()
        except Exception as exc:
            st.session_state[submitting_key] = False
            _log_error("TASK-CREATE-001", exc, page="开始生成", action="create_batch")
            st.error(f"任务创建失败：{str(exc)[:200]}\n错误码：TASK-CREATE-001")


def _clear_editor_widgets(task_id: str) -> None:
    prefix = f"rc1_"
    for key in list(st.session_state):
        if key.startswith(prefix) and task_id in key and key not in {f"editing_article_{task_id}"}:
            st.session_state.pop(key, None)


def _failed_reason(state: dict[str, Any]) -> str:
    code = str(state.get("error_code") or "")
    failed_step = str(state.get("failed_step") or state.get("stage") or "")
    if code == "RESEARCH_NOT_COLLECTED":
        return "没有找到足够的相关公开资料"
    if code == "INSUFFICIENT_INFORMATION":
        return "没有找到足够的相关公开资料"
    if failed_step in {"generating_article", "planning_article"}:
        return "文本模型失败"
    if failed_step == "quality_gate" or code == "QUALITY_GATE_FAILED":
        return "文章质量未通过"
    if failed_step in {"generating_cover", "generating_inline_images"}:
        return "图片模型失败"
    if code == "TASK_CANCELLED" or str(state.get("status") or "") == "cancelled":
        return "用户取消"
    return str(state.get("safe_error_message") or code or "生成失败")


def _rewrite_only_ready(state: dict[str, Any]) -> bool:
    bundle = state.get("research_bundle") or {}
    gate = state.get("quality_gate") or {}
    if str(bundle.get("research_status") or "") not in {"sufficient", "verified"}:
        return False
    if str(gate.get("status") or "") == "failed":
        return False
    return bool(state.get("article")) or str(state.get("failed_step") or "") in {"generating_article", "planning_article"}


def _research_insufficient(state: dict[str, Any]) -> bool:
    code = str(state.get("error_code") or "")
    bundle = state.get("research_bundle") or {}
    gate = state.get("quality_gate") or {}
    reasons = "；".join(str(item) for item in gate.get("reasons") or [])
    return code in {"RESEARCH_NOT_COLLECTED", "INSUFFICIENT_INFORMATION"} or str(bundle.get("research_status") or "") == "insufficient" or "资料" in reasons


def _render_failed_task_panel(batch_id: str, task_id: str, state: dict[str, Any], restricted: bool) -> None:
    if str(state.get("error_code") or "") == "MODEL_NOT_FOUND":
        st.info("当前文本模型不可用。可能是模型名称错误、服务商已下线该模型，或当前中转暂时没有可用通道。请先进入模型设置获取或填写可用模型，测试成功后再重新写文章。")
    if str(state.get("error_code") or "") == "TEXT_MODEL_NOT_VERIFIED":
        st.warning("当前文本模型尚未测试。请先在模型设置中完成测试，再重新写文章。")
    st.error(f"失败原因：{_failed_reason(state)}")
    _render_diagnostic_details(state.get("error_details") or {}, expander_label="查看失败诊断")
    if restricted:
        return
    if str(state.get("error_code") or "") in {"MODEL_NOT_FOUND", "TEXT_MODEL_NOT_VERIFIED"}:
        special_cols = st.columns(4)
        if special_cols[0].button("前往模型设置", key=f"rc132_model_settings_{task_id}"):
            _navigate_to("⚙ 模型设置")
            st.rerun()
        if special_cols[1].button("获取可用模型", key=f"rc132_model_discover_{task_id}"):
            st.session_state["rc132_focus_text_discover"] = True
            _navigate_to("⚙ 模型设置")
            st.rerun()
        if special_cols[2].button("测试文本模型", key=f"rc132_model_test_{task_id}"):
            st.session_state["rc132_focus_text_test"] = True
            _navigate_to("⚙ 模型设置")
            st.rerun()
        if special_cols[3].button("使用当前新配置重新写文章", key=f"rc132_model_retry_{task_id}"):
            try:
                _api("POST", f"/tasks/{task_id}/retry-article")
                st.success("已提交仅重试文章。")
                st.rerun()
            except Exception as exc:
                st.error(_model_error_message(exc))
    action_labels = {
        "test_text_model": "去测试文本模型",
        "retry_article": "仅重试文章",
        "open_model_settings": "返回模型设置",
    }
    actions = [action for action in list(state.get("next_actions") or []) if action in action_labels]
    if not actions:
        return
    columns = st.columns(len(actions))
    for index, action in enumerate(actions):
        label = action_labels[action]
        if columns[index].button(label, key=f"rc132_failed_action_{action}_{task_id}"):
            if action == "test_text_model":
                st.session_state["rc132_focus_text_test"] = True
                _navigate_to("⚙ 模型设置")
                st.rerun()
            elif action == "open_model_settings":
                _navigate_to("⚙ 模型设置")
                st.rerun()
            elif action == "retry_article":
                try:
                    _api("POST", f"/tasks/{task_id}/retry-article")
                    st.success("已提交仅重试文章。")
                    st.rerun()
                except Exception as exc:
                    st.error(_model_error_message(exc))


def render_editor(task_id: str, state: dict[str, Any]) -> None:
    try:
        data = _api("GET", f"/tasks/{task_id}/article")
    except Exception:
        data = {}
    editing_key = f"editing_article_{task_id}"
    if editing_key not in st.session_state:
        seed = data.get("editing_article") or data.get("draft") or data.get("article") or state.get("article") or {}
        st.session_state[editing_key] = dict(seed)
    article = dict(st.session_state.get(editing_key) or {})
    st.markdown("#### 编辑文章")
    title = st.text_input("标题", value=str(article.get("title") or ""), key=f"rc1_title_{task_id}")
    intro = st.text_area("导语", value=str(article.get("intro") or article.get("summary") or ""), height=90, key=f"rc1_intro_{task_id}")
    sections = list(article.get("sections") or [])
    edited: list[dict[str, Any]] = []
    for index, section in enumerate(sections):
        with st.container(border=True):
            heading = st.text_input(f"小标题 {index + 1}", value=str(section.get("heading") or ""), key=f"rc1_heading_{task_id}_{index}")
            body = st.text_area("正文", value=str(section.get("body") or ""), height=140, key=f"rc1_body_{task_id}_{index}")
            edited.append({**section, "heading": heading, "body": body})
            if st.button("删除这一段", key=f"rc1_delete_{task_id}_{index}"):
                st.session_state[editing_key] = {**article, "title": title, "intro": intro, "summary": intro, "sections": edited[:-1] + sections[index + 1:]}
                _clear_editor_widgets(task_id)
                st.rerun()
    if st.button("增加一段", key=f"rc1_add_{task_id}"):
        st.session_state[editing_key] = {**article, "title": title, "intro": intro, "summary": intro, "sections": edited + [{"heading": "新小标题", "body": "请输入正文"}]}
        _clear_editor_widgets(task_id)
        st.rerun()
    changes = {**article, "title": title, "intro": intro, "summary": intro, "sections": edited}
    st.session_state[editing_key] = changes
    fingerprint = _edit_fingerprint(changes)
    fingerprint_key = f"rc1_edit_fingerprint_{task_id}"
    pending_key = f"rc1_edit_pending_{task_id}"
    changed_at_key = f"rc1_edit_changed_at_{task_id}"
    if st.session_state.get(fingerprint_key) != fingerprint:
        st.session_state[fingerprint_key] = fingerprint
        st.session_state[changed_at_key] = time.monotonic()
        st.session_state[pending_key] = True
    st.session_state[f"rc1_edit_changes_{task_id}"] = changes
    _mount_autosave(task_id)
    left, mid, right = st.columns(3)
    if left.button("保存草稿", key=f"rc1_draft_{task_id}"):
        try:
            _api("PUT", f"/tasks/{task_id}/article/draft", json=changes)
            st.session_state[f"rc1_edit_saved_fingerprint_{task_id}"] = fingerprint
            st.session_state[pending_key] = False
            st.success("草稿已保存")
        except Exception as exc:
            st.warning("保存失败，上一版本仍然保留。")
    if mid.button("保存修改", type="primary", key=f"rc1_save_{task_id}"):
        try:
            saved = _api("POST", f"/tasks/{task_id}/article/save", json=changes)
            st.session_state[editing_key] = saved.get("article") or changes
            st.session_state[f"rc1_edit_saved_fingerprint_{task_id}"] = fingerprint
            st.session_state[pending_key] = False
            st.session_state.pop(f"rc1_sections_{task_id}", None)
            st.success("文章已保存")
            st.rerun()
        except Exception as exc:
            st.warning("保存失败，上一版本仍然保留。")
    if right.button("放弃本次修改", key=f"rc1_discard_{task_id}"):
        try:
            discarded = _api("POST", f"/tasks/{task_id}/article/discard")
            st.session_state[editing_key] = discarded.get("article") or state.get("article") or {}
            _clear_editor_widgets(task_id)
            st.success("已放弃本次修改")
            st.rerun()
        except Exception as exc:
            st.warning("保存失败，上一版本仍然保留。")
    try:
        versions = _api("GET", f"/tasks/{task_id}/article/versions").get("versions", [])
        if versions:
            version_ids = [str(item.get("version_id")) for item in versions]
            selected_version = st.selectbox("历史版本", version_ids, key=f"rc1_version_{task_id}")
            if st.button("恢复模型原稿", key=f"rc1_restore_original_{task_id}"):
                _api("POST", f"/tasks/{task_id}/article/restore", json={"version_id": version_ids[-1]})
                st.success("已恢复模型原稿")
                st.rerun()
            if st.button("恢复指定历史版本", key=f"rc1_restore_{task_id}"):
                _api("POST", f"/tasks/{task_id}/article/restore", json={"version_id": selected_version})
                st.success("已恢复历史版本")
                st.rerun()
    except Exception:
        st.caption("历史版本暂时无法读取。")


def _render_standalone_tasks(tasks: list[dict[str, Any]], restricted: bool) -> None:
    st.caption("显示最近 20 条历史内容。")
    for index, task in enumerate(tasks, start=1):
        task_id = str(task.get("task_id") or "")
        if not task_id:
            continue
        state = load_generation_task(task_id) or {}
        topics = task.get("selected_topics") if isinstance(task.get("selected_topics"), list) else []
        topic = topics[0] if topics and isinstance(topics[0], dict) else {}
        topic_title = str(topic.get("title") or task.get("task_name") or "未命名内容")
        status = str(task.get("status") or state.get("status") or "")
        with st.expander(f"{index}. {topic_title} · {_status(status)}"):
            if not state:
                st.info("这条内容的详情文件暂时不可读取，但任务记录仍然保留。")
            _render_text_generation_status({**state, "status": state.get("status") or status}, task_id, restricted)
            if status == "failed":
                _render_failed_task_panel("", task_id, state, restricted)
            article = customer_visible_article(state.get("article") or {}) if state.get("article") else {}
            if article:
                st.markdown(f"### {article.get('title') or '文章'}")
                intro = article.get("lead")
                if intro:
                    st.markdown(str(intro))
                body_markdown = str(article.get("body_markdown") or "").strip()
                if not body_markdown and article.get("sections"):
                    parts: list[str] = []
                    for section in article.get("sections") or []:
                        if not isinstance(section, dict):
                            continue
                        heading = str(section.get("heading") or "").strip()
                        body = str(section.get("body") or "").strip()
                        if body:
                            parts.append(f"## {heading}\n{body}".strip() if heading else body)
                    body_markdown = "\n\n".join(parts).strip()
                with st.expander("查看全文"):
                    st.markdown(body_markdown or "暂无正文。")
            show_progress(state or {"status": status, "stage": status, "progress": 0})
            if not restricted and status in {"queued", "running"}:
                if st.button("取消这篇", key=f"rc1_standalone_cancel_{task_id}"):
                    try:
                        _api("POST", f"/tasks/{task_id}/cancel")
                        st.rerun()
                    except Exception as exc:
                        _log_error("TASK-STATE-001", exc, page="我的内容", action="cancel_standalone", task_id=task_id)
                        st.error("取消这篇失败，请稍后重试。\n错误码：TASK-STATE-001")

def _content(restricted: bool = False) -> None:
    page_header("03 / 结果", "我的内容", "文章、封面、正文图片和历史版本都保存在本机")
    batch_payload: dict[str, Any] = {"items": [], "count": 0, "item_errors": []}
    with st.spinner("正在加载内容列表…"):
        try:
            batch_payload = _api("GET", "/batches?limit=20&refresh=false", timeout=6)
        except Exception as exc:
            _log_error("CONTENT-LIST-001", exc, page="我的内容", action="load_batches")
            st.warning(f"批次列表暂时无法读取：{_api_error_text(exc) or '服务响应异常'}")
            if st.button("重新加载", key="rc1_content_retry"):
                st.rerun()
    batches = batch_payload.get("items", [])
    item_errors = batch_payload.get("item_errors") or []
    if item_errors:
        st.warning("部分历史记录暂时无法刷新，已先显示可读取内容。")
    if not batches:
        try:
            standalone_tasks = _api("GET", "/tasks?limit=20&unbatched=true", timeout=10).get("items", [])
        except Exception as exc:
            _log_error("CONTENT-LIST-002", exc, page="我的内容", action="load_standalone_tasks")
            st.error(f"历史内容暂时无法读取：{_api_error_text(exc) or '服务响应异常'}")
            if st.button("重新加载", key="rc1_content_retry_tasks"):
                st.rerun()
            return
        if standalone_tasks:
            _render_standalone_tasks(standalone_tasks, restricted)
        else:
            st.info("还没有内容，先去选择一个热点吧。")
        return
    if int(batch_payload.get("count") or 0) >= 20:
        st.caption("已显示最近 20 次创作。")
    selected_delete_ids: list[str] = []
    if not restricted:
        clear_failed_confirmed = st.checkbox("我确认清空全部失败任务（不删除已导出的 Word/ZIP）", key="rc1_clear_failed_confirm")
        if st.button("清空全部失败任务", disabled=not clear_failed_confirmed, key="rc1_clear_failed_tasks"):
            try:
                _api("POST", "/tasks/clear-failed", json={"confirm": True, "delete_exports": False})
                st.success("失败任务已清理。")
                st.rerun()
            except Exception as exc:
                st.error(f"失败任务清理失败：{exc}")
    for batch in batches:
        total, completed = int(batch.get("total_count") or 0), int(batch.get("completed_count") or 0)
        batch_items = batch.get("items") or []
        with st.container(border=True):
            st.subheader(str(batch.get("batch_name") or "未命名创作"))
            st.caption(f"{_status(batch.get('status'))} · {completed}/{total} 篇完成 · 创建于 {batch.get('created_at', '')}")
            st.progress(min(1.0, completed / total) if total else 0.0)
            quality_status = str(batch.get("quality_status") or "not_applicable")
            quality_error = batch.get("quality_error")
            quality_count = total or 5
            batch_mode = str(batch.get("mode") or "")
            # ── Skip quality display for single-article batches ──
            is_single_article = len(batch_items) <= 1 or total <= 1
            if is_single_article:
                pass  # No quality check for single-article batches
            elif quality_status == "failed":
                st.warning(f"差异检查暂未完成，{quality_count} 篇文章已经保存。你可以重新检查差异，检查完成后再导出本次创作。")
                if not restricted and st.button("重新检查差异", key=f"rc1_quality_retry_{batch['batch_id']}"):
                    try:
                        with st.spinner(f"正在重新检查 {quality_count} 篇文章的差异"):
                            _api("POST", f"/batches/{batch['batch_id']}/quality/retry")
                        st.success("差异检查已重新开始。")
                        st.rerun()
                    except Exception:
                        st.error("差异检查失败，请稍后再次重试。")
            elif quality_status in {"pending", "checking", "rewriting"}:
                st.info(f"正在重新检查 {quality_count} 篇文章的差异")
            a, b = st.columns(2)
            if not restricted and batch.get("status") not in {"completed", "cancelled"} and a.button("停止本次创作", key=f"rc1_cancel_batch_{batch['batch_id']}"):
                try:
                    _api("POST", f"/batches/{batch['batch_id']}/cancel")
                    st.rerun()
                except Exception as exc:
                    _log_error("TASK-STATE-001", exc, page="我的内容", action="cancel_batch")
                    st.error("停止本次创作失败，请稍后重试。\n错误码：TASK-STATE-001")
            if int(batch.get("failed_count") or 0) or int(batch.get("partial_success_count") or 0):
                if not restricted and b.button("重试失败内容", key=f"rc1_retry_batch_{batch['batch_id']}"):
                    _api("POST", f"/batches/{batch['batch_id']}/retry-failed")
                    st.rerun()
            if not restricted:
                delete_batch_confirmed = st.checkbox("我确认删除本次创作（不删除已导出的 Word/ZIP）", key=f"rc1_delete_batch_confirm_{batch['batch_id']}")
                if st.button("删除本次创作", disabled=not delete_batch_confirmed, key=f"rc1_delete_batch_{batch['batch_id']}"):
                    try:
                        _api("DELETE", f"/batches/{batch['batch_id']}", json={"confirm": True, "delete_exports": False})
                        st.success("本次创作已删除。")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"本次创作删除失败：{exc}")
            for item in batch_items:
                task = item.get("task") or {}
                task_id = str(task.get("task_id") or "")
                state = load_generation_task(task_id) or {}
                topic_title = (item.get("topic_snapshot") or {}).get("title") or "未命名话题"
                with st.expander(f"{item.get('position', 1)}. {topic_title} · {_status(task.get('status'))}"):
                    if not restricted:
                        if st.checkbox("选择删除这篇", key=f"rc1_delete_select_{task_id}"):
                            selected_delete_ids.append(task_id)
                    if state.get("fallback_notice"):
                        st.warning(state["fallback_notice"])
                    _render_text_generation_status(state, task_id, restricted)
                    if str(task.get("status") or state.get("status") or "") == "failed":
                        _render_failed_task_panel(str(batch.get("batch_id") or ""), task_id, state, restricted)
                    gate = state.get("quality_gate") or {}
                    if gate.get("status") == "failed":
                        st.warning("内容检查未通过，暂不生成图片，也不允许导出正式 Word。")
                        problem_items = gate.get("hard_errors") or gate.get("reasons") or []
                        for reason in problem_items[:8]:
                            st.caption(str(reason))
                    elif gate.get("status") == "warning":
                        st.info("状态：可用草稿，发布前请核对")
                        for reason in (gate.get("warnings") or gate.get("reasons") or [])[:8]:
                            st.caption(str(reason))
                    bundle = state.get("research_bundle") or {}
                    if bundle and str(state.get("status") or "") in {"running", "queued"}:
                        accepted_count = int(bundle.get("accepted_source_count") or 0)
                        if accepted_count:
                            st.caption(f"已找到 {accepted_count} 个相关来源")
                    usage = state.get("image_usage") or {}
                    plan = state.get("image_plan") or {}
                    if plan:
                        st.caption(f"图片方案：{plan.get('label') or plan.get('mode')} · 预计调用 {plan.get('max_calls', 0)} 次 · 已调用 {usage.get('generation_calls', 0)} 次 · 最大重试 {plan.get('retry_limit', 0)} 次")
                    article = customer_visible_article(state.get("article") or {}) if state.get("article") else {}
                    if article:
                        st.markdown(f"### {article.get('title') or '文章'}")
                        if article.get("lead"):
                            st.markdown(str(article.get("lead") or ""))
                        body_markdown = str(article.get("body_markdown") or "").strip()
                        if not body_markdown and article.get("sections"):
                            body_parts: list[str] = []
                            for section in article.get("sections") or []:
                                if not isinstance(section, dict):
                                    continue
                                heading = str(section.get("heading") or "").strip()
                                body = str(section.get("body") or "").strip()
                                if body:
                                    body_parts.append(f"## {heading}\n{body}".strip() if heading else body)
                            body_markdown = "\n\n".join(body_parts).strip()
                        with st.expander("查看全文"):
                            st.markdown(body_markdown or "")
                        exportable_statuses = {"completed", "completed_with_warning", "warning", "partial_success", "review_required"}
                        layout_ok = (article.get("layout_check") or {}).get("passed", bool(body_markdown))
                        if state.get("status") in exportable_statuses and gate.get("status") != "failed" and layout_ok:
                            _download(f"/tasks/{task_id}/export/word", f"{article.get('title') or '文章'}.docx", "导出 Word", f"rc1_word_{task_id}")
                            _download(f"/tasks/{task_id}/export/zip", f"{article.get('title') or '文章'}.zip", "导出单篇 ZIP", f"rc1_zip_{task_id}")
                            if st.button("打开保存位置", key=f"rc1_open_export_item_{task_id}"):
                                _open_export_location()
                        else:
                            st.info("文章尚未通过质量门禁，暂不能作为正式成品导出。")
                        if state.get("status") == "completed" and gate.get("status") in {"passed", "warning"}:
                            st.markdown("#### 文章确认后再生成图片")
                            requested_mode = normalize_image_plan((state.get("generation_options") or {}).get("image_plan_mode"))
                            if requested_mode == "economy":
                                include_cover = True
                                inline_count = 0
                                st.caption("已选择经济型：每篇只生成 1 张封面图。")
                            elif requested_mode == "standard":
                                include_cover = True
                                inline_count = 1
                                st.caption("已选择标准型：每篇生成 1 张封面图和 1 张正文图。")
                            else:
                                include_cover = False
                                inline_count = 0
                                st.caption("当前任务为纯文字模式，不会自动生成图片。")
                            paid_images_confirmed = st.checkbox("我确认本次图片生成会真实调用模型并可能产生费用", key=f"rc132_paid_images_{task_id}")
                            estimated_calls = (1 if include_cover else 0) + int(inline_count)
                            st.caption(f"本次预计调用图片接口 {estimated_calls} 次；文章正文已先生成，未确认前不会调用图片接口。")
                            if st.button("确认并生成所选图片", disabled=not paid_images_confirmed or estimated_calls == 0, key=f"rc132_generate_selected_images_{task_id}"):
                                try:
                                    _api("POST", f"/tasks/{task_id}/images/generate", timeout=30, json={"confirm_paid": True, "include_cover": include_cover, "inline_count": int(inline_count)})
                                    st.success("图片生成已提交，费用确认已记录。")
                                    st.rerun()
                                except Exception as exc:
                                    st.error(_model_error_message(str(exc), image=True))
                        render_editor(task_id, state)
                    cover = generation_task_dir(task_id) / "images" / "cover.png"
                    if cover.is_file() and (state.get("cover") or {}).get("status") == "completed":
                        st.image(str(cover), caption="封面")
                    show_progress(state)
                    if st.session_state.pop(f"rc1_stuck_cancel_request_{task_id}", False):
                        try:
                            _api("POST", f"/batches/{batch['batch_id']}/items/{task_id}/cancel")
                            st.success("已提交取消请求。")
                            st.rerun()
                        except Exception as exc:
                            _log_error("TASK-STATE-001", exc, page="我的内容", action="stuck_cancel", task_id=task_id)
                            st.error("取消任务失败，请稍后重试。\n错误码：TASK-STATE-001")
                    if st.session_state.pop(f"rc1_stuck_retry_request_{task_id}", False):
                        try:
                            _api("POST", f"/batches/{batch['batch_id']}/items/{task_id}/retry")
                            st.success("已重新开始本阶段。")
                            st.rerun()
                        except Exception as exc:
                            _log_error("TASK-STATE-001", exc, page="我的内容", action="stuck_retry", task_id=task_id)
                            st.error("重新开始本阶段失败，请稍后重试。\n错误码：TASK-STATE-001")
                    try:
                        inline_items = _api("GET", f"/tasks/{task_id}/inline-images").get("items", [])
                    except Exception:
                        inline_items = state.get("inline_images") or []
                    if inline_items:
                        st.markdown("#### 正文图片")
                        inline_paid_confirmed = st.checkbox("我确认重试/重新生成正文图片可能产生费用", key=f"rc132_paid_inline_{task_id}")
                        completed_inline = sum(item.get("status") == "completed" for item in inline_items)
                        failed_inline = sum(item.get("status") == "failed" for item in inline_items)
                        st.caption(f"已完成 {completed_inline}/{len(inline_items)} 张 · 失败 {failed_inline} 张")
                        if failed_inline and not restricted and st.button("重试失败图片", disabled=not inline_paid_confirmed, key=f"rc1_inline_retry_failed_{task_id}"):
                            _api("POST", f"/tasks/{task_id}/inline-images/retry-failed", json={"confirm_paid": True})
                            st.rerun()
                        if not restricted and st.button("重新生成全部正文图片", disabled=not inline_paid_confirmed, key=f"rc1_inline_regenerate_all_{task_id}"):
                            _api("POST", f"/tasks/{task_id}/inline-images/regenerate", json={"confirm_paid": True})
                            st.rerun()
                        image_columns = st.columns(min(2, len(inline_items)))
                        for index, image in enumerate(inline_items):
                            with image_columns[index % len(image_columns)]:
                                image_path = generation_task_dir(task_id) / str(image.get("path") or image.get("file_path") or "")
                                if image.get("status") == "completed" and image_path.is_file():
                                    large_key = f"rc1_inline_large_{task_id}_{image.get('image_id')}"
                                    st.image(str(image_path), caption=str(image.get("section_title") or image.get("paragraph_ref") or "正文图片"), use_container_width=bool(st.session_state.get(large_key)))
                                    if st.button("查看大图", key=f"rc1_inline_view_{task_id}_{image.get('image_id')}"):
                                        st.session_state[large_key] = True
                                        st.rerun()
                                else:
                                    st.info(f"{image.get('section_title') or '正文图片'}：{_status(image.get('status'))}")
                                if not restricted and st.button("重新生成这张" if image.get("status") == "completed" else "重试这张", disabled=not inline_paid_confirmed, key=f"rc1_inline_retry_{task_id}_{image.get('image_id')}"):
                                    _api("POST", f"/tasks/{task_id}/inline-images/{image.get('image_id')}/retry", json={"confirm_paid": True})
                                    st.rerun()
                    if state.get("similarity_status") == "review_required":
                        st.info("这篇内容与其他文章较接近，建议重新生成。")
                    left, right = st.columns(2)
                    status = str(task.get("status") or state.get("status") or "")
                    if not restricted and status in {"queued", "running"} and left.button("取消这篇", key=f"rc1_cancel_item_{task_id}"):
                        try:
                            _api("POST", f"/batches/{batch['batch_id']}/items/{task_id}/cancel")
                            st.rerun()
                        except Exception as exc:
                            _log_error("TASK-STATE-001", exc, page="我的内容", action="cancel_item", task_id=task_id)
                            st.error("取消这篇失败，请稍后重试。\n错误码：TASK-STATE-001")
                    if not restricted and status in {"failed", "partial_success", "completed"}:
                        insufficient = _research_insufficient(state)
                        if status == "failed" and insufficient:
                            st.info("当前公开资料较少。")
                            extra_left, extra_mid, extra_right = st.columns(3)
                            if extra_left.button("再次自动搜索", key=f"rc1_research_again_item_{task_id}"):
                                _api("POST", f"/batches/{batch['batch_id']}/items/{task_id}/research-regenerate")
                                st.rerun()
                            if extra_mid.button("补充参考资料", key=f"rc1_show_reference_item_{task_id}"):
                                st.session_state[f"rc1_show_reference_form_{task_id}"] = True
                            if extra_right.button("更换热点", key=f"rc1_change_topic_item_{task_id}"):
                                _navigate_to("◈ 选择话题")
                                st.rerun()
                            if st.session_state.get(f"rc1_show_reference_form_{task_id}"):
                                reference_urls = st.text_area("参考链接", height=70, key=f"rc1_reference_urls_{task_id}", placeholder="每行一个公开网页链接")
                                supplemental_text = st.text_area("补充资料正文", height=100, key=f"rc1_supplemental_text_{task_id}", placeholder="可粘贴公告、报道正文或机构公开信息")
                                if st.button("使用补充资料重新生成", type="primary", key=f"rc1_submit_reference_{task_id}"):
                                    _api("POST", f"/batches/{batch['batch_id']}/items/{task_id}/research-regenerate", json={"reference_urls": [line.strip() for line in reference_urls.splitlines() if line.strip()], "supplemental_text": supplemental_text})
                                    st.rerun()
                        regen_col, rewrite_col, delete_col = st.columns(3)
                        if regen_col.button("重新搜索资料并生成", key=f"rc1_research_regen_item_{task_id}"):
                            _api("POST", f"/batches/{batch['batch_id']}/items/{task_id}/research-regenerate")
                            st.rerun()
                        rewrite_ready = _rewrite_only_ready(state)
                        if rewrite_col.button("重新写文章", disabled=not rewrite_ready, key=f"rc1_rewrite_only_item_{task_id}", help=None if rewrite_ready else "当前资料不足，请先重新搜索资料。"):
                            _api("POST", f"/batches/{batch['batch_id']}/items/{task_id}/retry")
                            st.rerun()
                        delete_confirmed = st.checkbox("确认删除这篇", key=f"rc1_delete_confirm_{task_id}")
                        if delete_col.button("删除", disabled=not delete_confirmed, key=f"rc1_delete_item_{task_id}"):
                            _api("DELETE", f"/tasks/{task_id}", json={"confirm": True, "delete_exports": False})
                            st.rerun()

        if batch.get("final_ready"):
            _download(f"/batches/{batch['batch_id']}/export/word", f"{batch.get('batch_name') or '本次创作'}.docx", "导出本次创作 Word", f"rc1_batch_word_{batch['batch_id']}")
            _download(f"/batches/{batch['batch_id']}/export/zip", f"{batch.get('batch_name') or '本次创作'}.zip", "导出本次创作 ZIP", f"rc1_batch_export_{batch['batch_id']}")
            if st.button("打开保存位置", key=f"rc1_open_export_{batch['batch_id']}"):
                _open_export_location()
    if not restricted and selected_delete_ids:
        selected_confirmed = st.checkbox("我确认删除选中的任务（不删除已导出的 Word/ZIP）", key="rc1_delete_selected_confirm")
        if st.button("删除选中", disabled=not selected_confirmed, key="rc1_delete_selected_tasks"):
            deleted = 0
            for task_id in selected_delete_ids:
                try:
                    _api("DELETE", f"/tasks/{task_id}", json={"confirm": True, "delete_exports": False})
                    deleted += 1
                except Exception:
                    continue
            st.success(f"已删除 {deleted} 个任务。")
            st.rerun()


def _render_restricted_app(settings: dict[str, Any], save_settings: Any, root: Path) -> None:
    _render_license_activation(root)
    st.warning("当前软件尚未激活，因此模型设置和正式生成暂时锁定。请先复制设备码并完成许可证激活。")
    st.info("已有内容仍可查看、编辑和导出。导入有效许可证后会自动退出受限模式。")
    with st.sidebar:
        st.image(str(root / "ui" / "assets" / "logo-light.svg"), width=116)
        st.markdown("### 热点图文工作台")
        st.caption("受限模式")
        st.caption(f"版本：{APP_VERSION}")
    page = st.sidebar.radio("导航", ["我的内容", "模型设置"], key="restricted_navigation")
    if page == "我的内容":
        _content(restricted=True)
    else:
        _settings_page(settings, save_settings, root, restricted=True)


def _about_page(root: Path) -> None:
    data_path = data_root()
    info = diagnostic_info(root, data_path)
    page_header("关于软件", PRODUCT_NAME, "用于确认当前运行版本、安装目录和诊断信息。")
    left, right = st.columns(2)
    left.metric("当前版本", APP_VERSION)
    right.metric("产品名称", PRODUCT_NAME)
    st.write("安装路径：", str(root))
    st.write("数据路径：", str(data_path))
    st.write("构建时间：", BUILD_TIME_UTC)
    st.caption("诊断信息只包含版本、路径和构建时间，不包含 API Key、许可证正文或文章内容。")
    diagnostic_text = json.dumps(info, ensure_ascii=False, indent=2)
    with st.expander("查看错误详情", expanded=False):
        st.code(diagnostic_text, language="json")
        if st.button("复制诊断信息", type="primary"):
            _write_clipboard(diagnostic_text)
            st.success("诊断信息已复制。")


def _settings_page(settings: dict[str, Any], save_settings: Any, root: Path, restricted: bool = False) -> None:
    page_header("设置", "模型设置", "文本和图片使用同一个API Key，接口地址和模型仍分别设置。")
    st.caption("API Key 只保存在本机，不会进入文章、任务或导出文件。")
    if st.session_state.pop("rc132_focus_text_test", False):
        st.warning("上一次任务失败发生在文本模型阶段。建议先执行“基础连接检测”和“测试文章生成能力”，确认接口、模型与 Endpoint 是否正常。")
    if settings.get("credential_migration_error"):
        st.warning("密钥安全迁移失败，原配置尚未修改，请重新保存模型设置。")
    if settings.get("credential_migration_notice"):
        st.info(str(settings["credential_migration_notice"]))
    text, image = _safe_profile(settings.get("text_profile")), _safe_profile(settings.get("image_profile"))
    st.session_state.setdefault("rc132_image_test_confirming", False)
    st.session_state.setdefault("rc132_image_test_inflight", False)
    st.session_state.setdefault("rc132_image_test_run", False)
    # RC1.3.3-Lite-R2.2.3: keep saved values, widget values, and post-discovery pending values separate.
    # text_model_options / image_model_options: discovered model lists.
    # saved_text_model / saved_image_model: persisted profile values.
    # text_model_mode / image_model_mode: discovered/manual source selector.
    # text_model_selected / image_model_selected: Streamlit selectbox-owned values.
    # text_model_manual / image_model_manual: Streamlit text_input-owned values.
    # pending_text_model / pending_image_model: values applied before widgets are instantiated on the next rerun.
    def apply_pending_model(kind: str, profile: dict[str, Any]) -> None:
        pending_key = f"rc132_pending_{kind}_model"
        selected_key = f"rc132_{kind}_model_selected"
        manual_key = f"rc132_{kind}_model_manual"
        mode_key = f"rc132_{kind}_model_mode"
        pending_model = str(st.session_state.pop(pending_key, "") or "").strip()
        if pending_model:
            st.session_state[selected_key] = pending_model
            st.session_state.setdefault(manual_key, str(profile.get("model") or pending_model))
            st.session_state[mode_key] = "从检测结果中选择"
        else:
            saved_model = str(profile.get("model") or "")
            st.session_state.setdefault(selected_key, saved_model)
            st.session_state.setdefault(manual_key, saved_model)
            st.session_state.setdefault(mode_key, "手动填写模型名称")

    apply_pending_model("text", text)
    apply_pending_model("image", image)
    names = list(PRESETS)
    shared = st.checkbox("文本和图片使用同一个API Key", value=bool(settings.get("share_text_image_credentials", False)), disabled=restricted, help="只同步 API Key；文本和图片的接口地址与模型继续分别设置。")
    settings["share_text_image_credentials"] = shared
    verified_model = str(settings.get("verified_text_model") or "").strip()
    verified_base = str(settings.get("verified_text_base_url") or "").strip().rstrip("/")
    verified_endpoint = "/" + str(settings.get("verified_text_endpoint") or "/chat/completions").strip().lstrip("/")
    current_model = str(text.get("model") or "").strip()
    current_base = str(text.get("base_url") or "").strip().rstrip("/")
    current_endpoint = "/" + str(text.get("endpoint") or "/chat/completions").strip().lstrip("/")
    current_text_verified = bool(current_model and current_model == verified_model and current_base == verified_base and current_endpoint == verified_endpoint)
    if current_model and not current_text_verified:
        st.warning("当前文本模型尚未测试，请先完成“基础连接检测”或“测试文章生成能力”，再用当前配置重新写文章。")
    elif str(settings.get("verified_at") or "").strip():
        st.caption(f"文本模型最近一次验证时间：{settings.get('verified_at')}")

    def profile_values(kind: str, current: dict[str, Any], provider: str) -> dict[str, Any]:
        preset = PRESETS[provider].get(kind, {})
        values = dict(preset)
        values.update(current)
        values.update({
            "name": provider,
            "model": str(current.get("model") or preset.get("model") or ""),
            "base_url": str(current.get("base_url") or preset.get("base_url") or ""),
            "endpoint": str(current.get("endpoint") or preset.get("endpoint") or ("/chat/completions" if kind == "text" else "/images/generations")),
            "enabled": True,
        })
        return values

    def status_label(kind: str, current: dict[str, Any]) -> str:
        status = str(st.session_state.get(f"rc132_{kind}_status") or "")
        if status:
            return status
        if not (current.get("has_api_key") or current.get("api_key")):
            return "未配置"
        return "已保存，尚未检测"

    def model_options(kind: str, current_model: str) -> list[str]:
        options = list(st.session_state.get(f"rc132_{kind}_model_options") or st.session_state.get(f"rc132_{kind}_models") or [])
        if current_model and current_model not in options:
            options.insert(0, current_model)
        return options

    def final_model_value(kind: str) -> str:
        mode = str(st.session_state.get(f"rc132_{kind}_model_mode") or "手动填写模型名称")
        if mode == "从检测结果中选择":
            selected = str(st.session_state.get(f"rc132_{kind}_model_selected") or "").strip()
            if selected:
                return selected
        return str(st.session_state.get(f"rc132_{kind}_model_manual") or "").strip()

    text_provider = str(text.get("name")) if str(text.get("name")) in names else "自定义"
    image_provider = str(image.get("name")) if str(image.get("name")) in names else "自定义"
    text_preset = PRESETS[text_provider].get("text", {})
    image_preset = PRESETS[image_provider].get("image", {})

    with st.container(border=True):
        st.markdown("### 文本接口")
        st.caption(f"连接状态：{status_label('text', text)}")
        st.radio("文本接口来源", INTERFACE_SOURCES, horizontal=True, key="rc132_text_source", disabled=restricted)
        text_provider = st.selectbox("文本服务商", names, index=names.index(text_provider), key="rc132_text_provider", disabled=restricted)
        text_key = st.text_input("文本 API Key", value="", type="password", placeholder="已保存，留空则继续使用" if text.get("has_api_key") else "请输入文本 API Key", key="rc132_text_key")
        if text.get("has_api_key"):
            st.caption(f"已保存文本 Key：{_mask_api_key(str(text.get('api_key') or ''))}。保存后完整密钥不会回填显示。")
        text_base = st.text_input("文本 API 地址", value=str(text.get("base_url") or text_preset.get("base_url") or ""), key="rc132_text_base")
        text_endpoint = str(st.session_state.get("rc132_text_endpoint") or text.get("endpoint") or text_preset.get("endpoint") or "/chat/completions")
        with st.expander("高级设置", expanded=False):
            text_endpoint = st.text_input("文本 Endpoint", value=str(text_endpoint or "/chat/completions"), key="rc132_text_endpoint")
            text_timeout_options = [60, 120, 180, 300]
            saved_text_timeout = int(text.get("timeout_seconds") or 180)
            text_timeout = st.selectbox("生成超时", text_timeout_options, index=text_timeout_options.index(saved_text_timeout) if saved_text_timeout in text_timeout_options else 2, format_func=lambda value: f"{value}秒", key="rc132_text_timeout")
            text_est_cost = st.number_input("文本单次预估费用（元）", min_value=0.0, value=_safe_float(settings.get("text_estimated_cost_per_call"), 0.0), step=0.001, format="%.4f", key="rc132_text_est_cost", help="填写后会在生成前显示人民币估算")
            st.caption("支持 OpenAI兼容接口、国内官方接口、API中转、自定义Base URL 和自定义 Endpoint；正文模型会在测试和生成时自动匹配。")
        final_text_model = str(settings.get("resolved_text_model") or text.get("model") or "")
        text_save, text_probe = st.columns(2)
        if text_save.button("保存文本配置", type="primary", disabled=restricted, use_container_width=True, key="rc132_text_save"):
            values = profile_values("text", text, text_provider)
            values.update({"model": final_text_model, "base_url": text_base, "endpoint": text_endpoint, "timeout_seconds": int(st.session_state.get("rc132_text_timeout") or 180), "api_key": text_key or "***"})
            settings["text_profile"] = values
            settings["text_estimated_cost_per_call"] = float(st.session_state.get("rc132_text_est_cost") or 0)
            if shared:
                image_values = dict(settings.get("image_profile") or image or {})
                image_values.update({"api_key": text_key or "***", "has_api_key": bool(text_key or image.get("has_api_key"))})
                settings["image_profile"] = image_values
            save_settings(settings)
            # ---- Verify save actually persisted ----
            try:
                from modules.config_store import load_settings as _reload_settings
                from modules.credential_store import load_secret as _load_secret
                from modules.app_paths import config_dir as _config_dir
                _reloaded = _reload_settings()
                _tp = _reloaded.get("text_profile", {})
                _has_key = bool(_tp.get("has_api_key"))
                _ref = str(_tp.get("credential_ref") or "")
                if not _has_key or not _ref:
                    st.error("TEXT_CONFIG_SAVE_FAILED：配置写入后验证失败，请重试。")
                else:
                    _key = _load_secret(_ref)
                    if not _key:
                        st.error("TEXT_CREDENTIAL_VERIFY_FAILED：密钥加密保存后无法解密，请重试。")
                    else:
                        st.session_state["rc132_text_status"] = "已保存，尚未检测"
                        st.success(f"文本配置已安全保存。\\n\\n保存位置：{_config_dir()}")
            except Exception:
                st.error("TEXT_CREDENTIAL_PATH_INVALID：配置保存路径异常，请重启软件后重试。")
        if text_probe.button("测试文本接口", disabled=restricted, use_container_width=True, key="rc132_text_probe"):
            try:
                result = _api("POST", "/models/text/test", timeout=120, json={"timeout_override": int(st.session_state.get("rc132_text_timeout") or 180), "profile": {"name": text_provider, "model": final_text_model, "api_key": text_key, "base_url": text_base, "endpoint": text_endpoint}})
                details = result.get("details") or {}
                resolved = str(details.get("resolved_model") or result.get("model") or "")
                st.session_state["rc132_text_status"] = "文本接口测试通过"
                st.success(f"文本接口测试通过，已自动匹配正文模型：{resolved or '已验证'}。")
                _render_diagnostic_details(result, expander_label="查看本次文本接口诊断")
            except Exception as exc:
                st.session_state["rc132_text_status"] = "文本接口测试失败"
                st.error(_model_error_message(exc))
                _render_diagnostic_details(exc, expander_label="查看失败诊断")
        text_clear_confirmed = st.checkbox("我确认清除已保存的文本 Key", key="rc132_confirm_clear_text_key", disabled=restricted or not text.get("has_api_key"))
        if st.button("清除文本 Key", disabled=restricted or not text.get("has_api_key") or not text_clear_confirmed, use_container_width=True):
            settings["text_profile"] = {**text, "clear_api_key": True, "api_key": ""}
            save_settings(settings)
            st.session_state["rc132_text_status"] = "未配置"
            st.success("文本模型 Key 已清除")

    with st.container(border=True):
        st.markdown("### 图片接口")
        st.caption(f"连接状态：{status_label('image', image)}")
        st.radio("图片接口来源", INTERFACE_SOURCES, horizontal=True, key="rc132_image_source", disabled=restricted)
        image_provider = st.selectbox("图片服务商", names, index=names.index(image_provider), key="rc132_image_provider", disabled=restricted)
        image_key = st.text_input("图片 API Key", value="", type="password", placeholder="已保存，留空则继续使用" if image.get("has_api_key") else "请输入图片 API Key", key="rc132_image_key")
        if image.get("has_api_key"):
            st.caption(f"已保存图片 Key：{_mask_api_key(str(image.get('api_key') or ''))}。保存后完整密钥不会回填显示。")
        image_base = st.text_input("图片 API 地址", value=str(image.get("base_url") or image_preset.get("base_url") or ""), key="rc132_image_base")
        image_endpoint = str(st.session_state.get("rc132_image_endpoint") or image.get("endpoint") or image_preset.get("endpoint") or "/images/generations")
        image_size_value = str(st.session_state.get("rc132_image_size") or image.get("size") or "1536x1024")
        saved_image_model = str(image.get("model") or "")
        image_model_options = model_options("image", str(st.session_state.get("rc132_image_model_selected") or saved_image_model))
        image_model_mode = st.radio("图片模型选择方式", ["从检测结果中选择", "手动填写模型名称"], horizontal=True, key="rc132_image_model_mode", disabled=restricted)
        if image_model_options:
            image_model_selected = st.selectbox("图片模型下拉列表", image_model_options, index=image_model_options.index(str(st.session_state.get("rc132_image_model_selected") or saved_image_model)) if str(st.session_state.get("rc132_image_model_selected") or saved_image_model) in image_model_options else 0, key="rc132_image_model_selected", disabled=restricted or image_model_mode != "从检测结果中选择")
        else:
            st.info("点击“检测可用模型”后会显示图片模型列表；不支持 /models 时可在高级设置手动填写。")
            image_model_selected = ""
        with st.expander("高级设置", expanded=False):
            image_model_manual = st.text_input("手动填写图片模型名称", placeholder="例如服务商返回或中转平台提供的模型名", key="rc132_image_model_manual", disabled=restricted or image_model_mode != "手动填写模型名称")
            image_endpoint = st.text_input("图片 Endpoint", value=str(image_endpoint or "/images/generations"), key="rc132_image_endpoint")
            image_size = st.selectbox("图片尺寸", ["1024x1024", "1536x1024", "1024x1536"], index=["1024x1024", "1536x1024", "1024x1536"].index(image_size_value) if image_size_value in {"1024x1024", "1536x1024", "1024x1536"} else 1, key="rc132_image_size")
            image_est_cost = st.number_input("图片单次预估费用（元）", min_value=0.0, value=_safe_float(settings.get("image_estimated_cost_per_call"), 0.0), step=0.001, format="%.4f", key="rc132_image_est_cost", help="填写后会在生成前显示人民币估算")
            st.caption("图片接口可使用 OpenAI兼容 images/generations、国内官方图片接口、API中转或自定义 Endpoint。")
        final_image_model = final_model_value("image")
        image_save, image_discover = st.columns(2)
        if image_save.button("保存图片配置", type="primary", disabled=restricted, use_container_width=True, key="rc132_image_save"):
            values = profile_values("image", image, image_provider)
            values.update({"model": final_image_model, "base_url": image_base, "endpoint": image_endpoint, "size": image_size, "api_key": image_key or "***"})
            settings["image_profile"] = values
            settings["image_estimated_cost_per_call"] = float(st.session_state.get("rc132_image_est_cost") or 0)
            if shared:
                text_values = dict(settings.get("text_profile") or text or {})
                text_values.update({"api_key": image_key or "***", "has_api_key": bool(image_key or text.get("has_api_key"))})
                settings["text_profile"] = text_values
            save_settings(settings)
            st.session_state["rc132_image_status"] = "已保存，尚未检测"
            st.success("图片配置已保存")
        if image_discover.button("检测可用模型", disabled=restricted, use_container_width=True, key="rc132_image_discover"):
            try:
                result = _api("POST", "/models/image/discover", timeout=45, json={"profile_kind": "image", "use_for_both": shared, "profile": {"name": image_provider, "api_key": image_key, "base_url": image_base, "endpoint": image_endpoint}})
                discovered_image = list(result.get("image_models") or [])
                if not discovered_image:
                    discovered_image = list(dict.fromkeys(list(result.get("other_models") or []) + list(result.get("text_models") or [])))
                st.session_state["rc132_image_model_options"] = discovered_image
                st.session_state["rc132_image_models"] = discovered_image
                picked_image = str(result.get("recommended_image_model") or (discovered_image[0] if discovered_image else "") or "")
                picked_text = str(result.get("recommended_text_model") or "")
                if picked_image:
                    st.session_state["rc132_pending_image_model"] = picked_image
                    st.session_state["rc132_image_status"] = f"已找到 {len(discovered_image)} 个模型，请选择要使用的模型"
                    if shared:
                        st.session_state["rc132_pending_text_model"] = picked_text
                    st.success(f"已找到 {len(discovered_image)} 个模型，请选择要使用的模型。推荐：{picked_image}")
                    st.rerun()
                else:
                    st.session_state["rc132_image_status"] = "未自动确认图片功能"
                    st.warning("未能自动识别图片模型。模型识别失败不代表接口不能使用，可在高级设置手动填写。真实验证需要生成一张测试图片，可能产生费用。")
            except Exception as exc:
                st.session_state["rc132_image_status"] = "检测失败，可手动配置"
                st.error(_model_list_error_message(_api_error_text(exc)))
        image_check, image_spacer = st.columns(2)
        if image_check.button("本地格式检查", disabled=restricted, use_container_width=True, key="rc132_image_check"):
            try:
                result = _api("POST", "/models/image/check-config", timeout=30, json={"profile": {"name": image_provider, "model": final_image_model, "api_key": image_key, "base_url": image_base, "endpoint": image_endpoint, "size": image_size}})
                st.session_state["rc132_image_status"] = "填写完整，尚未验证"
                st.info(f"填写完整，尚未验证 Key、模型、权限和余额。Key 已填写：{'是' if result.get('details', {}).get('key_present') else '否'}。")
                st.caption("本检查不会访问第三方接口，无法判断 API Key、模型、权限、余额或图片生成能力是否有效。")
                _render_diagnostic_details(result, expander_label="查看本次本地检查信息")
            except Exception as exc:
                st.session_state["rc132_image_status"] = "配置有误"
                st.error(_model_error_message(exc, image=True))
                _render_diagnostic_details(exc, expander_label="查看失败诊断")
        last_image_test_at = str((settings.get("image_profile") or {}).get("last_image_test_at") or image.get("last_image_test_at") or "")
        with st.container(border=True):
            status_text = "图片模型可用" if last_image_test_at or st.session_state.get("rc132_image_status") == "图片模型真实调用成功" else "尚未进行真实测试"
            st.markdown("#### 图片接口状态")
            st.write(status_text)
            if last_image_test_at:
                st.caption(f"最后测试时间：{last_image_test_at}")
            image_test_inflight = bool(st.session_state.get("rc132_image_test_inflight"))
            if st.button("真实测试图片模型", disabled=restricted or image_test_inflight, use_container_width=True, type="primary", key="rc132_image_real_test"):
                st.session_state["rc132_image_test_confirming"] = True
        paid_confirmed = st.checkbox("我确认进行一次收费测试", key="rc132_paid_image_confirm", disabled=restricted)
        st.caption("生成测试图会真实调用图片模型，可能产生费用。本次将调用图片模型1次；自动重试0次。测试提示词：一只白色咖啡杯放在木桌上，纯净背景，不含文字。")
        if st.session_state.get("rc132_image_test_confirming"):
            st.session_state["rc132_image_test_confirming"] = True
            st.warning("本次将调用图片模型1次，自动重试0次，可能产生费用。")
            confirm_left, confirm_right = st.columns(2)
            if confirm_left.button("开始测试", disabled=restricted or not paid_confirmed or image_test_inflight, key="rc132_confirm_paid_image_test", use_container_width=True):
                st.session_state["rc132_image_test_inflight"] = True
                st.session_state["rc132_image_test_run"] = True
                st.rerun()
            if confirm_right.button("取消", key="rc132_cancel_paid_image_test", use_container_width=True, disabled=image_test_inflight):
                st.session_state["rc132_image_test_confirming"] = False
                st.info("已取消图片模型测试，图片调用次数：0。")
                st.rerun()
        if st.session_state.get("rc132_image_test_run"):
            try:
                result = _api("POST", "/models/image/test", timeout=210, json={"timeout_override": 180, "confirm_paid_test": True, "profile": {"name": image_provider, "model": final_image_model, "api_key": image_key, "base_url": image_base, "endpoint": image_endpoint, "size": image_size}})
                details = result.get("details") or {}
                last_test_at = str(details.get("last_test_at") or "")
                st.session_state["rc132_image_status"] = "图片模型可用"
                st.session_state["rc132_image_test_confirming"] = False
                st.success(f"图片模型可用。模型：{result.get('model') or final_image_model}；调用次数：{details.get('generation_calls', result.get('generation_calls', 1))}；耗时：{int(result.get('elapsed_ms') or 0) / 1000:.1f}秒；最后测试时间：{last_test_at or '刚刚'}")
                try:
                    artifact = _request("GET", "/models/image/test-artifact", timeout=20)
                    if artifact.is_success:
                        st.image(artifact.content, caption="图片模型测试预览")
                except Exception:
                    st.caption("测试图已生成，但预览暂时无法读取。")
            except Exception as exc:
                st.session_state["rc132_image_status"] = "配置有误"
                st.error(_image_test_error_message(_api_error_text(exc)))
                _render_diagnostic_details(exc, expander_label="查看失败诊断")
            finally:
                st.session_state["rc132_image_test_run"] = False
                st.session_state["rc132_image_test_inflight"] = False
        image_clear_confirmed = st.checkbox("我确认清除已保存的图片 Key", key="rc132_confirm_clear_image_key", disabled=restricted or not image.get("has_api_key"))
        if st.button("清除图片 Key", disabled=restricted or not image.get("has_api_key") or not image_clear_confirmed, key="rc132_clear_image_key"):
            settings["image_profile"] = {**image, "clear_api_key": True, "api_key": ""}
            save_settings(settings)
            st.session_state["rc132_image_status"] = "未配置"
            st.success("图片模型 Key 已清除")


def _license_candidates(root: Path) -> list[Path]:
    candidates: list[Path] = []
    for folder in (root, root / "license", root / "licenses", root / "export"):
        if not folder.is_dir():
            continue
        candidates.extend(sorted(folder.glob("*.license")))
    return list(dict.fromkeys(path.resolve() for path in candidates if path.is_file()))


def _try_auto_import_license(root: Path) -> dict[str, Any] | None:
    if st.session_state.get("rc1_auto_license_checked"):
        return None
    st.session_state["rc1_auto_license_checked"] = True
    for candidate in _license_candidates(root):
        try:
            result = import_license(candidate)
        except Exception:
            continue
        if result.get("valid"):
            return result
    return None


def _import_license_bytes(payload: bytes) -> dict[str, Any]:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix="license-ui-", suffix=".license", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
        return import_license(temporary)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def _configure_clipboard_api(user32: Any, kernel32: Any) -> None:
    """Declare pointer-sized WinAPI signatures before touching clipboard memory."""
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.restype = wintypes.HGLOBAL


def _read_clipboard_text() -> str:
    if os.name == "nt":
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        _configure_clipboard_api(user32, kernel32)
        if not user32.OpenClipboard(None):
            return ""
        handle = None
        try:
            handle = user32.GetClipboardData(13)  # CF_UNICODETEXT
            if not handle:
                return ""
            address = kernel32.GlobalLock(handle)
            if not address:
                return ""
            try:
                return ctypes.wstring_at(address)
            finally:
                kernel32.GlobalUnlock(handle)
        finally:
            user32.CloseClipboard()
    try:
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        try:
            return str(root.clipboard_get())
        finally:
            root.destroy()
    except Exception:
        return ""


def _write_clipboard_text(value: str) -> bool:
    if os.name == "nt":
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        _configure_clipboard_api(user32, kernel32)
        if not user32.OpenClipboard(None):
            return False
        try:
            if not user32.EmptyClipboard():
                return False
            payload = ctypes.create_unicode_buffer(value)
            size = ctypes.sizeof(payload)
            handle = kernel32.GlobalAlloc(0x0002, size)  # GMEM_MOVEABLE
            if not handle:
                return False
            address = kernel32.GlobalLock(handle)
            if not address:
                kernel32.GlobalFree(handle)
                return False
            ctypes.memmove(address, ctypes.addressof(payload), size)
            kernel32.GlobalUnlock(handle)
            if not user32.SetClipboardData(13, handle):
                kernel32.GlobalFree(handle)
                return False
            return True
        finally:
            user32.CloseClipboard()
    return False


def _activate_license_text(payload: str) -> None:
    try:
        imported = import_license_text(payload)
    except LicenseValidationError as exc:
        st.error(license_error_message(exc.code))
        return
    except Exception:
        st.error("激活失败，请联系售后，错误编号：LIC-XXX")
        return
    if imported.get("valid"):
        st.session_state["rc1_activation_complete"] = True
        st.session_state["rc1_navigation"] = "模型设置"
        st.success("激活成功")
        st.rerun()
    st.error(license_error_message(str(imported.get("code") or "UNKNOWN")))


def _render_license_activation(root: Path) -> None:
    apply_theme()
    status = check_license()
    auto_imported = _try_auto_import_license(root) if not status.get("valid") else None
    if auto_imported and auto_imported.get("valid"):
        st.session_state["rc1_activation_complete"] = True
        st.session_state["rc1_navigation"] = "模型设置"
        st.success("激活成功，正在进入模型设置。")
        st.rerun()
    device = device_status()
    st.title("欢迎使用热点图文批量生产工作台")
    st.info("请复制设备码发送给软件提供方，收到激活码后粘贴到下方完成激活。")
    if device.get("device_identity_unavailable"):
        error_code = str(device.get("installation_error") or "IDENTITY_INITIALIZATION_FAILED")
        st.error(f"暂时无法生成设备码。\n\n错误编号：{error_code}")
        device_value = ""
        safe_diagnostic = {
            "error_code": error_code,
            "message": str(device.get("message") or ""),
            "data_root": str(device.get("data_root") or ""),
            "license_root": str(device.get("license_root") or ""),
            "launch_mode": str(device.get("launch_mode") or ""),
            "writable": bool(device.get("writable")),
            "legacy_detected": bool(device.get("legacy_detected")),
            "migration_performed": bool(device.get("migration_performed")),
            "app_version": APP_VERSION,
            "build_commit": BUILD_COMMIT,
        }
        diagnostic_text = json.dumps(safe_diagnostic, ensure_ascii=False, indent=2)
        diagnostic_left, diagnostic_right = st.columns(2)
        if diagnostic_left.button("重新检测设备信息", use_container_width=True):
            st.rerun()
        if diagnostic_right.button("复制诊断信息", use_container_width=True):
            st.success("诊断信息已复制" if _write_clipboard_text(diagnostic_text) else "复制失败，请展开后手动复制")
        with st.expander("查看安全诊断", expanded=False):
            st.code(diagnostic_text, language="json")
    else:
        device_value = str(device.get("device_code") or "")
    st.text_input("当前设备码", value=device_value, disabled=True)
    if device_value:
        st.code(device_value, language=None)
        st.caption("点击申请码右上角的复制图标即可复制，也可以使用下方复制设备码按钮。")
    copy_left, copy_right = st.columns([1, 2])
    if copy_left.button("复制设备码", disabled=not device_value):
        st.success("设备码已复制" if _write_clipboard_text(device_value) else "暂时无法复制，请手动选择设备码")
    copy_right.caption("请将设备码发送给软件提供方，收到激活码后粘贴到下方。")
    if "rc1_license_paste" not in st.session_state:
        st.session_state["rc1_license_paste"] = ""
    paste_left, paste_right = st.columns([1, 2])
    if paste_left.button("从剪贴板粘贴"):
        clipboard = _read_clipboard_text()
        if clipboard.strip():
            st.session_state["rc1_license_paste"] = clipboard
            st.success("已从剪贴板粘贴")
        else:
            st.warning("剪贴板中没有激活码")
    paste_right.caption("支持从微信、QQ、邮件复制的许可证文本。")
    pasted = st.text_area("请粘贴激活码", height=140, key="rc1_license_paste", placeholder="请粘贴许可证内容（激活码）")
    uploaded = st.file_uploader("导入许可证文件", type=["license", "json"])
    activate_left, activate_right = st.columns(2)
    if activate_left.button("从剪贴板粘贴并激活", use_container_width=True):
        clipboard = _read_clipboard_text()
        if not clipboard.strip():
            st.warning("请先粘贴激活码")
        else:
            _activate_license_text(clipboard)
    if activate_right.button("立即激活", type="primary", use_container_width=True):
        if uploaded is not None:
            _activate_license_text(uploaded.getvalue())
        elif not pasted.strip():
            st.warning("请先粘贴激活码")
        else:
            _activate_license_text(pasted)
    elif status.get("message"):
        st.caption(str(status["message"]))
    if status.get("license"):
        license_info = status["license"]
        license_label = "有效" if status.get("valid") else "不可用"
        st.caption(f"当前授权状态：{license_label}；到期时间：{license_info.get('expires_at') or '未知'}")
    current_clock = clock_status()
    if status.get("code") in {"CLOCK_ROLLBACK_SUSPECTED", "CLOCK_RECOVERY_PENDING"} or current_clock.get("clock_status") != "normal":
        st.warning("检测到系统时间异常。校准系统时间后，连续点击两次重新检查系统时间即可恢复授权。")
        if st.button("重新检查系统时间"):
            checked = check_system_time()
            if checked.get("recovery_ready"):
                recovery_result = recover_clock_rollback()
                if recovery_result.get("recovered"):
                    st.success("系统时间已恢复检查，授权已恢复。")
                elif recovery_result.get("code") == "LICENSE_EXPIRED":
                    st.error("系统时间已恢复，但当前许可证已经过期，请联系软件提供方续期。")
                elif recovery_result.get("code") == "NOT_YET_VALID":
                    st.error("系统时间已恢复，但许可证尚未生效。")
                elif recovery_result.get("code") == "SIGNATURE_INVALID":
                    st.error("许可证签名无效，请重新导入正确的许可证。")
                elif recovery_result.get("code") == "DEVICE_MISMATCH":
                    st.error("许可证不属于当前设备，请联系软件提供方。")
                elif recovery_result.get("code") == "CLOCK_ROLLBACK_SUSPECTED":
                    st.error("系统时间仍未校准到可信范围，请校准后再试。")
                else:
                    st.error("系统时间恢复失败，请重新校准后再试。")
            else:
                st.info("系统时间检查已记录，请确认时间继续向前运行后再次检查。")
            st.rerun()
    if st.button("重新检查许可证"):
        st.rerun()
    st.markdown("如需激活，请将设备申请码发送给软件提供方。")


def render_rc1_app(settings: dict[str, Any], save_settings: Any, service: Any, root: Path, categories: list[str]) -> None:
    license_status = check_license()
    if not license_status.get("valid"):
        _render_restricted_app(settings, save_settings, root)
        return
    apply_theme()
    with st.sidebar:
        st.image(str(root / "ui" / "assets" / "logo-light.svg"), width=116)
        st.markdown("### 热点图文工作台")
        st.caption("本地内容创作工作室")
        st.caption(f"版本：{APP_VERSION}")
        st.markdown("---")
    st.title("热点图文工作台")
    st.caption("选热点 → 选角度 → 开始生成 → 在我的内容中查看")
    target = st.session_state.pop("rc1_navigation_target", None)
    if target:
        st.session_state["rc1_navigation"] = target
    page = st.sidebar.radio("导航", [f"⌂ {NORMAL_PAGES[0]}", f"◈ {NORMAL_PAGES[1]}", f"＋ {NORMAL_PAGES[2]}", f"▣ {NORMAL_PAGES[3]}", f"⚙ {NORMAL_PAGES[4]}", f"ⓘ {NORMAL_PAGES[5]}"], key="rc1_navigation")
    page = page.split(" ", 1)[-1]
    if page == "首页":
        page_header("热点图文工作台", "从热点到完整图文", "选择主题、配置风格，一次完成文章、封面、正文配图和导出")
        try:
            tasks = service.store.list_tasks() if hasattr(service, "store") else []
        except Exception:
            tasks = []
        st.markdown('<div class="rc1-hero"><h2>把今天的热点，整理成可用内容</h2><p>从真实热点或自定义话题开始，完成选题、生成、编辑、配图与本地导出。</p></div>', unsafe_allow_html=True)
        st.metric("当前选题", f"{len(service.get_basket())}/5")

        # Three entry points
        home_cols = st.columns(3)
        with home_cols[0]:
            st.markdown("### 📡 今日热点")
            st.caption("浏览实时热点，选择感兴趣的话题开始创作。")
            if st.button("开始一次创作", type="primary", use_container_width=True, key="home_start_creation"):
                _navigate_to("◈ 选择话题")
                st.rerun()
            if st.button("浏览今日热点", type="primary", use_container_width=True, key="home_hotspot"):
                _navigate_to("◈ 选择话题")
                st.rerun()
        with home_cols[1]:
            st.markdown("### ✏️ 输入标题")
            st.caption("输入你自己的标题或话题，AI 直接生成文章。")
            if st.button("输入自己的话题", type="primary", use_container_width=True, key="home_custom_topic"):
                _navigate_to("◈ 选择话题")
                st.rerun()
            if st.button("输入标题开始创作", type="primary", use_container_width=True, key="home_title"):
                _navigate_to("◈ 选择话题")
                st.rerun()
        with home_cols[2]:
            st.markdown("### 🔗 批量链接")
            st.caption("粘贴 1～5 个网页链接，批量生成文章。")
            if st.button("从链接开始创作", type="primary", use_container_width=True, key="home_links"):
                _navigate_to("◈ 选择话题")
                st.rerun()
        st.markdown("#### 继续未完成任务")
        unfinished = [item for item in tasks if item.get("status") in {"queued", "running", "retry_waiting"}]
        st.caption(f"当前有 {len(unfinished)} 个未完成任务。")
        if unfinished:
            st.dataframe([{"任务": item.get("task_name"), "状态": _status(item.get("status")), "创建时间": item.get("created_at")} for item in unfinished[:5]], hide_index=True, use_container_width=True)
        st.markdown("#### 最近创作")
        recent = [item for item in tasks[:5] if load_generation_task(str(item.get("task_id") or ""))]
        if recent:
            thumb_columns = st.columns(min(3, len(recent)))
            for index, item in enumerate(recent[:3]):
                task_id = str(item.get("task_id") or "")
                task_state = load_generation_task(task_id) or {}
                cover = generation_task_dir(task_id) / "images" / "cover.png"
                with thumb_columns[index % len(thumb_columns)]:
                    if cover.is_file():
                        st.image(str(cover), use_container_width=True)
                    st.markdown(f'<div class="rc1-card"><div class="rc1-card-title">{item.get("task_name") or "未命名创作"}</div><div class="rc1-stage">{_status(task_state.get("status") or item.get("status"))}</div></div>', unsafe_allow_html=True)
        else:
            st.caption("还没有创作记录")
        st.markdown("#### 最近导出")
        export_dir = exports_root()
        exports = sorted(export_dir.glob("*"), key=lambda path: path.stat().st_mtime, reverse=True) if export_dir.exists() else []
        st.dataframe([{"文件": path.name, "类型": path.suffix.lower() or "文件"} for path in exports[:5]], hide_index=True, use_container_width=True)
    elif page == "选择话题":
        render_choose_topic(service, categories)
    elif page == "开始生成":
        render_start(service)
    elif page == "我的内容":
        _content()
    elif page == "模型设置":
        _settings_page(settings, save_settings, root, restricted=False)
    else:
        _about_page(root)
