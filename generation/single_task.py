from __future__ import annotations

import json
import hashlib
import shutil
from pathlib import Path
from typing import Any

from export.cover_builder import add_cover_title
from generation.article_generator import _prompt, generate_article, plan_for_topic
from generation.image_prompt_generator import build_cover_prompt
from generation.image_budget import calculate_image_budget, image_plan_for, normalize_image_plan, recommended_word_count
from generation.content_quality import quality_gate, sanitize_article_hard_facts
from export.layout_pipeline import ensure_article_layout
from generation.inline_images import reserve_image_generation_call, run_inline_images, set_approved_image_budget
from generation.source_overlap import analyze_source_overlap
from modules.database import SQLiteStore, get_store
from modules.generation_store import GenerationStateConflict, generation_task_dir, load_generation_task, save_generation_task
from modules.models import HotTopic, utc_now
from modules.security import redact_sensitive_text, sanitize_json, sanitize_sensitive_data
from modules.task_locks import task_lock
from research.service import ResearchService, load_research_bundle
from modules.source_formatter import normalize_source_list
from providers.errors import is_retryable_error, map_provider_exception
from providers.image_provider import OpenAIImageProvider, inspect_image
from providers.text_provider import ProviderError
from generation.versioning import MANAGED_FILES, VersionCommitError, commit_candidate, finalize_candidate, formal_files_match, recover_version_commits, snapshot_current, rollback_candidate, update_commit_record, write_intended_state


class TaskCancelledError(ProviderError):
    def __init__(self) -> None:
        super().__init__("TASK_CANCELLED", "task cancellation requested")


def _safe_model_info(profile: dict[str, Any]) -> dict[str, Any]:
    allowed = {"name", "base_url", "endpoint", "model", "auth_type", "auth_header", "timeout_seconds", "response_format", "response_type", "size", "api_format", "enabled"}
    return sanitize_json({key: profile.get(key) for key in allowed if key in profile})


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sanitize_json(value), ensure_ascii=False, indent=2), encoding="utf-8")


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(redact_sensitive_text(value), encoding="utf-8")


def _file_sha(path: Path) -> str | None:
    commit_record: dict[str, Any] | None = None
    pre_commit_state: dict[str, Any] = {}
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _commit_rewrite_result(root: Path, work_root: Path, assets: list[dict[str, Any]], task_id: str) -> dict[str, Any]:
    # source.replace(target) is executed by generation.versioning for each managed file.
    files = [
        (work_root / "article.json", root / "article.json"),
        (work_root / "article.md", root / "article.md"),
        (work_root / "prompts" / "article_prompt.txt", root / "prompts" / "article_prompt.txt"),
        (work_root / "prompts" / "cover_prompt.txt", root / "prompts" / "cover_prompt.txt"),
        (work_root / "images" / "cover.png", root / "images" / "cover.png"),
        (work_root / "images" / "assets.json", root / "images" / "assets.json"),
    ]
    relative_files = [str(source.relative_to(work_root)).replace("\\", "/") for source, _ in files if source.exists()]
    relative_files.extend(
        f"images/{str(asset.get('image_id') or '')}.png"
        for asset in assets
        if (work_root / "images" / f"{asset.get('image_id')}.png").is_file()
    )
    current_section_files = [str(path.relative_to(root)).replace("\\", "/") for path in (root / "images").glob("section-*.png")]
    candidate_section_ids = {str(item.get("image_id") or "") for item in assets}
    files_to_delete = [relative for relative in current_section_files if Path(relative).stem not in candidate_section_ids]
    return commit_candidate(root, work_root, files=relative_files, files_to_delete=files_to_delete, defer_finalize=True, metadata={"task_id": task_id})


def _restore_previous_version_after_commit_failure(state: dict[str, Any], previous_result: dict[str, Any], store: SQLiteStore, message: str) -> dict[str, Any]:
    restored = dict(state)
    restored.update({
        "status": "partial_success",
        "stage": "committing_version",
        "progress": 95,
        "failed_step": "committing_version",
        "error_code": "VERSION_STATE_COMMIT_FAILED",
        "safe_error_message": message,
        "fallback_notice": message,
        "retryable": True,
        "rewrite_requested": False,
        "previous_result": sanitize_json(previous_result),
        "inline_operation": False,
    })
    for key in ("article", "cover", "inline_images", "inline_image_summary", "version_id", "version_commit", "quality_evidence", "attempt_history", "completed_at", "article_revision", "article_edit_status", "paths", "stage", "progress"):
        if key in previous_result:
            restored[key] = sanitize_json(previous_result[key])
    restored["status"] = "partial_success"
    restored["stage"] = "committing_version"
    restored["failed_step"] = "committing_version"
    restored["error_code"] = "VERSION_STATE_COMMIT_FAILED"
    restored["safe_error_message"] = message
    restored["fallback_notice"] = message
    restored["retryable"] = True
    restored["rewrite_requested"] = False
    restored["inline_operation"] = False
    current = load_generation_task(restored["task_id"])
    restored["state_version"] = int(current.get("state_version") or 0) + 1 if current else int(restored.get("state_version") or 0) + 1
    restored["updated_at"] = utc_now()
    save_generation_task(restored, expected_version=None, allow_terminal_recovery=True)
    getattr(store, "force_task_status", store.update_task_status)(restored["task_id"], "partial_success")
    return restored


def _capture_previous_result(state: dict[str, Any]) -> dict[str, Any]:
    previous = sanitize_json(state.get("previous_result") or {})
    if not isinstance(previous, dict):
        previous = {}
    for key in ("article", "cover", "inline_images", "inline_image_summary", "version_id", "version_commit", "quality_evidence", "attempt_history", "completed_at", "article_revision", "article_edit_status", "paths", "status", "stage", "progress"):
        if key not in previous:
            previous[key] = sanitize_json(state.get(key))
    return previous


def _new_state(task: dict[str, Any], topic: HotTopic, text_profile: dict[str, Any], image_profile: dict[str, Any]) -> dict[str, Any]:
    now = utc_now()
    angle_plan = sanitize_sensitive_data(task.get("angle_plan") or (task.get("generation_options") or {}).get("angle_plan") or {})
    return {
        "task_id": task["task_id"], "status": "queued", "stage": "queued", "progress": 0,
        "started_at": None, "updated_at": now, "stage_started_at": now, "finished_at": None, "completed_at": None, "cancelled_at": None,
        "public_stage": "queued", "last_message": "等待生成",
        "cancellation_requested": False, "retry_count": 0, "attempt": 0, "next_retry_at": None,
        "max_auto_retries": 0, "state_version": 0, "failed_step": None, "error_code": "", "retryable": False,
        "generation_options": sanitize_sensitive_data(task.get("generation_options") or {}),
        "angle_id": redact_sensitive_text(str(task.get("angle_id") or angle_plan.get("angle_id") or "")),
        "angle_name": redact_sensitive_text(str(task.get("angle_name") or angle_plan.get("angle_name") or angle_plan.get("name") or "")),
        "angle_plan": angle_plan,
        "similarity_status": redact_sensitive_text(str(task.get("similarity_status") or "not_checked")),
        "similarity_score": task.get("similarity_score"),
        "rewrite_count": int(task.get("rewrite_count") or 0),
        "rewrite_requested": False,
        "safe_error_message": "", "errors": [], "topic_id": topic.id,
        "topic_snapshot": sanitize_sensitive_data(topic.to_dict()), "article": None, "cover": None,
        "model_info": {"text": _safe_model_info(text_profile), "image": _safe_model_info(image_profile)},
        "attempt_history": [],
        "quality_evidence": {"article_sha_before": None, "article_sha_after": None, "prompt_sha_before": None, "prompt_sha_after": None, "cover_prompt_sha": None},
        "similarity_evidence": None,
        "inline_images": [], "inline_image_summary": {"total": 0, "completed": 0, "failed": 0, "pending": 0, "status": "pending"}, "inline_operation": False,
        "research_bundle": None, "quality_gate": {"status": "not_checked", "metrics": {}, "reasons": []}, "quality_rewrite_count": 0,
        "text_generation_calls": 0, "text_generation_limit": 1, "text_generation_second_call_reason": "",
        "image_plan": {}, "image_usage": {"generation_calls": 0, "paid_calls": 0, "retry_calls": 0, "budget_exceeded": False},
        "version_id": None, "version_commit": None, "article_revision": 0, "article_edit_status": "saved", "article_draft_path": "article.draft.json",
        "paths": {"article_json": "article.json", "article_markdown": "article.md", "article_prompt": "prompts/article_prompt.txt", "cover_prompt": "prompts/cover_prompt.txt", "cover": "images/cover.png", "inline_assets": "images/assets.json"},
        "output_dir": ".",
    }


