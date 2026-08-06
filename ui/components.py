from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import streamlit as st


STAGE_LABELS = {
    "queued": "等待生成",
    "collecting_research": "正在搜索资料",
    "research_collected": "正在整理事件信息…",
    "planning_article": "正在整理事件信息…",
    "generating_article": "正在生成正文…",
    "article_saved": "正在检查内容…",
    "quality_gate": "正在检查内容…",
    "quality_rewrite": "正在检查内容…",
    "generating_image_prompt": "正在生成配图提示词",
    "generating_cover": "正在生成封面",
    "generating_inline_images": "正在生成正文配图",
    "layout_check": "正在自动排版…",
    "version_ready": "正在检查结果…",
    "committing_version": "正在保存结果",
    "committing_state": "正在保存结果",
    "failed": "生成失败",
    "partial_success": "部分完成",
    "completed": "已完成",
    "cancelled": "已取消",
}

STAGE_TIMEOUTS: dict[str, int] = {
    "collecting_research": 25,
    "research_collected": 10,
    "planning_article": 10,
    "generating_article": 70,
}

PAGE_STUCK_SECONDS = 90


def friendly_error(_: Exception | str) -> str:
    return "操作没有完成，请稍后重试。"


def stage_label(value: Any) -> str:
    return STAGE_LABELS.get(str(value or ""), "正在检查内容…")


def _parse_iso(raw: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _seconds_since(raw: Any) -> int:
    started = _parse_iso(str(raw or ""))
    if started is None:
        return 0
    return max(0, int((datetime.now(timezone.utc) - started).total_seconds()))


def _elapsed_seconds(state: dict[str, Any]) -> int:
    return _seconds_since(state.get("started_at"))


def _stage_elapsed_seconds(state: dict[str, Any]) -> int:
    return _seconds_since(state.get("stage_started_at"))


def _article_elapsed_seconds(state: dict[str, Any]) -> int:
    return _seconds_since(state.get("article_generation_started_at") or state.get("stage_started_at"))


def _seconds_since_updated(state: dict[str, Any]) -> int:
    return _seconds_since(state.get("updated_at"))


def _remaining_seconds(state: dict[str, Any], total_seconds: int = 180) -> int:
    return max(0, total_seconds - _elapsed_seconds(state))


def _is_stuck(state: dict[str, Any]) -> bool:
    status = str(state.get("status") or "")
    if status in {"completed", "failed", "cancelled", "partial_success"}:
        return False
    since_update = _seconds_since_updated(state)
    if since_update >= PAGE_STUCK_SECONDS:
        return True
    stage = str(state.get("stage") or "")
    timeout = STAGE_TIMEOUTS.get(stage)
    if timeout and _stage_elapsed_seconds(state) >= timeout * 2:
        return True
    return False


def show_progress(state: dict[str, Any]) -> None:
    status = str(state.get("status") or "")

    if status == "cancelled":
        st.progress(0.0)
        st.info("任务已取消。")
        return

    if status == "failed":
        st.progress(0.0)
        code = str(state.get("error_code") or "")
        reason = str(state.get("safe_error_message") or code or "生成失败")
        st.error(f"生成失败\n\n原因：{reason}")
        if code == "TIMEOUT" or "180秒内未返回文章" in reason:
            st.info("下一步：仅重试文章，或切换文本模型后重新生成。")
        return

    progress = max(0.0, min(1.0, float(state.get("progress") or 0) / 100))
    st.progress(progress)
    stage = str(state.get("stage") or "")

    if _is_stuck(state):
        st.warning("任务长时间没有进展。")
        task_id = str(state.get("task_id") or "")
        col1, col2, col3 = st.columns(3)
        if col1.button("继续等待", key=f"rc1_stuck_wait_{task_id}"):
            st.session_state[f"rc1_stuck_wait_ack_{task_id}"] = True
        if col2.button("取消任务", key=f"rc1_stuck_cancel_{task_id}"):
            st.session_state[f"rc1_stuck_cancel_request_{task_id}"] = True
        if col3.button("重新开始本阶段", key=f"rc1_stuck_retry_{task_id}"):
            st.session_state[f"rc1_stuck_retry_request_{task_id}"] = True
        st.caption("如果继续等待后仍无变化，请取消任务或重新开始本阶段。")

    bundle = state.get("research_bundle") or {}
    if stage == "collecting_research":
        attempts = state.get("research_attempts") or []
        accepted = int((attempts[-1] or {}).get("accepted_source_count") or 0) if attempts else 0
        st.caption(f"正在查找资料，已用时 {_stage_elapsed_seconds(state)} 秒")
        st.caption(f"已找到 {accepted} 个可用来源")
        st.caption(f"预计剩余时间约 {_remaining_seconds(state)} 秒")
        return

    if stage in {"research_collected", "planning_article"} and bundle:
        st.caption(f"已找到 {int(bundle.get('accepted_source_count') or 0)} 个相关来源")
        st.caption(f"当前阶段已用时 {_stage_elapsed_seconds(state)} 秒")
        st.caption(f"预计剩余时间约 {_remaining_seconds(state)} 秒")
        return

    if stage == "generating_article":
        model_info = state.get("model_info") or {}
        text_model = (model_info.get("text") or {}).get("model") or "未命名模型"
        timeout_limit = int((model_info.get("text") or {}).get("timeout_seconds") or 180)
        st.caption("正在生成正文…")
        st.caption(f"已等待 {_article_elapsed_seconds(state)} 秒")
        st.caption(f"预计剩余时间约 {_remaining_seconds(state)} 秒")
        st.caption(f"当前模型：{text_model}")
        st.caption(f"本次超时上限：{timeout_limit} 秒")
        return

    st.caption(stage_label(stage))