def _validate_transition(old_status: str | None, new_status: str, allow_rewrite: bool = False) -> None:
    if not old_status or old_status == new_status:
        return
    if old_status == "completed" and not allow_rewrite:
        raise GenerationStateConflict("completed task cannot transition")
    if old_status == "completed" and allow_rewrite:
        return
    if old_status == "cancelled":
        raise GenerationStateConflict("cancelled task cannot transition")
    allowed = {
        "queued": {"running", "cancelled", "queued"},
        "running": {"running", "completed", "failed", "partial_success", "cancelled"},
        "failed": {"queued", "running", "failed", "cancelled"},
        "partial_success": {"queued", "running", "partial_success", "cancelled"},
    }
    if new_status not in allowed.get(old_status, set()):
        raise GenerationStateConflict(f"invalid task transition: {old_status} -> {new_status}")


PUBLIC_STAGE_MAP = {
    "queued": "queued",
    "collecting_research": "researching",
    "research_collected": "organizing",
    "planning_article": "organizing",
    "generating_article": "generating_text",
    "article_saved": "checking_content",
    "quality_gate": "checking_content",
    "quality_rewrite": "checking_content",
    "generating_image_prompt": "generating_images",
    "generating_cover": "generating_images",
    "generating_inline_images": "generating_images",
    "layout_check": "formatting",
    "version_ready": "formatting",
    "committing_version": "formatting",
    "committing_state": "formatting",
    "completed": "completed",
    "failed": "failed",
    "cancelled": "cancelled",
}

PUBLIC_STAGE_MESSAGES = {
    "queued": "等待生成",
    "researching": "正在查找公开资料",
    "organizing": "正在整理事件信息",
    "generating_text": "正在生成正文",
    "checking_content": "正在检查内容",
    "generating_images": "正在生成图片",
    "formatting": "正在排版和保存结果",
    "completed": "已完成",
    "failed": "生成失败",
    "cancelled": "已取消",
}


def _persist(state: dict[str, Any], store: SQLiteStore) -> dict[str, Any]:
    with task_lock(state["task_id"]):
        current = load_generation_task(state["task_id"])
        if current and current.get("cancellation_requested") and state.get("status") != "cancelled":
            raise TaskCancelledError()
        if current and current.get("status") == "cancelled" and state.get("status") != "cancelled":
            raise TaskCancelledError()
        _validate_transition((current or {}).get("status"), str(state.get("status")), bool(state.get("rewrite_requested")))
        expected_version = int((current or {}).get("state_version") or 0) if current else None
        now = utc_now()
        current_stage = str(state.get("stage") or "queued")
        previous_stage = str((current or {}).get("stage") or "")
        if current_stage != previous_stage:
            state["stage_started_at"] = now
        else:
            state["stage_started_at"] = state.get("stage_started_at") or (current or {}).get("stage_started_at") or now
        public_stage = PUBLIC_STAGE_MAP.get(current_stage, current_stage)
        state["public_stage"] = public_stage
        state["last_message"] = PUBLIC_STAGE_MESSAGES.get(public_stage, PUBLIC_STAGE_MESSAGES.get(str(state.get("status") or ""), "处理中"))
        if str(state.get("status") or "") in {"completed", "failed", "cancelled", "partial_success"}:
            state["finished_at"] = state.get("finished_at") or state.get("completed_at") or state.get("cancelled_at") or now
        state["state_version"] = (expected_version or 0) + 1
        state["updated_at"] = now
        save_generation_task(state, expected_version=expected_version)
        store.update_task_status(state["task_id"], state["status"])
        return state


def prepare_generation_state(task: dict[str, Any], text_profile: dict[str, Any], image_profile: dict[str, Any], store: SQLiteStore | None = None) -> dict[str, Any]:
    store = store or get_store()
    selected = task.get("selected_topics") or []
    if len(selected) != 1 or int(task.get("article_count") or 0) != 1:
        raise ProviderError("PHASE2A_SINGLE_ONLY", "2A only accepts one topic and one article")
    existing = load_generation_task(task["task_id"])
    if existing:
        if existing.get("status") in {"failed", "partial_success"}:
            existing.update({
                "status": "queued",
                "stage": "queued",
                "cancellation_requested": False,
                "cancelled_at": None,
                "next_retry_at": None,
                "model_info": {"text": _safe_model_info(text_profile), "image": _safe_model_info(image_profile)},
            })
            return _persist(existing, store)
        return existing
    topic = HotTopic.from_dict(sanitize_sensitive_data(selected[0]))
    return _persist(_new_state(task, topic, text_profile, image_profile), store)


def is_cancel_requested(task_id: str) -> bool:
    state = load_generation_task(task_id)
    return bool(state and (state.get("cancellation_requested") or state.get("status") == "cancelled"))


def _check_cancel(task_id: str) -> None:
    if is_cancel_requested(task_id):
        raise TaskCancelledError()


def _quality_block(state: dict[str, Any], store: SQLiteStore, bundle: dict[str, Any] | None, reason: str, code: str = "QUALITY_GATE_FAILED") -> dict[str, Any]:
    gate = dict(state.get("quality_gate") or quality_gate({}, bundle))
    gate["reasons"] = list(gate.get("reasons") or []) + [reason]
    state["research_bundle"] = sanitize_json(bundle or {})
    state["quality_gate"] = sanitize_json(gate)
    state.update({"status": "failed", "stage": "quality_gate", "progress": 45, "failed_step": "quality_gate", "error_code": code, "safe_error_message": reason, "retryable": False, "image_usage": {"generation_calls": 0, "paid_calls": 0, "retry_calls": 0, "budget_exceeded": False}})
    return _persist(state, store)


def _has_hotlist_metadata(topic: HotTopic) -> bool:
    fields = (
        topic.title,
        topic.summary,
        topic.source_name,
        topic.source_url,
        topic.hot_value,
        topic.raw_data,
    )
    return any(str(value or "").strip() for value in fields)


def _is_hotlist_limited_bundle(bundle: dict[str, Any] | None) -> bool:
    return bool(bundle and bundle.get("hotlist_metadata_available") and str(bundle.get("research_status") or "") == "hotlist_limited")


def _build_hotlist_limited_bundle(topic: HotTopic, original_bundle: dict[str, Any] | None = None, error: str = "") -> dict[str, Any]:
    original_bundle = original_bundle or {}
    source_name = str(topic.source_name or topic.source or "热榜来源").strip()
    title = str(topic.title or "未命名热点").strip()
    summary = str(topic.summary or "").strip()
    captured_at = str(topic.captured_at or "").strip()
    hot_value = str(topic.hot_value or "").strip()
    source_url = str(topic.source_url or "").strip()
    known_lines = [f"热榜标题：{title}"]
    if summary:
        known_lines.append(f"热榜摘要：{summary}")
    if source_name:
        known_lines.append(f"热榜来源：{source_name}")
    if hot_value:
        known_lines.append(f"热度信息：{hot_value}")
    if captured_at:
        known_lines.append(f"抓取时间：{captured_at}")
    fact_text = "；".join(known_lines)
    source = {
        "source_id": "hotlist-metadata",
        "source_name": source_name,
        "publisher": source_name,
        "title": title,
        "published_at": captured_at,
        "url": source_url,
        "summary": summary or title,
        "content": summary or title,
        "fetch_success": True,
        "accepted_for_research": False,
        "limited_metadata": True,
        "source_level": "hotlist_metadata",
    }
    fact_card = {
        "fact_id": "hotlist-metadata-fact",
        "subject": title,
        "action": "进入热榜关注",
        "object": summary or title,
        "time": captured_at,
        "location": "",
        "number": hot_value,
        "source_name": source_name,
        "source_url": source_url,
        "canonical_fact": fact_text,
        "fact": fact_text,
        "source_ids": ["hotlist-metadata"],
        "supporting_source_ids": ["hotlist-metadata"],
        "verification_type": "hotlist_metadata",
        "reliability": "limited",
    }
    bundle = {
        **original_bundle,
        "topic_id": topic.id,
        "topic_title": title,
        "research_status": "hotlist_limited",
        "hotlist_metadata_available": True,
        "accepted_source_count": 0,
        "official_or_reliable_source_count": 0,
        "usable_fact_count": 1,
        "candidate_link_count": int(original_bundle.get("candidate_link_count") or 0),
        "rejected_source_count": int(original_bundle.get("rejected_source_count") or 0),
        "sources": [source],
        "usable_facts": [fact_card],
        "verified_facts": [],
        "research_fact_cards": [fact_card],
        "background": ["目前公开资料有限，需要发布前继续核对权威信息。"],
        "follow_up": ["后续仍需关注权威来源是否发布更完整说明。"],
        "open_questions": ["事件主体、时间、数据和后续处置仍需以权威来源确认为准。"],
        "limited_research_notice": "当前仅获取到热榜标题、摘要和来源元数据，只能生成谨慎基础稿，禁止补写未经确认的人物、金额、伤亡、处罚和官方结论。",
    }
    if error:
        bundle["research_error"] = redact_sensitive_text(error)[:240]
    return bundle


def _bundle_ready(bundle: dict[str, Any] | None) -> bool:
    if not bundle:
        return False
    accepted = int(bundle.get("accepted_source_count") or 0)
    reliable = int(bundle.get("official_or_reliable_source_count") or bundle.get("official_source_count") or 0)
    return accepted > 0 or reliable > 0 or str(bundle.get("research_status") or "") in {"sufficient", "verified", "limited"} or _is_hotlist_limited_bundle(bundle)


def _auto_collect_research(state: dict[str, Any], store: SQLiteStore, topic: HotTopic) -> dict[str, Any] | None:
    """Collect public research before article generation; this path never calls text or image models."""
    bundle = state.get("research_bundle") if isinstance(state.get("research_bundle"), dict) else None
    if _bundle_ready(bundle):
        state["research_bundle"] = sanitize_json(bundle or {})
        state["research_status"] = str((bundle or {}).get("research_status") or "not_collected")
        return bundle
    options = state.get("generation_options") if isinstance(state.get("generation_options"), dict) else {}
    shared_bundle = options.get("shared_research_bundle") if isinstance(options.get("shared_research_bundle"), dict) else None
    if _bundle_ready(shared_bundle):
        state["research_bundle"] = sanitize_json(shared_bundle or {})
        state["research_status"] = str((shared_bundle or {}).get("research_status") or "not_collected")
        return shared_bundle
    bundle = load_research_bundle(topic.id)
    if _bundle_ready(bundle):
        state["research_bundle"] = sanitize_json(bundle or {})
        state["research_status"] = str((bundle or {}).get("research_status") or "not_collected")
        return bundle
    manual_payload = state.get("manual_research_payload") if isinstance(state.get("manual_research_payload"), dict) else {}
    reference_urls = [str(value).strip() for value in manual_payload.get("reference_urls") or [] if str(value).strip()]
    supplemental_text = str(manual_payload.get("supplemental_text") or "")
    state.setdefault("research_attempts", [])
    state.update({"status": "running", "stage": "collecting_research", "progress": 5, "research_status": "collecting", "research_started_at": utc_now(), "failed_step": None, "error_code": "", "safe_error_message": ""})
    state["research_attempts"].append({"round": 1, "started_at": utc_now(), "status": "running"})
    _persist(state, store)
    try:
        bundle = ResearchService().collect(topic, references=reference_urls, supplemental_text=supplemental_text)
        if int((bundle or {}).get("accepted_source_count") or 0) <= 0 and _has_hotlist_metadata(topic):
            bundle = _build_hotlist_limited_bundle(topic, bundle)
        state["research_attempts"][-1].update({
            "status": str(bundle.get("research_status") or "unknown"),
            "candidate_link_count": int(bundle.get("candidate_link_count") or 0),
            "accepted_source_count": int(bundle.get("accepted_source_count") or 0),
            "rejected_source_count": int(bundle.get("rejected_source_count") or 0),
        })
        state["research_bundle"] = sanitize_json(bundle or {})
        state["research_status"] = str((bundle or {}).get("research_status") or "not_collected")
        state.update({"stage": "research_collected", "progress": 12})
        _persist(state, store)
        return bundle
    except Exception as exc:
        if _has_hotlist_metadata(topic):
            bundle = _build_hotlist_limited_bundle(topic, bundle, str(exc))
            state["research_attempts"][-1].update({
                "status": "hotlist_limited",
                "error": redact_sensitive_text(str(exc))[:240],
                "candidate_link_count": int(bundle.get("candidate_link_count") or 0),
                "accepted_source_count": 0,
                "rejected_source_count": int(bundle.get("rejected_source_count") or 0),
            })
            state["research_bundle"] = sanitize_json(bundle)
            state["research_status"] = "hotlist_limited"
            state.update({"stage": "research_collected", "progress": 12})
            _persist(state, store)
            return bundle
        state["research_attempts"][-1].update({"status": "failed", "error": redact_sensitive_text(str(exc))[:240]})
        state["research_status"] = "not_collected"
        _persist(state, store)
        return bundle


def _mark_cancelled(state: dict[str, Any], store: SQLiteStore) -> dict[str, Any]:
    current = load_generation_task(state["task_id"])
    if current and current.get("status") == "completed":
        return current
    state.update({"status": "cancelled", "stage": "cancelled", "cancellation_requested": True, "cancelled_at": (current or {}).get("cancelled_at") or utc_now(), "failed_step": None, "error_code": "TASK_CANCELLED", "safe_error_message": "task cancellation requested"})
    return _persist(state, store)


def finalize_cancelled_task(task_id: str, store: SQLiteStore | None = None) -> dict[str, Any]:
    store = store or get_store()
    with task_lock(task_id):
        if not store.get_task(task_id):
            raise ProviderError("TASK_NOT_FOUND", "task not found")
        current = load_generation_task(task_id)
        if not current:
            raise ProviderError("TASK_NOT_FOUND", "task not found")
        if current.get("status") == "completed":
            return current
        current.update({
            "status": "cancelled",
            "stage": "cancelled",
            "cancellation_requested": True,
            "cancelled_at": current.get("cancelled_at") or utc_now(),
            "next_retry_at": None,
            "error_code": "TASK_CANCELLED",
            "safe_error_message": "task cancellation requested",
            "failed_step": None,
            "retryable": False,
        })
        return _persist(current, store)


def _failure(state: dict[str, Any], store: SQLiteStore, step: str, error: Exception, status: str) -> dict[str, Any]:
    if isinstance(error, TaskCancelledError) or is_cancel_requested(state["task_id"]):
        return _mark_cancelled(state, store)
    mapped = map_provider_exception(error)
    code = str(getattr(mapped, "code", "PROVIDER_INTERNAL_ERROR"))
    detail = str(getattr(mapped, "detail", mapped))
    error_details = sanitize_json(getattr(mapped, "details", {}) or {})
    safe_message = redact_sensitive_text(detail)
    next_actions = []
    if code == "MODEL_NOT_FOUND" and step == "generating_article":
        model_name = str(error_details.get("model") or ((state.get("model_info") or {}).get("text") or {}).get("model") or "")
        safe_message = (
            "当前文本模型不可用。\n"
            "请检查模型名称、接口地址和鉴权配置是否正确。\n"
            "确认设置无误后，可以先测试模型，再重新生成文章。\n"
            f"错误代码：MODEL_NOT_FOUND\n当前模型：{model_name or '未命名模型'}"
        )
        next_actions = ["test_text_model", "retry_article", "open_model_settings"]
    if code == "TIMEOUT" and step == "generating_article":
        limit = int(((state.get("model_info") or {}).get("text") or {}).get("timeout_seconds") or 180)
        safe_message = (
            f"正文生成在 {limit} 秒内未返回结果。\n\n"
            "建议处理：\n"
            "- 继续使用已整理资料生成基础稿\n"
            "- 切换更稳定的文本模型后重试\n"
            "- 稍后重新发起文章生成"
        )
        next_actions = ["retry_article", "open_model_settings"]
    if step == "generating_article" and error_details.get("http_status") == 502 and code != "MODEL_NOT_FOUND":
        safe_message = "文本模型服务暂时不可用，请检查接口状态后重试。"
        next_actions = ["test_text_model", "retry_article", "open_model_settings"]
    retryable = is_retryable_error(code)
    if step == "generating_article" and error_details.get("http_status") == 502:
        retryable = False
    state.update({"status": status, "stage": step, "failed_step": step, "error_code": code, "safe_error_message": safe_message, "retryable": retryable, "error_details": error_details, "next_actions": next_actions})
    retry_after = getattr(mapped, "retry_after_seconds", None)
    if retry_after is not None:
        state["retry_after_seconds"] = max(0, int(float(retry_after)))
    state.setdefault("errors", []).append({"step": step, "code": code, "safe_error_message": safe_message})
    return _persist(state, store)


def _build_local_fallback_article(topic: HotTopic, angle: dict[str, Any], article_type: str, style: str, bundle: dict[str, Any] | None, reason: str) -> dict[str, Any]:
    bundle = bundle or {}
    limited_mode = _is_hotlist_limited_bundle(bundle)
    sources = [
        item
        for item in bundle.get("sources") or []
        if isinstance(item, dict) and item.get("fetch_success") and (item.get("accepted_for_research") or (limited_mode and item.get("limited_metadata"))) and not item.get("duplicate_of")
    ]
    facts = [
        str(item.get("canonical_fact") or item.get("fact") or "").strip()
        for item in (bundle.get("verified_facts") or bundle.get("usable_facts") or [])
        if isinstance(item, dict)
    ]
    facts = [item for item in facts if item][:6]
    timeline = [str(item).strip() for item in (bundle.get("timeline") or []) if str(item).strip()][:4]
    background = [str(item).strip() for item in (bundle.get("background") or []) if str(item).strip()][:3]
    impact_hints = [
        str(item.get("summary") or item.get("title") or "").strip()
        for item in sources[:3]
        if str(item.get("summary") or item.get("title") or "").strip()
    ]
    follow_up = [
        str(item).strip()
        for item in (bundle.get("follow_up") or bundle.get("open_questions") or [])
        if str(item).strip()
    ][:3]

    def _join_sentences(items: list[str], fallback: str) -> str:
        cleaned = [item.strip("。；;，, \n\t") for item in items if str(item).strip()]
        if not cleaned:
            cleaned = [fallback.strip("。；;，, \n\t")]
        return "。".join(item for item in cleaned if item) + "。"

    if limited_mode:
        topic_hint = topic.summary or topic.title
        sections = [
            {
                "heading": "事件概览",
                "body": _join_sentences([f"根据当前热榜信息，{topic.title}正在受到关注。{topic_hint}"], "根据当前热榜信息，该事件仍处在公开信息有限阶段"),
                "image_brief": f"{topic.title}相关的新闻现场感画面，无文字",
            },
            {
                "heading": "已知信息与缺口",
                "body": "目前可确认的信息主要来自热榜标题、摘要和来源元数据。公开资料尚不足以确认更多人物、具体时间、金额、伤亡、处罚或官方结论，发布前需要继续补充权威来源。",
                "image_brief": "信息核对、新闻资料整理、编辑台场景，无文字",
            },
            {
                "heading": "为什么受到关注",
                "body": "从现有信息看，该热点之所以被关注，可能与公众对事件进展、相关主体回应以及后续影响的关心有关。由于资料有限，本文只做谨慎梳理，不扩大解读。",
                "image_brief": "公众关注热点新闻的现实场景，无文字",
            },
            {
                "heading": "后续值得关注什么",
                "body": "后续仍需等待权威信息确认，包括事件主体是否发布正式说明、关键时间线是否清晰、是否存在可核验数据，以及相关平台或机构是否进一步更新。",
                "image_brief": "后续新闻追踪、公告更新、信息确认场景，无文字",
            },
        ]
    else:
        sections = [
            {
                "heading": "事件概览",
                "body": _join_sentences(facts[:2], topic.summary or "当前公开资料仍在整理中，已先生成可编辑基础稿"),
                "image_brief": "与事件概览相关的真实新闻场景，无文字",
            },
            {
                "heading": "已确认信息",
                "body": _join_sentences(facts[2:5] or timeline, "目前已确认的信息仍以公开资料和原始来源为准"),
                "image_brief": "体现已确认信息的真实新闻场景，无文字",
            },
            {
                "heading": "背景信息",
                "body": _join_sentences(background, topic.summary or "背景信息仍在补充，建议结合原始来源继续核对"),
                "image_brief": "体现背景信息的真实新闻场景，无文字",
            },
            {
                "heading": "可能影响",
                "body": _join_sentences(impact_hints or background, "根据现有公开资料，这一进展可能影响后续观察与公众理解"),
                "image_brief": "体现可能影响的真实新闻场景，无文字",
            },
            {
                "heading": "后续关注",
                "body": _join_sentences(follow_up or timeline[-2:], "后续仍需关注公开资料更新、机构说明和进一步确认信息"),
                "image_brief": "体现后续关注方向的真实新闻场景，无文字",
            },
        ]
    source_list = normalize_source_list(
        [
            {
                "publisher": source.get("source_name") or source.get("publisher") or source.get("domain"),
                "title": source.get("title"),
                "published_at": source.get("published_at"),
                "url": source.get("url"),
            }
            for source in sources[:3]
        ]
    )
    fallback_angle_name = angle.get("name") or "热点解读"
    title = f"{topic.title}：{fallback_angle_name}"
    intro = "当前模型返回异常，软件已根据已抓取公开资料生成可编辑基础稿，建议发布前继续核对关键信息。"
    ai_statement = "AI辅助声明：当前模型返回异常，本文改由软件根据公开资料整理生成，发布前请再次核对关键信息。"
    if limited_mode:
        intro = "目前公开信息有限，本文根据当前热榜标题、摘要和来源元数据生成谨慎基础稿，重点说明已知信息、信息缺口和后续核对方向。"
        ai_statement = "AI辅助声明：当前仅获取到热榜元数据，本文根据有限公开信息和AI辅助生成，发布前请核对人物、时间、数字和来源。"
    article = {
        "title": title,
        "intro": intro,
        "summary": topic.summary or intro,
        "sections": sections,
        "source_list": source_list,
        "source_statement": "\n\n".join(source_list),
        "ai_statement": ai_statement,
        "fact_basis": [],
        "body_char_count": 0,
        "text_generation_calls": 1,
        "text_generation_limit": 1,
        "text_generation_second_call_reason": "",
        "recommended_status": "review_required",
        "fallback_reason": reason,
        "fallback_kind": "hotlist_limited_draft" if limited_mode else "local_research_draft",
        "response_format_warning": True,
        "format_warning": "已生成基础稿\n当前模型返回异常，软件已根据公开资料生成可编辑版本。",
        "fallback_complete": True,
        "content_markdown": "",
    }
    markdown_parts = [f"# {title}", intro]
    for section in sections:
        markdown_parts.append(f"## {section['heading']}\n{section['body']}")
    if source_list:
        markdown_parts.append("## 资料来源\n" + "\n\n".join(source_list))
    markdown_parts.append(article["ai_statement"])
    article["content_markdown"] = "\n\n".join(part for part in markdown_parts if part).strip()
    article["body_char_count"] = sum(1 for ch in article["content_markdown"] if "\u4e00" <= ch <= "\u9fff")
    return article


def _article_requires_review(article: dict[str, Any], gate: dict[str, Any], removed_claims: list[str], used_fallback: bool) -> bool:
    return bool(used_fallback or removed_claims or str(article.get("recommended_status") or "") != "completed" or str(gate.get("status") or "") == "warning")

def run_single_task(task: dict[str, Any], text_profile: dict[str, Any], image_profile: dict[str, Any], settings: dict[str, Any] | None = None, store: SQLiteStore | None = None, retry_step: str | None = None) -> dict[str, Any]:
    settings = settings or {}
    store = store or get_store()
    selected = task.get("selected_topics") or []
    if len(selected) != 1 or int(task.get("article_count") or 0) != 1:
        raise ProviderError("PHASE2A_SINGLE_ONLY", "2A only accepts one topic and one article")
    existing = load_generation_task(task["task_id"])
    if existing and existing.get("status") == "completed":
        raise ProviderError("TASK_ALREADY_COMPLETED", "completed task cannot run again")
    if existing and existing.get("status") == "cancelled":
        raise ProviderError("TASK_CANCELLED", "cancelled task cannot run again")
    topic = HotTopic.from_dict(sanitize_sensitive_data(selected[0]))
    state = existing or _new_state(task, topic, text_profile, image_profile)
    state["max_auto_retries"] = max(0, int(settings.get("max_auto_retries", 0)))
    state["attempt"] = int(state.get("attempt") or 0) + 1
    state["retry_count"] = int(state.get("retry_count") or 0) + (1 if retry_step else 0)
    state["started_at"] = state.get("started_at") or utc_now()
    root = generation_task_dir(task["task_id"])
    article_path = root / "article.json"
    article_md_path = root / "article.md"
    article_prompt_path = root / "prompts" / "article_prompt.txt"
    cover_prompt_path = root / "prompts" / "cover_prompt.txt"
    cover_path = root / "images" / "cover.png"
    previous_result = _capture_previous_result(state) if (state.get("rewrite_requested") or state.get("previous_result") or (existing and existing.get("status") == "completed")) else {}
    rewrite_run = bool(state.get("rewrite_requested") or previous_result)
    if rewrite_run and "inline_images" not in previous_result:
        previous_result["inline_images"] = sanitize_json(state.get("inline_images") or [])
        previous_result["inline_image_summary"] = sanitize_json(state.get("inline_image_summary") or {})
    work_root = root / ".attempts" / f"attempt-{state['attempt']}" if rewrite_run else root
    work_article_path = work_root / "article.json"
    work_article_md_path = work_root / "article.md"
    work_article_prompt_path = work_root / "prompts" / "article_prompt.txt"
    work_cover_prompt_path = work_root / "prompts" / "cover_prompt.txt"
    work_cover_path = work_root / "images" / "cover.png"
    run_article = retry_step != "retry-cover" and not (existing and existing.get("article") and existing.get("failed_step") == "generating_cover")
    state["model_info"] = {"text": _safe_model_info(text_profile), "image": _safe_model_info(image_profile)}
    attempt_step = "generating_article" if run_article else "generating_cover"
    state.setdefault("attempt_history", []).append({
        "attempt": state["attempt"],
        "step": attempt_step,
        "text_model": state["model_info"]["text"].get("model"),
        "model": state["model_info"]["image"].get("model") if not run_article else state["model_info"]["text"].get("model"),
        "status": "running",
    })
    try:
        text_timeout_limit = min(70, max(25, int(text_profile.get("timeout_seconds") or (settings.get("network") or {}).get("timeout_seconds") or 70)))
        effective_text_profile = dict(text_profile)
        effective_text_profile["timeout_seconds"] = text_timeout_limit
        state["model_info"] = {"text": _safe_model_info(effective_text_profile), "image": _safe_model_info(image_profile)}
        state.update({"status": "running", "stage": "collecting_research" if run_article else "generating_image_prompt", "progress": 5 if run_article else 55, "failed_step": None, "error_code": "", "safe_error_message": "", "next_retry_at": None})
        _persist(state, store)
        _check_cancel(task["task_id"])
        options = state.get("generation_options") or task.get("generation_options") or {}
        article_type = str(options.get("article_type") or settings.get("phase2a_article_type") or "热点资讯")
        style = str(options.get("style") or settings.get("phase2a_style") or "客观通俗")
        image_style = str(options.get("image_style") or settings.get("phase2a_image_style") or "anime editorial news illustration")
        word_count = recommended_word_count(options.get("word_count") or settings.get("phase2a_word_count") or 800)
        requested_image_mode = normalize_image_plan(str(options.get("image_plan_mode") or settings.get("image_plan_mode") or "none"))
        requested_image_plan = image_plan_for(word_count, requested_image_mode)
        requested_image_plan["max_calls"] = calculate_image_budget(1, requested_image_mode)
        image_budget = int(options.get("image_call_budget_per_article") or settings.get("image_call_budget_per_article") or 0)
        if image_budget > 0 and int(requested_image_plan["max_calls"]) > image_budget:
            requested_image_plan["inline_count"] = max(0, image_budget - int(requested_image_plan["cover"]))
            requested_image_plan["inline_max"] = requested_image_plan["inline_count"]
            requested_image_plan["max_calls"] = int(requested_image_plan["cover"]) + int(requested_image_plan["inline_count"])
        auto_image_requested = bool(options.get("image_generation_requested"))
        execution_image_mode = requested_image_mode if auto_image_requested else "none"
        execution_image_plan = image_plan_for(word_count, execution_image_mode)
        state["image_plan"] = sanitize_json(requested_image_plan)
        set_approved_image_budget(state, int(execution_image_plan.get("max_calls") or 0))
        state["pending_image_confirmation"] = bool(requested_image_plan.get("max_calls")) and not auto_image_requested
        bundle = _auto_collect_research(state, store, topic)
        accepted_source_count = int((bundle or {}).get("accepted_source_count") or 0)
        limited_research_mode = _is_hotlist_limited_bundle(bundle)
        if not bundle:
            return _quality_block(state, store, bundle or {"research_status": "not_collected", "topic_id": topic.id, "topic_title": topic.title}, "有效资料来源为 0，无法生成文章。", "RESEARCH_NOT_COLLECTED")
        if accepted_source_count <= 0 and not limited_research_mode:
            return _quality_block(state, store, bundle, "有效资料来源为 0，无法生成文章。", "RESEARCH_NOT_COLLECTED")
        angle = state.get("angle_plan") or (options.get("angle_plan") if isinstance(options.get("angle_plan"), dict) else None) or plan_for_topic(1)[0]
        state["article_plan"] = sanitize_json({"angle": angle.get("name"), "core_question": angle.get("core_question"), "opening_strategy": angle.get("opening_strategy"), "structure": angle.get("structure") or [], "reader_value": angle.get("instruction")})
        state.update({"stage": "planning_article", "progress": 20})
        _persist(state, store)
        rewrite_context = options.get("rewrite_context") if isinstance(options.get("rewrite_context"), dict) else None
        if run_article:
            prompt = _prompt(topic, angle, article_type, style, word_count, rewrite_context, bundle)
            _write_text(work_article_prompt_path, prompt)
            state.update({"stage": "generating_article", "article_generation_started_at": utc_now(), "progress": 30})
            _persist(state, store)
            used_fallback = False
            state["fallback_notice"] = ""
            generation_stats = {"text_generation_calls": 0, "text_generation_limit": 2 if rewrite_context else 1, "text_generation_second_call_reason": ""}
            try:
                article = generate_article(topic, angle, article_type, style, word_count, effective_text_profile, demo_mode=False, app_mode="production", network_settings=settings.get("network"), rewrite_context=rewrite_context, research_bundle=bundle, generation_stats=generation_stats)
                if str(article.get("recommended_status") or "") == "too_short":
                    raise ProviderError("ARTICLE_TOO_SHORT", "\u6a21\u578b\u8fd4\u56de\u6b63\u6587\u8fc7\u77ed")
            except ProviderError as exc:
                if exc.code not in {"TIMEOUT", "ARTICLE_TOO_SHORT", "MODEL_OUTPUT_INVALID", "INVALID_RESPONSE", "MODEL_NOT_CONFIGURED"}:
                    raise
                used_fallback = True
                article = _build_local_fallback_article(topic, angle, article_type, style, bundle, exc.code)
                if limited_research_mode:
                    state["fallback_notice"] = "已生成谨慎基础稿\n当前仅获取到热榜元数据，发布前请补充核对权威来源。"
                else:
                    state["fallback_notice"] = "\u5df2\u751f\u6210\u57fa\u7840\u7a3f\n\u5f53\u524d\u6a21\u578b\u8fd4\u56de\u5f02\u5e38\uff0c\u8f6f\u4ef6\u5df2\u6839\u636e\u516c\u5f00\u8d44\u6599\u751f\u6210\u53ef\u7f16\u8f91\u7248\u672c\u3002"

            def _finalize_article_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
                payload["summary"] = str(payload.get("summary") or payload.get("intro") or topic.summary or "").strip()
                payload.update({"topic_id": topic.id, "topic_title": topic.title, "source": topic.source, "source_name": topic.source_name, "source_url": topic.source_url, "captured_at": topic.captured_at, "article_type": article_type, "style": style, "word_count": word_count, "status": "completed", "demo_mode": False, "angle_id": state.get("angle_id"), "angle_name": state.get("angle_name"), "angle_plan": sanitize_sensitive_data(angle)})
                payload["article_plan"] = state["article_plan"]
                cleaned_payload = sanitize_article_hard_facts(payload, bundle)
                return cleaned_payload["article"], list(cleaned_payload.get("removed_claims") or [])

            _check_cancel(task["task_id"])
            article, removed_claims = _finalize_article_payload(article)
            overlap_report = analyze_source_overlap(article, bundle)
            gate = quality_gate(article, bundle)
            overlap_warning = ""
            if overlap_report.get("status") != "passed":
                overlap_warning = "\u5185\u5bb9\u5df2\u751f\u6210\uff0c\u4f46\u4e0e\u6765\u6e90\u8868\u8fbe\u4ecd\u8f83\u63a5\u8fd1\uff0c\u5efa\u8bae\u4eba\u5de5\u4fee\u6539\u540e\u53d1\u5e03\u3002"
                warnings = list(gate.get("warnings") or [])
                if overlap_warning not in warnings:
                    warnings.append(overlap_warning)
                gate = {**gate, "status": "warning", "warnings": warnings}
                article["recommended_status"] = "review_required"
                article["warning_note"] = overlap_warning
                state["fallback_notice"] = overlap_warning
            state["source_overlap_check"] = sanitize_json(overlap_report)
            article["source_overlap_check"] = sanitize_json(overlap_report)
            state["text_generation_calls"] = int(article.get("text_generation_calls") or generation_stats.get("text_generation_calls") or (0 if used_fallback else 1))
            state["text_generation_limit"] = int(article.get("text_generation_limit") or generation_stats.get("text_generation_limit") or 1)
            state["text_generation_second_call_reason"] = str(article.get("text_generation_second_call_reason") or generation_stats.get("text_generation_second_call_reason") or "")
            article["source_statement"] = article.get("source_statement") or "\uff1b".join(str(item) for item in article.get("source_list") or [])
            if not used_fallback and article.get("response_format_warning") and not state.get("fallback_notice"):
                state["fallback_notice"] = "\u6587\u7ae0\u5df2\u751f\u6210\uff0c\u4f46\u6a21\u578b\u8fd4\u56de\u683c\u5f0f\u4e0d\u6807\u51c6\uff0c\u5df2\u81ea\u52a8\u8f6c\u6362\u4e3a\u53ef\u7f16\u8f91\u6587\u7ae0\u3002"
            article["review_required"] = bool(overlap_warning or _article_requires_review(article, gate, removed_claims, used_fallback))
            state["review_required"] = article["review_required"]
            state["research_bundle"] = sanitize_json(bundle or {})
            state["quality_gate"] = sanitize_json(gate)
            state["article"] = sanitize_json(article)
            _write_json(work_article_path, article)
            _write_text(work_article_md_path, str(article.get("content_markdown") or ""))
            state.update({"stage": "article_saved", "progress": 45})
            _persist(state, store)
            _check_cancel(task["task_id"])
        article = state.get("article")
        if not article:
            raise ProviderError("ARTICLE_NOT_AVAILABLE", "article result is missing")
        state.update({"stage": "layout_check" if int(execution_image_plan.get("max_calls") or 0) == 0 else "generating_image_prompt", "progress": 55})
        _persist(state, store)
        _check_cancel(task["task_id"])
        if int(execution_image_plan.get("max_calls") or 0) == 0:
            article["images"] = []
            try:
                article = ensure_article_layout(article)
            except Exception as layout_error:
                return _failure(state, store, "layout_check", ProviderError("ARTICLE_LAYOUT_CHECK_FAILED", str(layout_error)), "failed")
            state["article"] = sanitize_json(article)
            state["layout_check"] = sanitize_json(article.get("layout_check") or {})
            state.update({"article": sanitize_json(article), "cover": None, "inline_images": [], "inline_image_summary": {"total": 0, "completed": 0, "failed": 0, "pending": 0, "status": "completed"}, "stage": "completed", "progress": 100, "status": "completed", "completed_at": utc_now(), "failed_step": None, "error_code": "", "safe_error_message": "", "quality_gate": state.get("quality_gate") or {"status": "passed"}})
            _write_json(root / "article.json", article)
            _write_text(root / "article.md", str(article.get("content_markdown") or ""))
            return _persist(state, store)
        cover_prompt = build_cover_prompt(article, image_style)
        _write_text(work_cover_prompt_path, cover_prompt)
        state.update({"stage": "generating_cover", "progress": 65})
        _persist(state, store)
        _check_cancel(task["task_id"])
        raw_path = work_root / "images" / "cover_raw"
        image_provider = OpenAIImageProvider(image_profile, network_settings=settings.get("network"))
        reserve_image_generation_call(state)
        _persist(state, store)
        image_provider.generate(cover_prompt, raw_path)
        _check_cancel(task["task_id"])
        inspect_image(raw_path)
        add_cover_title(raw_path, str(article.get("title") or topic.title), work_cover_path)
        _check_cancel(task["task_id"])
        final_metadata = inspect_image(work_cover_path)
        raw_path.unlink(missing_ok=True)
        work_root.mkdir(parents=True, exist_ok=True)
        state["cover"] = {"status": "completed", "path": "images/cover.png", "prompt": cover_prompt, "metadata": final_metadata, "provider_response_type": image_provider.last_response_type}
        article["cover"] = state["cover"]
        article["images"] = [{"role": "cover", "path": "images/cover.png", "status": "completed", "metadata": final_metadata}]
        state["article"] = sanitize_json(article)
        _write_json(work_article_path, article)
        _write_text(work_article_md_path, str(article.get("content_markdown") or ""))
        state["quality_evidence"] = {
            "article_sha_before": previous_result.get("article_sha"),
            "article_sha_after": _file_sha(work_article_path),
            "prompt_sha_before": previous_result.get("prompt_sha"),
            "prompt_sha_after": _file_sha(work_article_prompt_path),
            "cover_prompt_sha": _file_sha(work_cover_prompt_path),
        }
        state["attempt_history"][-1].update({
            "article_sha_before": previous_result.get("article_sha"),
            "article_sha_after": _file_sha(work_article_path),
            "prompt_sha_before": previous_result.get("prompt_sha"),
            "prompt_sha_after": _file_sha(work_article_prompt_path),
            "cover_prompt_sha": _file_sha(work_cover_prompt_path),
        })
        state.update({"stage": "generating_inline_images", "progress": 75})
        _persist(state, store)
        try:
            state = run_inline_images(
                task["task_id"],
                image_profile,
                settings=settings,
                store=store,
                persist_article=True,
                provider=image_provider,
                replan=run_article,
                exact_count=int(execution_image_plan.get("inline_count") or 0),
                output_root=work_root,
            )
        except Exception as inline_error:
            return _failure(state, store, "generating_inline_images", inline_error, "partial_success")
        article = state.get("article") or article
        _check_cancel(task["task_id"])
        try:
            article = ensure_article_layout(article)
        except Exception as layout_error:
            return _failure(state, store, "layout_check", ProviderError("ARTICLE_LAYOUT_CHECK_FAILED", str(layout_error)), "failed")
        state["article"] = sanitize_json(article)
        state["layout_check"] = sanitize_json(article.get("layout_check") or {})
        state.update({"stage": "layout_check", "progress": 92})
        _persist(state, store)
        inline_summary = state.get("inline_image_summary") or {}
        if int(inline_summary.get("failed") or 0) or int(inline_summary.get("pending") or 0):
            failed_asset = next((item for item in state.get("inline_images") or [] if item.get("status") == "failed"), {})
            if rewrite_run:
                shutil.rmtree(work_root, ignore_errors=True)
                state["article"] = previous_result.get("article")
                state["cover"] = previous_result.get("cover")
                state["inline_images"] = previous_result.get("inline_images") or []
                state["inline_image_summary"] = previous_result.get("inline_image_summary") or {}
                state["inline_images"] = previous_result.get("inline_images") or []
                state["inline_image_summary"] = previous_result.get("inline_image_summary") or {}
                state.update({"status": "partial_success", "stage": "generating_inline_images", "progress": 85, "failed_step": "generating_inline_images", "error_code": str(failed_asset.get("error_code") or "INLINE_IMAGES_INCOMPLETE"), "safe_error_message": "正文图片生成未全部完成，已保留当前可用结果。", "fallback_notice": "正文图片生成未全部完成，当前版本仍可继续查看和导出。", "new_version_status": "partial_success", "retryable": bool(failed_asset.get("retryable", True)), "inline_operation": False})
                return _persist(state, store)
            state.update({
                "status": "partial_success",
                "stage": "generating_inline_images",
                "progress": 85,
                "failed_step": "generating_inline_images",
                "error_code": str(failed_asset.get("error_code") or "INLINE_IMAGES_INCOMPLETE"),
                "safe_error_message": "正文图片生成未全部完成，请稍后重试缺失图片。",
                "retryable": bool(failed_asset.get("retryable", True)),
            })
            return _persist(state, store)
        if rewrite_run:
            pre_commit_state = sanitize_json(load_generation_task(task["task_id"]) or {})
            state.update({"status": "running", "stage": "version_ready", "progress": 92, "failed_step": "version_ready", "error_code": "", "safe_error_message": ""})
            _persist(state, store)
            state.update({"status": "running", "stage": "committing_version", "progress": 95, "failed_step": "committing_version", "error_code": "", "safe_error_message": ""})
            _persist(state, store)
            commit_record = _commit_rewrite_result(root, work_root, state.get("inline_images") or [], task["task_id"])
            state["version_id"] = commit_record.get("version_id")
            state["version_commit"] = {key: value for key, value in commit_record.items() if key not in {"candidate_hashes", "rollback_hashes"}}
            attempts_root = root / ".attempts" / str(commit_record["attempt_root"])
            final_state = sanitize_json(dict(state))
            final_state.update({"status": "completed", "stage": "completed", "progress": 100, "completed_at": utc_now(), "failed_step": None, "error_code": "", "safe_error_message": "", "rewrite_requested": False, "previous_result": None, "inline_operation": False})
            final_state["attempt_history"][-1]["status"] = "completed"
            final_state["version_commit"] = {key: value for key, value in commit_record.items() if key not in {"candidate_hashes", "rollback_hashes"}}
            write_intended_state(attempts_root, {"task_id": task["task_id"], "version_id": commit_record.get("version_id"), "final_state": final_state, "previous_state": pre_commit_state, "previous_result": previous_result})
            commit_record = update_commit_record(attempts_root, commit_record, "committing_state")
            final_state["version_commit"] = {key: value for key, value in commit_record.items() if key not in {"candidate_hashes", "rollback_hashes"}}
            current_state = load_generation_task(task["task_id"]) or {}
            final_state["state_version"] = int(current_state.get("state_version") or 0) + 1
            save_generation_task(final_state, expected_version=int(current_state.get("state_version") or 0), allow_terminal_recovery=True)
            store.update_task_status(task["task_id"], "completed")
            saved_state = load_generation_task(task["task_id"]) or {}
            saved_task = store.get_task(task["task_id"])
            if saved_state.get("status") != "completed" or saved_state.get("version_id") != commit_record.get("version_id") or not saved_task or saved_task.get("status") != "completed" or not formal_files_match(root, commit_record):
                raise VersionCommitError("task state and formal version verification failed", commit_path=attempts_root / "commit.json")
            try:
                finalized = finalize_candidate(root, attempts_root, commit_record)
            except Exception:
                if not formal_files_match(root, commit_record):
                    raise
                finalized = dict(commit_record)
            final_state["version_commit"] = {key: value for key, value in finalized.items() if key not in {"candidate_hashes", "rollback_hashes"}}
            return final_state
        else:
            snapshot_files = list(MANAGED_FILES)
            snapshot_files.extend(f"images/{item.get('image_id')}.png" for item in state.get("inline_images") or [] if item.get("image_id"))
            snapshot = snapshot_current(root, label="initial-generation", files=snapshot_files)
            if snapshot:
                state["version_id"] = snapshot.get("version_id")
        state["quality_evidence"]["article_sha_after"] = _file_sha(article_path)
        state["quality_evidence"]["prompt_sha_after"] = _file_sha(article_prompt_path)
        state["quality_evidence"]["cover_prompt_sha"] = _file_sha(cover_prompt_path)
        state["attempt_history"][-1].update({"article_sha_after": state["quality_evidence"]["article_sha_after"], "prompt_sha_after": state["quality_evidence"]["prompt_sha_after"], "cover_prompt_sha": state["quality_evidence"]["cover_prompt_sha"]})
        state.update({"status": "completed", "stage": "completed", "progress": 100, "completed_at": utc_now(), "failed_step": None, "error_code": "", "safe_error_message": "", "rewrite_requested": False, "previous_result": None})
        state["attempt_history"][-1]["status"] = "completed"
        return _persist(state, store)
    except VersionCommitError as exc:
        commit_path = getattr(exc, "commit_path", None)
        if rewrite_run and commit_path and commit_path.exists():
            try:
                record = sanitize_json(json.loads(commit_path.read_text(encoding="utf-8")))
                rollback_candidate(root, commit_path.parent, record)
            except Exception:
                pass
        if rewrite_run:
            shutil.rmtree(work_root, ignore_errors=True)
            if previous_result:
                state["article"] = previous_result.get("article")
                state["cover"] = previous_result.get("cover")
                state["inline_images"] = previous_result.get("inline_images") or []
                state["inline_image_summary"] = previous_result.get("inline_image_summary") or {}
        safe_commit_message = "版本提交失败，已恢复到上一版本结果。" if previous_result else "版本提交失败，请稍后重试。"
        if rewrite_run and previous_result:
            try:
                return _restore_previous_version_after_commit_failure(state, previous_result, store, safe_commit_message)
            except Exception:
                pass
        state.update({"status": "partial_success" if previous_result else "failed", "stage": "committing_version", "failed_step": "committing_version", "error_code": "VERSION_COMMIT_FAILED", "safe_error_message": safe_commit_message, "fallback_notice": safe_commit_message if previous_result else "", "retryable": True, "inline_operation": False})
        if state.get("attempt_history"):
            state["attempt_history"][-1].update({"status": "failed", "error_code": "VERSION_COMMIT_FAILED"})
        return _persist(state, store)
    except TaskCancelledError as exc:
        return _mark_cancelled(state, store)
    except Exception as exc:
        if rewrite_run and commit_record:
            try:
                rollback_candidate(root, root / ".attempts" / str(commit_record["attempt_root"]), commit_record)
            except Exception:
                pass
            if previous_result:
                try:
                    return _restore_previous_version_after_commit_failure(state, previous_result, store, "版本提交失败，已恢复到上一版本结果。")
                except Exception:
                    pass
        if rewrite_run:
            shutil.rmtree(work_root, ignore_errors=True)
            if previous_result:
                state["article"] = previous_result.get("article")
                state["cover"] = previous_result.get("cover")
                state["fallback_notice"] = "版本提交失败，已保留上一版本结果。"
        if state.get("attempt_history"):
            state["attempt_history"][-1]["status"] = "failed"
            state["attempt_history"][-1]["error_code"] = str(getattr(map_provider_exception(exc), "code", "PROVIDER_ERROR"))
        if state.get("article") and state.get("stage") in {"generating_cover", "generating_image_prompt"}:
            if state.get("attempt_history"):
                state["attempt_history"][-1]["step"] = "generating_cover"
                state["attempt_history"][-1]["model"] = state.get("model_info", {}).get("image", {}).get("model")
            if state.get("fallback_notice") and (state.get("previous_result") or {}).get("cover"):
                state["cover"] = state["previous_result"]["cover"]
                state["new_version_status"] = "failed"
            else:
                existing_cover = state.get("cover") or {}
                state["cover"] = {"status": "failed", "path": "images/cover.png", "prompt": existing_cover.get("prompt", "")}
            return _failure(state, store, "generating_cover", exc, "partial_success")
        return _failure(state, store, "generating_article", exc, "failed")


def cancel_single_task(task_id: str, store: SQLiteStore | None = None) -> dict[str, Any]:
    store = store or get_store()
    with task_lock(task_id):
        task = store.get_task(task_id)
        if not task:
            raise ProviderError("TASK_NOT_FOUND", "task not found")
        state = load_generation_task(task_id)
        if not state:
            topic = HotTopic.from_dict(sanitize_sensitive_data((task.get("selected_topics") or [{}])[0]))
            state = _new_state(task, topic, {}, {})
            state = _persist(state, store)
        if state.get("status") == "completed":
            raise ProviderError("TASK_ALREADY_COMPLETED", "completed task cannot be cancelled")
        if state.get("status") == "cancelled":
            return state
        state["cancellation_requested"] = True
        state["cancelled_at"] = state.get("cancelled_at") or utc_now()
        if state.get("status") in {"queued", "failed", "partial_success"}:
            state.update({"status": "cancelled", "stage": "cancelled", "error_code": "TASK_CANCELLED", "safe_error_message": "task cancellation requested", "next_retry_at": None, "retryable": False})
        return _persist(state, store)




