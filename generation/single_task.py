from __future__ import annotations

import json
import hashlib
import inspect
import shutil
import time
from pathlib import Path
from typing import Any

from export.cover_builder import add_cover_title
from generation.article_generator import CUSTOM_TOPIC_SECTION_HEADINGS, REQUIRED_SECTION_HEADINGS, _apply_quality_issue_rewrite, _apply_short_article_rewrite, _rewrite_min_chars, _prompt, generate_article, plan_for_topic
from generation.image_prompt_generator import build_cover_prompt
from generation.image_budget import calculate_image_budget, count_body_chinese_chars, image_plan_for, normalize_image_plan, recommended_word_count
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
from providers.errors import is_retryable_error, map_provider_exception, user_facing_error_message
from providers.image_provider import OpenAIImageProvider, inspect_image
from providers.text_provider import OpenAITextProvider, ProviderError
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
        "text_generation_calls": 0, "text_generation_limit": 3, "text_generation_second_call_reason": "", "text_generation_call_reasons": [],
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
    if _is_manual_topic(topic):
        return False
    fields = (
        topic.title,
        topic.summary,
        topic.source_name,
        topic.source_url,
        topic.hot_value,
        topic.raw_data,
    )
    return any(str(value or "").strip() for value in fields)


def _is_manual_topic(topic: HotTopic) -> bool:
    return (
        str(topic.source or "").lower() == "manual"
        or str(topic.provider_status or "").lower() == "manual"
        or str(topic.source_name or "") == "手动输入"
    )


def _is_custom_topic_bundle(bundle: dict[str, Any] | None) -> bool:
    return bool(bundle and str(bundle.get("research_status") or "") == "custom_topic" and bundle.get("custom_topic"))


def _is_hotlist_limited_bundle(bundle: dict[str, Any] | None) -> bool:
    return bool(bundle and bundle.get("hotlist_metadata_available") and str(bundle.get("research_status") or "") == "hotlist_limited")


def _build_custom_topic_bundle(topic: HotTopic, original_bundle: dict[str, Any] | None = None, error: str = "") -> dict[str, Any]:
    original_bundle = original_bundle or {}
    title = str(topic.title or "自定义话题").strip()
    summary = str(topic.summary or "").strip()
    source_url = str(topic.source_url or topic.url or "").strip()
    fact_text = "；".join(item for item in [f"用户输入话题：{title}", f"用户补充说明：{summary}" if summary else "", f"用户参考链接：{source_url}" if source_url else ""] if item)
    source = {
        "source_id": "custom-topic-input",
        "source_name": str(topic.source_name or "手动输入"),
        "publisher": str(topic.source_name or "手动输入"),
        "title": title,
        "published_at": str(topic.captured_at or ""),
        "url": source_url,
        "summary": summary or title,
        "content": summary or title,
        "fetch_success": True,
        "accepted_for_research": False,
        "custom_topic_input": True,
        "source_level": "manual",
    }
    fact_card = {
        "fact_id": "custom-topic-input-fact",
        "subject": title,
        "action": "用户自定义写作",
        "object": summary or title,
        "time": str(topic.captured_at or ""),
        "location": "",
        "number": "",
        "source_name": str(topic.source_name or "手动输入"),
        "source_url": source_url,
        "canonical_fact": fact_text,
        "fact": fact_text,
        "source_ids": ["custom-topic-input"],
        "supporting_source_ids": ["custom-topic-input"],
        "verification_type": "custom_topic",
        "reliability": "user_input",
    }
    bundle = {
        **original_bundle,
        "topic_id": topic.id,
        "topic_title": title,
        "research_status": "custom_topic",
        "custom_topic": True,
        "accepted_source_count": 0,
        "official_or_reliable_source_count": 0,
        "usable_fact_count": 1,
        "candidate_link_count": int(original_bundle.get("candidate_link_count") or 0),
        "rejected_source_count": int(original_bundle.get("rejected_source_count") or 0),
        "sources": [source],
        "usable_facts": [fact_card],
        "verified_facts": [],
        "research_fact_cards": [fact_card],
        "background": [summary] if summary else [],
        "follow_up": ["围绕用户选择的文章类型、风格和目标读者继续展开。"],
        "open_questions": [],
        "custom_topic_notice": "这是用户手动输入的自定义话题，应按方法型、观点型或用户选择的文章类型进行原创写作，不得套用新闻热点基础稿。",
    }
    if error:
        bundle["research_error"] = redact_sensitive_text(error)[:240]
    return bundle


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
        "background": ["本文为传播核验分析稿：先梳理热榜事实和传播方式，再指出已知与未知边界，分析标题为什么容易传播和可能被怎么误读，最后给出读者核验路径和权威渠道建议。"],
        "follow_up": ["后续关注：热榜事件是否有权威机构（政府部门、正规媒体、当事方）发布正式说明；原发平台是否补充事件细节和时间线；主流媒体是否跟进深度报道。"],
        "open_questions": ["热榜标题涉及的事件主体、时间节点、具体数据和处置进展，需以权威来源确认为准。"],
        "limited_research_notice": "当前仅获取到热榜标题、摘要和来源元数据，只能生成传播核验分析稿。任务是以热榜现象本身为对象，分析传播逻辑和核验方法，禁止补写未经确认的人物、金额、伤亡、处罚和官方结论。",
    }
    if error:
        bundle["research_error"] = redact_sensitive_text(error)[:240]
    return bundle


def _bundle_ready(bundle: dict[str, Any] | None) -> bool:
    if not bundle:
        return False
    accepted = int(bundle.get("accepted_source_count") or 0)
    reliable = int(bundle.get("official_or_reliable_source_count") or bundle.get("official_source_count") or 0)
    return accepted > 0 or reliable > 0 or str(bundle.get("research_status") or "") in {"sufficient", "verified", "limited"} or _is_hotlist_limited_bundle(bundle) or _is_custom_topic_bundle(bundle)


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
        if int((bundle or {}).get("accepted_source_count") or 0) <= 0 and _is_manual_topic(topic):
            bundle = _build_custom_topic_bundle(topic, bundle)
        elif int((bundle or {}).get("accepted_source_count") or 0) <= 0 and _has_hotlist_metadata(topic):
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
        if _is_manual_topic(topic):
            bundle = _build_custom_topic_bundle(topic, bundle, str(exc))
            state["research_attempts"][-1].update({
                "status": "custom_topic",
                "error": redact_sensitive_text(str(exc))[:240],
                "candidate_link_count": int(bundle.get("candidate_link_count") or 0),
                "accepted_source_count": 0,
                "rejected_source_count": int(bundle.get("rejected_source_count") or 0),
            })
            state["research_bundle"] = sanitize_json(bundle)
            state["research_status"] = "custom_topic"
            state.update({"stage": "research_collected", "progress": 12})
            _persist(state, store)
            return bundle
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
                "body": (
                    f"根据当前热榜信息，{topic.title}正在受到广泛关注。{topic_hint}。"
                    f"从热榜排名和讨论热度来看，该事件在短时间内聚集了大量用户关注和讨论。"
                    f"由于目前公开信息主要来自热榜标题和来源元数据，能够确认的具体事实相对有限。"
                    f"本文将基于现有公开信息进行谨慎梳理，重点区分已知信息与尚待确认的细节，"
                    f"帮助读者快速了解当前可以确认的内容和需要注意的信息缺口。"
                ),
                "image_brief": f"{topic.title}相关的新闻现场感画面，无文字",
            },
            {
                "heading": "已知信息与缺口",
                "body": (
                    "目前可确认的信息主要来自热榜标题、摘要和来源元数据。"
                    "通过对现有公开资料的系统整理，可以梳理出事件的基本轮廓和各方关注焦点。"
                    "但需要特别指出的是，公开资料尚不足以确认更多关键细节，"
                    "包括涉事人物的完整信息、具体发生时间、涉及的金额数字、人员伤亡情况、"
                    "处罚措施和官方正式结论等。这些信息缺口需要在发布前继续通过权威来源进行补充核实。"
                    "建议读者在阅读时将已确认信息与网络讨论中的推测区分对待。"
                ),
                "image_brief": "信息核对、新闻资料整理、编辑台场景，无文字",
            },
            {
                "heading": "为什么受到关注",
                "body": (
                    "从现有信息分析，该热点之所以能够迅速进入公众视野并持续受到关注，"
                    "可能与以下几个因素有关。首先，事件涉及的领域与大量普通用户的实际生活或工作场景相关，"
                    "因此引发了自发的讨论和转发。其次，事件中涉及的相关方具有一定的公众认知度，"
                    "其回应和后续处理方式也成为观察重点。第三，该事件可能对同行业或同类场景产生示范效应。"
                    "由于目前公开资料尚不完整，本文仅基于现有信息进行梳理，不扩大解读范围。"
                ),
                "image_brief": "公众关注热点新闻的现实场景，无文字",
            },
            {
                "heading": "后续值得关注什么",
                "body": (
                    "后续值得重点关注的几个方向包括：第一，事件相关主体是否会发布正式说明或回应，"
                    "这将直接影响公众对事件性质的判断。第二，关键时间线的进一步明确，"
                    "包括事件发生的准确时间节点和各方的反应序列。第三，是否存在可核验的官方数据或文件，"
                    "这有助于将讨论建立在更坚实的公开信息基础之上。第四，相关平台或监管机构是否会"
                    "进一步更新信息或出台相关指引。在更多权威信息出现之前，建议保持关注但不过度解读。"
                ),
                "image_brief": "后续新闻追踪、公告更新、信息确认场景，无文字",
            },
        ]
    else:
        def _fallback_para(items: list[str], fallback: str) -> str:
            cleaned = [item.strip("。；;，, \n\t") for item in items if str(item).strip()]
            if cleaned:
                return "。".join(item for item in cleaned if item) + "。"
            return fallback

        sections = [
            {
                "heading": "事件概览",
                "body": (
                    _fallback_para(facts[:2], topic.summary or "当前公开资料仍在整理中，已先生成可编辑基础稿")
                    + "从目前可获取的公开信息来看，该事件的基本脉络正在逐步清晰。"
                    + "本文将基于已收集的资料进行梳理，为读者提供一个可编辑的基础版本。"
                    + "需要注意的是，部分细节和数据仍可能随着后续信息的补充而有所调整。"
                ),
                "image_brief": "与事件概览相关的真实新闻场景，无文字",
            },
            {
                "heading": "已确认信息",
                "body": (
                    _fallback_para(facts[2:5] or timeline, "目前已确认的信息仍以公开资料和原始来源为准")
                    + "通过对现有公开报道和官方信息的交叉比对，以下信息具有较高的可信度。"
                    + "建议读者在使用这些信息时，保持对原始来源的关注和核对。"
                    + "对于尚存争议或仅有单一来源的细节，本文会标注说明。"
                ),
                "image_brief": "体现已确认信息的真实新闻场景，无文字",
            },
            {
                "heading": "背景信息",
                "body": (
                    _fallback_para(background, topic.summary or "背景信息仍在补充，建议结合原始来源继续核对")
                    + "了解事件发生的历史背景、相关环境和行业惯例，有助于更全面地理解当前进展。"
                    + "需要说明的是，背景信息主要来自公开渠道，不同来源可能存在视角差异。"
                    + "本文力求提供多角度的背景梳理，但仍建议读者根据实际需要进一步查证。"
                ),
                "image_brief": "体现背景信息的真实新闻场景，无文字",
            },
            {
                "heading": "可能影响",
                "body": (
                    _fallback_para(impact_hints or background, "根据现有公开资料，这一进展可能影响后续观察与公众理解")
                    + "从短期来看，事件可能引发相关领域的后续调整和政策回应。"
                    + "从中长期来看，这一案例也可能成为同类场景的参考坐标。"
                    + "当然，在更多正式结论出现之前，任何影响评估都应保持一定的审慎态度。"
                ),
                "image_brief": "体现可能影响的真实新闻场景，无文字",
            },
            {
                "heading": "后续关注",
                "body": (
                    _fallback_para(follow_up or timeline[-2:], "后续仍需关注公开资料更新、机构说明和进一步确认信息")
                    + "建议重点关注的几个方向：相关方的正式回应、权威机构的最新通报、"
                    + "以及是否有补充数据或第三方独立评估发布。"
                    + "本文也将根据公开信息的更新及时调整和完善相关内容。"
                ),
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
    ai_statement = ""
    if limited_mode:
        intro = "目前公开信息有限，本文根据当前热榜标题、摘要和来源元数据生成谨慎基础稿，重点说明已知信息、信息缺口和后续核对方向。"
        ai_statement = ""
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
        "used_local_fallback": True,
        "response_format_warning": True,
        "format_warning": "已生成基础稿\n当前模型返回异常，软件已根据公开资料生成可编辑版本。",
        "fallback_complete": True,
        "content_markdown": "",
    }
    markdown_parts = [f"# {title}", intro]
    for section in sections:
        markdown_parts.append(f"## {section['heading']}\n{section['body']}")
    article["content_markdown"] = "\n\n".join(part for part in markdown_parts if part).strip()
    article["body_char_count"] = count_body_chinese_chars(article)
    return article



def _build_expanded_custom_topic_article(topic: HotTopic, angle: dict[str, Any], article_type: str, style: str, original_article: dict[str, Any] | None = None) -> dict[str, Any]:
    """Auto-expand a too-short model article in custom_topic mode into a method-type article with >=700 body chars."""
    title_text = str(topic.title or '\u81ea\u5b9a\u4e49\u8bdd\u9898').strip()
    summary = str(topic.summary or '').strip()
    source_url = str(topic.source_url or topic.url or '').strip()
    source_name = str(topic.source_name or '\u624b\u52a8\u8f93\u5165').strip()
    captured_at = str(topic.captured_at or '').strip()
    _FALLBACK_SUMMARY = '\u7528\u6237\u672a\u63d0\u4f9b\u8bf4\u660e\uff0c\u540e\u7eed\u53ef\u6839\u636e\u76ee\u6807\u4eba\u7fa4\u548c\u671f\u671b\u6210\u679c\u8865\u5145\u5177\u4f53\u573a\u666f\u3002'
    LQ = '\u201c'
    RQ = '\u201d'
    title = f'{title_text}\uff1a\u771f\u6b63\u53ef\u843d\u5730\u7684\u64cd\u4f5c\u6307\u5357'

    h1 = '\u6838\u5fc3\u6982\u5ff5'
    h2 = '\u53ef\u6267\u884c\u65b9\u6cd5'
    h3 = '\u5177\u4f53\u6b65\u9aa4'
    h4 = '\u98ce\u9669\u63d0\u9192'
    h5 = '\u603b\u7ed3'

    def T(*parts):
        return ''.join(str(p) for p in parts)

    intro = T(
        '\u6587\u672c\u6a21\u578b\u6210\u529f\u8fd4\u56de\u4e86\u521d\u6b65\u5185\u5bb9\uff0c\u4f46\u6b63\u6587\u7bc7\u5e45\u8f83\u77ed\uff08\u4e0d\u8db3700\u5b57\uff09\uff0c',
        '\u8f6f\u4ef6\u5df2\u81ea\u52a8\u6269\u5c55\u4e3a\u5b8c\u6574\u53ef\u7f16\u8f91\u57fa\u7840\u7a3f\u3002',
        '\u672c\u6587\u56f4\u7ed5', LQ, title_text, RQ, '\u68b3\u7406\u65b9\u6cd5\u578b\u7ed3\u6784\uff0c',
        '\u5305\u542b\u6838\u5fc3\u6982\u5ff5\u3001\u53ef\u6267\u884c\u65b9\u6cd5\u3001\u5177\u4f53\u6b65\u9aa4\u3001\u98ce\u9669\u63d0\u9192\u548c\u603b\u7ed3\uff0c',
        '\u53d1\u5e03\u524d\u8bf7\u8865\u5145\u4e2a\u4eba\u6848\u4f8b\u3001\u771f\u5b9e\u6570\u636e\u548c\u53c2\u8003\u6765\u6e90\u3002',
    )

    sections = [
        {
            'heading': h1,
            'body': T(
                '\u7406\u89e3', LQ, title_text, RQ, '\u7684\u5173\u952e\uff0c\u4e0d\u662f\u8ffd\u9010\u5de5\u5177\u6216\u6982\u5ff5\u672c\u8eab\uff0c',
                '\u800c\u662f\u641e\u6e05\u695a\u5b83\u5230\u5e95\u80fd\u89e3\u51b3\u4ec0\u4e48\u5177\u4f53\u95ee\u9898\u3001\u670d\u52a1\u54ea\u7c7b\u4eba\u7fa4\u3001\u4ea4\u4ed8\u4ec0\u4e48\u7ed3\u679c\u3002',
                summary or _FALLBACK_SUMMARY,
                '\u5148\u628a\u8fb9\u754c\u5212\u5b9a\u6e05\u695a\uff0c\u624d\u80fd\u5728\u540e\u7eed\u65b9\u6cd5\u9009\u62e9\u548c\u8d44\u6e90\u6295\u5165\u4e0a\u4e0d\u8dd1\u504f\u3002',
                '\u5efa\u8bae\u5728\u6b63\u5f0f\u53d1\u5e03\u524d\uff0c\u7528\u4e00\u4e24\u53e5\u8bdd\u660e\u786e\u672c\u6587\u7684\u76ee\u6807\u8bfb\u8005\u548c\u4ed6\u4eec\u6700\u5173\u5fc3\u7684\u7ed3\u679c\u3002',
            ),
            'image_brief': '\u65b9\u6cd5\u578b\u6587\u7ae0\u7684\u6982\u5ff5\u68b3\u7406\u573a\u666f\uff0c\u65e0\u6587\u5b57',
        },
        {
            'heading': h2,
            'body': T(
                '\u56f4\u7ed5', LQ, title_text, RQ, '\uff0c\u53ef\u4ee5\u4ece\u4e09\u4e2a\u65b9\u5411\u9009\u62e9\u53ef\u884c\u8def\u5f84\u3002',
                '\u7b2c\u4e00\uff0c\u4ece\u5df2\u6709\u5de5\u5177\u6216\u6d41\u7a0b\u7684\u4f18\u5316\u5165\u624b\u2014\u2014\u5148\u68b3\u7406\u5f53\u524d\u505a\u6cd5\u4e2d\u8017\u65f6\u6700\u591a\u6216\u51fa\u9519\u6982\u7387\u6700\u9ad8\u7684\u73af\u8282\uff0c',
                '\u518d\u8bc4\u4f30\u5de5\u5177\u662f\u5426\u80fd\u5728\u8be5\u73af\u8282\u63d0\u5347\u6548\u7387\u6216\u964d\u4f4e\u8fd4\u5de5\u7387\u3002',
                '\u7b2c\u4e8c\uff0c\u4ece\u6807\u51c6\u5316\u4ea4\u4ed8\u5207\u5165\u2014\u2014\u628a\u4e00\u6b21\u6027\u7684\u624b\u5de5\u64cd\u4f5c\u6574\u7406\u6210\u53ef\u91cd\u590d\u4f7f\u7528\u7684\u6a21\u677f\u3001\u6e05\u5355\u6216\u811a\u672c\uff0c',
                '\u8ba9\u6bcf\u6b21\u4ea4\u4ed8\u6210\u672c\u548c\u54c1\u8d28\u66f4\u53ef\u63a7\u3002',
                '\u7b2c\u4e09\uff0c\u4ece\u5c0f\u8303\u56f4\u8bd5\u70b9\u5f00\u59cb\u2014\u2014\u4e0d\u5fc5\u7b49\u65b9\u6848\u5b8c\u7f8e\uff0c\u5148\u5728\u771f\u5b9e\u573a\u666f\u91cc\u8dd1\u4e00\u4e24\u4e2a\u95ed\u73af\uff0c',
                '\u7528\u53cd\u9988\u6570\u636e\u51b3\u5b9a\u662f\u5426\u7ee7\u7eed\u6295\u5165\u3002',
                '\u4e09\u79cd\u65b9\u5411\u4e0d\u4e92\u65a5\uff0c\u5173\u952e\u662f\u5148\u627e\u5230\u4e00\u4e2a\u6700\u5c0f\u95ed\u73af\u9a8c\u8bc1\u53ef\u884c\u6027\u3002',
            ),
            'image_brief': '\u5de5\u4f5c\u53f0\u3001\u6d41\u7a0b\u5361\u7247\u3001\u670d\u52a1\u4ea4\u4ed8\u573a\u666f\uff0c\u65e0\u6587\u5b57',
        },
        {
            'heading': h3,
            'body': T(
                '\u5efa\u8bae\u6309\u4ee5\u4e0b\u987a\u5e8f\u63a8\u8fdb\uff1a\u7b2c\u4e00\u6b65\uff0c\u82b1\u534a\u5929\u628a\u5f53\u524d\u5df2\u6709\u7684\u4fe1\u606f\u3001\u5de5\u5177\u548c\u8d44\u6e90\u5217\u6e05\u695a\uff0c',
                '\u786e\u8ba4\u54ea\u4e9b\u662f\u73b0\u6210\u7684\u3001\u54ea\u4e9b\u9700\u8981\u8865\u3002\u7b2c\u4e8c\u6b65\uff0c\u7528\u73b0\u6709\u6750\u6599\u505a\u51fa\u4e00\u4efd\u6700\u5c0f\u53ef\u884c\u7248\u672c\u2014\u2014',
                '\u54ea\u6015\u53ea\u662f\u4e00\u9875\u6587\u6863\u3001\u4e00\u5f20\u622a\u56fe\u6216\u4e00\u6b21\u8bd5\u4ea4\u4ed8\u3002\u7b2c\u4e09\u6b65\uff0c\u5e26\u7740\u8fd9\u4e2a\u7248\u672c\u627e\u5230\u613f\u610f\u53cd\u9988\u7684\u771f\u5b9e\u7528\u6237\uff0c',
                '\u8bb0\u5f55\u5bf9\u65b9\u6700\u5173\u5fc3\u7684\u95ee\u9898\u548c\u72b9\u8c6b\u7684\u539f\u56e0\u3002\u7b2c\u56db\u6b65\uff0c\u6839\u636e\u53cd\u9988\u628a\u6d41\u7a0b\u7cbe\u7b80\u5230\u4e09\u6b65\u4ee5\u5185\uff0c',
                '\u53bb\u6389\u7528\u6237\u4e0d\u5173\u5fc3\u7684\u73af\u8282\u3002\u7b2c\u4e94\u6b65\uff0c\u56fa\u5b9a\u4ea4\u4ed8\u8def\u5f84\u5e76\u8bbe\u5b9a\u4e00\u4e2a\u4fdd\u5b88\u62a5\u4ef7\uff0c',
                '\u5148\u5b8c\u6574\u8dd1\u901a\u4e00\u7b14\u518d\u8003\u8651\u4f18\u5316\u548c\u63d0\u4ef7\u3002',
                '\u6bcf\u5b8c\u6210\u4e00\u6b65\u90fd\u5efa\u8bae\u8bb0\u5f55\u7528\u65f6\u548c\u5361\u70b9\uff0c\u65b9\u4fbf\u540e\u7eed\u8fed\u4ee3\u548c\u65b0\u4eba\u4e0a\u624b\u3002',
            ),
            'image_brief': '\u8ba1\u5212\u6e05\u5355\u548c\u6267\u884c\u6b65\u9aa4\u573a\u666f\uff0c\u65e0\u6587\u5b57',
        },
        {
            'heading': h4,
            'body': T(
                '\u5728\u63a8\u8fdb', LQ, title_text, RQ, '\u7684\u8fc7\u7a0b\u4e2d\uff0c\u6709\u51e0\u4e2a\u5e38\u89c1\u8bef\u533a\u548c\u771f\u5b9e\u98ce\u9669\u9700\u8981\u63d0\u524d\u6ce8\u610f\u3002',
                '\u7b2c\u4e00\uff0c\u4e0d\u8981\u627f\u8bfa\u65e0\u6cd5\u9a8c\u8bc1\u7684\u7ed3\u679c\u6216\u7a33\u5b9a\u6536\u5165\u2014\u2014',
                '\u4efb\u4f55\u65b9\u6cd5\u7684\u6548\u679c\u90fd\u53d7\u9650\u4e8e\u5177\u4f53\u573a\u666f\u548c\u6267\u884c\u4eba\uff0c\u7b3c\u7edf\u627f\u8bfa\u5bb9\u6613\u5f15\u53d1\u7ea0\u7eb7\u3002',
                '\u7b2c\u4e8c\uff0c\u4e0d\u8981\u4f7f\u7528\u672a\u7ecf\u6388\u6743\u7684\u7d20\u6750\u3001\u6570\u636e\u6216\u4ee3\u7801\u2014\u2014',
                '\u6d89\u53ca\u7248\u6743\u548c\u5e73\u53f0\u89c4\u5219\u65f6\uff0c\u4fdd\u7559\u4eba\u5de5\u6838\u5bf9\u73af\u8282\u6bd4\u8ffd\u6c42\u5168\u81ea\u52a8\u66f4\u91cd\u8981\u3002',
                '\u7b2c\u4e09\uff0c\u628a\u5de5\u5177\u8ba2\u9605\u3001\u5b66\u4e60\u6210\u672c\u548c\u8fd4\u5de5\u65f6\u95f4\u7eb3\u5165\u9884\u7b97\u2014\u2014',
                '\u5f88\u591a\u4eba\u5728\u65e9\u671f\u4f4e\u4f30\u4e86\u8fd9\u4e9b\u9690\u6027\u652f\u51fa\u3002',
                '\u7b2c\u56db\uff0c\u672a\u7ecf\u9a8c\u8bc1\u7684\u81ea\u52a8\u751f\u6210\u5185\u5bb9\u4e0d\u8981\u76f4\u63a5\u4ea4\u4ed8\u7ed9\u5ba2\u6237\u2014\u2014',
                '\u4e8b\u5b9e\u9519\u8bef\u3001\u6570\u636e\u504f\u5dee\u548c\u8bed\u6c14\u4e0d\u5f53\u90fd\u53ef\u80fd\u635f\u5bb3\u4fe1\u4efb\u3002',
            ),
            'image_brief': '\u98ce\u9669\u63a7\u5236\u548c\u590d\u6838\u573a\u666f\uff0c\u65e0\u6587\u5b57',
        },
        {
            'heading': h5,
            'body': T(
                LQ, title_text, RQ, '\u8fd9\u4ef6\u4e8b\u7684\u8d77\u70b9\u4e0d\u662f\u627e\u5230\u5b8c\u7f8e\u65b9\u6848\uff0c',
                '\u800c\u662f\u5148\u8dd1\u901a\u4e00\u4e2a\u6700\u5c0f\u4ea4\u4ed8\u95ed\u73af\u2014\u2014\u7528\u73b0\u6709\u5de5\u5177\u5b8c\u6210\u4e00\u6b21\u771f\u5b9e\u670d\u52a1\uff0c',
                '\u62ff\u5230\u53cd\u9988\u540e\u518d\u51b3\u5b9a\u662f\u5426\u7ee7\u7eed\u4f18\u5316\u3001\u6269\u5c55\u6216\u6362\u65b9\u5411\u3002',
                '\u8fd9\u4efd\u57fa\u7840\u7a3f\u7684\u7ed3\u6784\u53ef\u4ee5\u957f\u671f\u590d\u7528\uff1a\u6838\u5fc3\u6982\u5ff5\u2192\u53ef\u884c\u8def\u5f84\u2192\u5177\u4f53\u6b65\u9aa4\u2192\u98ce\u9669\u63d0\u9192\u2192\u603b\u7ed3\uff0c',
                '\u6bcf\u6b21\u53ea\u9700\u586b\u5145\u5f53\u524d\u573a\u666f\u7684\u5177\u4f53\u4fe1\u606f\u3002',
                '\u5efa\u8bae\u68c0\u67e5\u6587\u672c\u6a21\u578b\u914d\u7f6e\u540e\u70b9\u51fb', LQ, '\u4f7f\u7528\u6587\u672c\u6a21\u578b\u91cd\u65b0\u751f\u6210', RQ, '\uff0c',
                '\u8ba9\u6b63\u5f0f\u6a21\u578b\u5728\u8be5\u7ed3\u6784\u57fa\u7840\u4e0a\u8865\u5145\u66f4\u4e30\u5bcc\u7684\u6848\u4f8b\u548c\u8bed\u6c14\u3002',
            ),
            'image_brief': '\u603b\u7ed3\u548c\u4e0b\u4e00\u6b65\u884c\u52a8\u573a\u666f\uff0c\u65e0\u6587\u5b57',
        },
    ]
    source_list = normalize_source_list(
        [
            {
                'publisher': source_name,
                'title': title_text,
                'published_at': captured_at,
                'url': source_url,
            }
        ]
    )
    ai_statement = ''
    format_warning_body = '\u6587\u672c\u6a21\u578b\u8fd4\u56de\u6b63\u6587\u4e0d\u8db3700\u5b57\uff0c\u5df2\u81ea\u52a8\u6269\u5c55\u4e3a\u5b8c\u6574\u53ef\u7f16\u8f91\u57fa\u7840\u7a3f\u3002'
    format_warning_tip = '\u5efa\u8bae\u68c0\u67e5\u6a21\u578b\u914d\u7f6e\u540e\u70b9\u51fb' + LQ + '\u4f7f\u7528\u6587\u672c\u6a21\u578b\u91cd\u65b0\u751f\u6210' + RQ + '\u3002'
    article = {
        'title': title,
        'intro': intro,
        'summary': summary or intro,
        'sections': sections,
        'source_list': source_list,
        'source_statement': '\n\n'.join(source_list),
        'ai_statement': ai_statement,
        'fact_basis': [],
        'body_char_count': 0,
        'text_generation_calls': 1,
        'text_generation_limit': 1,
        'text_generation_second_call_reason': '',
        'recommended_status': 'review_required',
        'fallback_reason': 'BODY_TOO_SHORT_EXPANDED',
        'fallback_kind': 'custom_topic_expanded',
        'used_local_fallback': True,
        'response_format_warning': True,
        'format_warning': format_warning_body + format_warning_tip,
        'fallback_complete': True,
        'content_markdown': '',
    }
    markdown_parts = [f'# {title}', intro]
    for section in sections:
        heading = section['heading']
        body = section['body']
        markdown_parts.append(f'## {heading}\n{body}')
    article['content_markdown'] = '\n\n'.join(part for part in markdown_parts if part).strip()
    article['body_char_count'] = count_body_chinese_chars(article)
    return article


def _build_custom_topic_fallback_article(topic: HotTopic, angle: dict[str, Any], article_type: str, style: str, reason: str) -> dict[str, Any]:
    title_text = str(topic.title or "自定义话题").strip()
    summary = str(topic.summary or "").strip()
    source_url = str(topic.source_url or topic.url or "").strip()
    title = f"{title_text}：一套可执行的入门方案"
    intro = "文本模型调用失败，当前展示的是可编辑基础框架。本文先按用户输入的话题搭建方法型结构，便于继续补充案例、数据和个人经验。"
    sections = [
        {
            "heading": "核心概念",
            "body": f"围绕“{title_text}”，首先要明确它不是单纯追逐技巧，而是把工具能力转化为可交付的服务、内容或流程。{summary or '用户未提供额外说明，后续可补充目标人群、预算和期望成果。'}",
            "image_brief": "方法型文章的概念梳理场景，无文字",
        },
        {
            "heading": "可执行方法",
            "body": "可以从低成本、轻交付、可复用三个方向选择路径：用工具提升内容生产效率，用标准化模板承接简单需求，或把重复工作整理成小服务。每一种方法都应对应清晰结果，而不是只展示工具本身。",
            "image_brief": "工作台、流程卡片、服务交付场景，无文字",
        },
        {
            "heading": "具体步骤",
            "body": "第一步确定一个具体场景，第二步做出可展示样例，第三步找到愿意付费的目标用户，第四步用固定流程完成交付，第五步记录反馈并优化报价。先跑通一笔小订单，再考虑扩大规模。",
            "image_brief": "计划清单和执行步骤场景，无文字",
        },
        {
            "heading": "风险提醒",
            "body": "需要控制学习成本、工具订阅成本和承诺范围。不要承诺无法验证的收益，不要使用未授权素材，也不要把自动生成内容直接交付给客户。涉及合同、版权和平台规则时，应保留人工核对环节。",
            "image_brief": "风险控制和复核场景，无文字",
        },
        {
            "heading": "总结",
            "body": "这份框架适合作为继续编辑的底稿。建议检查模型配置后点击“使用文本模型重新生成”，让正式文本模型在该结构基础上补充更完整案例、语气和段落细节。",
            "image_brief": "总结和下一步行动场景，无文字",
        },
    ]
    source_list = normalize_source_list(
        [
            {
                "publisher": topic.source_name or "手动输入",
                "title": title_text,
                "published_at": topic.captured_at,
                "url": source_url,
            }
        ]
    )
    ai_statement = ""
    article = {
        "title": title,
        "intro": intro,
        "summary": summary or intro,
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
        "fallback_kind": "custom_topic_fallback",
        "used_local_fallback": True,
        "response_format_warning": True,
        "format_warning": "文本模型调用失败，当前展示的是可编辑基础框架。建议检查模型配置后点击“使用文本模型重新生成”。",
        "fallback_complete": True,
        "content_markdown": "",
    }
    markdown_parts = [f"# {title}", intro]
    for section in sections:
        markdown_parts.append(f"## {section['heading']}\n{section['body']}")
    article["content_markdown"] = "\n\n".join(part for part in markdown_parts if part).strip()
    article["body_char_count"] = count_body_chinese_chars(article)
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
    commit_record: dict[str, Any] | None = None
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
        configured_timeout = int(text_profile.get("timeout_seconds") or (settings.get("network") or {}).get("timeout_seconds") or 150)
        # Formal article generation uses a delivery timeout window; short connection-test timeouts must not leak here.
        text_timeout_limit = max(90, min(180, configured_timeout))
        effective_text_profile = dict(text_profile)
        effective_text_profile["timeout_seconds"] = text_timeout_limit
        if bool(effective_text_profile.get("has_api_key")) and not str(effective_text_profile.get("api_key") or "").strip():
            return _failure(state, store, "generating_article", ProviderError("TEXT_KEY_LOAD_FAILED", "已保存的文本密钥无法读取，请重新保存文本配置。"), "failed")
        state["model_info"] = {"text": _safe_model_info(effective_text_profile), "image": _safe_model_info(image_profile)}
        state["text_model_name"] = str(effective_text_profile.get("model") or effective_text_profile.get("name") or "")
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
        if retry_step == "retry-cover":
            execution_image_plan["inline_count"] = 0
            execution_image_plan["inline_max"] = 0
            execution_image_plan["max_calls"] = int(execution_image_plan.get("cover") or 0)
        state["image_plan"] = sanitize_json(requested_image_plan)
        approved_calls = int(execution_image_plan.get("max_calls") or 0)
        if retry_step == "retry-cover" or rewrite_run:
            previous_calls = int((state.get("image_usage") or {}).get("generation_calls") or state.get("image_generation_calls") or 0)
            approved_calls += previous_calls
        set_approved_image_budget(state, approved_calls)
        state["pending_image_confirmation"] = bool(requested_image_plan.get("max_calls")) and not auto_image_requested
        bundle = _auto_collect_research(state, store, topic)
        accepted_source_count = int((bundle or {}).get("accepted_source_count") or 0)
        limited_research_mode = _is_hotlist_limited_bundle(bundle)
        custom_topic_mode = _is_custom_topic_bundle(bundle)
        if not bundle:
            return _quality_block(state, store, bundle or {"research_status": "not_collected", "topic_id": topic.id, "topic_title": topic.title}, "有效资料来源为 0，无法生成文章。", "RESEARCH_NOT_COLLECTED")
        if accepted_source_count <= 0 and not limited_research_mode and not custom_topic_mode:
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
            generation_stats = {"text_generation_calls": 0, "text_generation_limit": 3, "text_generation_second_call_reason": "", "text_generation_call_reasons": []}
            try:
                state["text_model_started_at"] = utc_now()
                state["text_model_finished_at"] = None
                state["provider_error_code"] = ""
                state["provider_error_message"] = ""
                state["request_endpoint"] = str(effective_text_profile.get("base_url") or "")[:200]
                state["response_format"] = "none"
                state["text_timeout_seconds"] = int(text_timeout_limit)
                state["text_max_tokens"] = 1400
                text_started = time.perf_counter()
                _persist(state, store)
                article = generate_article(topic, angle, article_type, style, word_count, effective_text_profile, demo_mode=False, app_mode="production", network_settings=settings.get("network"), rewrite_context=rewrite_context, research_bundle=bundle, generation_stats=generation_stats)
                state["text_model_finished_at"] = utc_now()
                state["text_generation_result"] = "success"
                state["text_model_elapsed_seconds"] = round(time.perf_counter() - text_started, 1)
                state["text_http_status"] = 200
                # ── R1.2: custom_topic short article auto-expand ──
                if custom_topic_mode and not used_fallback:
                    body_chars = count_body_chinese_chars(article)
                    if body_chars < 700:
                        used_fallback = True
                        state["fallback_notice"] = "文本模型返回正文较短（{}字），已自动扩展为完整可编辑基础稿。建议检查模型配置后重新生成。".format(body_chars)
                        state["provider_error_code"] = "BODY_TOO_SHORT_EXPANDED"
                        state["provider_error_message"] = state["fallback_notice"]
                        state["text_generation_result"] = "expanded"
                        article = _build_expanded_custom_topic_article(topic, angle, article_type, style, article)
                if str(article.get("content_warning_code") or "") == "CONTENT_TOO_SHORT":
                    state["provider_error_code"] = "CONTENT_TOO_SHORT"
                    state["provider_error_message"] = str(article.get("warning_note") or "模型返回正文偏短，已保留原始可编辑正文。")
                    state["text_generation_result"] = "warning"
            except ProviderError as exc:
                state["text_model_finished_at"] = utc_now()
                state["text_model_elapsed_seconds"] = round(time.perf_counter() - text_started, 1)
                state["provider_error_code"] = str(exc.code)
                state["provider_error_message"] = redact_sensitive_text(str(getattr(exc, "detail", exc)))[:500]
                state["text_http_status"] = int((getattr(exc, "details", {}) or {}).get("http_status") or 0)
                if exc.code not in {"TIMEOUT", "TLS_ERROR", "ARTICLE_TOO_SHORT", "INVALID_RESPONSE", "MODEL_NOT_CONFIGURED", "ARTICLE_PARSE_ERROR", "MODEL_OUTPUT_EMPTY", "MODEL_OUTPUT_REASONING_ONLY", "PROVIDER_INTERNAL_ERROR"}:
                    raise
                used_fallback = True
                state["text_generation_result"] = "fallback"
                provider_code = str(exc.code)
                retry_message = user_facing_error_message(provider_code, "公开资料已保存，但正文模型返回异常，请重新生成正文。资料不会丢失。")
                gate = {
                    "status": "failed",
                    "passed": False,
                    "hard_error_count": 1,
                    "warning_count": 0,
                    "hard_errors": [provider_code],
                    "warnings": [],
                    "reasons": ["ARTICLE_TEXT_RETRY_REQUIRED", provider_code],
                    "metrics": {"source_count": accepted_source_count},
                }
                state["research_bundle"] = sanitize_json(bundle or {})
                state["quality_gate"] = sanitize_json(gate)
                state["article"] = None
                state["cover"] = None
                state["inline_images"] = []
                state["inline_image_summary"] = {"total": 0, "completed": 0, "failed": 0, "pending": 0, "status": "blocked"}
                state["fallback_notice"] = retry_message
                state.update({
                    "status": "failed",
                    "stage": "generating_article",
                    "progress": 35,
                    "failed_step": "generating_article",
                    "error_code": provider_code,
                    "safe_error_message": retry_message,
                    "retryable": is_retryable_error(provider_code),
                    "used_local_fallback": False,
                    "fallback_kind": "",
                    "image_usage": {"generation_calls": 0, "paid_calls": 0, "retry_calls": 0, "budget_exceeded": False},
                })
                return _persist(state, store)

            def _finalize_article_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
                payload["summary"] = str(payload.get("summary") or payload.get("intro") or topic.summary or "").strip()
                payload.update({"topic_id": topic.id, "topic_title": topic.title, "source": topic.source, "source_name": topic.source_name, "source_url": topic.source_url, "captured_at": topic.captured_at, "article_type": article_type, "style": style, "word_count": word_count, "status": "completed", "demo_mode": False, "angle_id": state.get("angle_id"), "angle_name": state.get("angle_name"), "angle_plan": sanitize_sensitive_data(angle)})
                payload["article_plan"] = state["article_plan"]
                cleaned_payload = sanitize_article_hard_facts(payload, bundle)
                return cleaned_payload["article"], list(cleaned_payload.get("removed_claims") or [])

            _check_cancel(task["task_id"])
            article, removed_claims = _finalize_article_payload(article)
            post_sanitize_body_chars = count_body_chinese_chars(article)
            if (
                run_article
                and not bool(used_fallback or article.get("used_local_fallback"))
                and post_sanitize_body_chars >= 600
                and post_sanitize_body_chars < _rewrite_min_chars(word_count)
                and int(generation_stats.get("text_generation_calls") or 0) < int(generation_stats.get("text_generation_limit") or 3)
            ):
                required_headings = CUSTOM_TOPIC_SECTION_HEADINGS if custom_topic_mode else REQUIRED_SECTION_HEADINGS
                rewrite_provider = OpenAITextProvider(effective_text_profile, network_settings=settings.get("network"))
                article, rewrite_diagnostic = _apply_short_article_rewrite(
                    provider=rewrite_provider,
                    topic=topic,
                    angle=angle,
                    article_type=article_type,
                    style=style,
                    requested_word_count=word_count,
                    article=article,
                    body_count=post_sanitize_body_chars,
                    required_headings=required_headings,
                    research_bundle=bundle,
                    stats=generation_stats,
                )
                if rewrite_diagnostic:
                    state["text_http_status"] = int(rewrite_diagnostic.get("http_status") or state.get("text_http_status") or 0)
                    state["text_content_type"] = str(rewrite_diagnostic.get("content_type") or state.get("text_content_type") or "")
                    state["provider_parser_mode"] = str(rewrite_diagnostic.get("parser_mode") or state.get("provider_parser_mode") or "")
                    article["text_http_status"] = state["text_http_status"]
                    article["text_content_type"] = state["text_content_type"]
                    article["provider_parser_mode"] = state["provider_parser_mode"]
                    article["request_timeout_seconds"] = rewrite_diagnostic.get("timeout_seconds") or article.get("request_timeout_seconds")
                article, removed_claims = _finalize_article_payload(article)
            overlap_report = analyze_source_overlap(article, bundle)
            gate = quality_gate(article, bundle)
            if (
                run_article
                and str(gate.get("status") or "") == "failed"
                and not bool(used_fallback or article.get("used_local_fallback"))
                and int(generation_stats.get("text_generation_calls") or 0) < int(generation_stats.get("text_generation_limit") or 3)
            ):
                required_headings = CUSTOM_TOPIC_SECTION_HEADINGS if custom_topic_mode else REQUIRED_SECTION_HEADINGS
                issue_rewrite_provider = OpenAITextProvider(effective_text_profile, network_settings=settings.get("network"))
                try:
                    article, rewrite_diagnostic = _apply_quality_issue_rewrite(
                        provider=issue_rewrite_provider,
                        topic=topic,
                        angle=angle,
                        article_type=article_type,
                        style=style,
                        requested_word_count=word_count,
                        article=article,
                        issue_list=[str(item) for item in (gate.get("hard_errors") or gate.get("reasons") or [])],
                        required_headings=required_headings,
                        research_bundle=bundle,
                        stats=generation_stats,
                    )
                    if rewrite_diagnostic:
                        state["text_http_status"] = int(rewrite_diagnostic.get("http_status") or state.get("text_http_status") or 0)
                        state["text_content_type"] = str(rewrite_diagnostic.get("content_type") or state.get("text_content_type") or "")
                        state["provider_parser_mode"] = str(rewrite_diagnostic.get("parser_mode") or state.get("provider_parser_mode") or "")
                        article["text_http_status"] = state["text_http_status"]
                        article["text_content_type"] = state["text_content_type"]
                        article["provider_parser_mode"] = state["provider_parser_mode"]
                    article, removed_claims = _finalize_article_payload(article)
                    overlap_report = analyze_source_overlap(article, bundle)
                    gate = quality_gate(article, bundle)
                except Exception as rewrite_error:
                    article["quality_rewrite_failed"] = True
                    article["quality_rewrite_error"] = redact_sensitive_text(str(rewrite_error))[:240]
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
            # ── 质量门一致性：failed 不得继续标 completed ──
            if str(gate.get("status") or "") == "failed":
                state["research_bundle"] = sanitize_json(bundle or {})
                state["quality_gate"] = sanitize_json(gate)
                failed_reasons = "；".join(gate.get("hard_errors", []) or gate.get("reasons", []))
                code = "ARTICLE_TEXT_RETRY_REQUIRED" if bool(used_fallback or article.get("used_local_fallback")) else "QUALITY_GATE_FAILED"
                state.update({
                    "status": "failed",
                    "stage": "quality_gate",
                    "progress": 45,
                    "failed_step": "quality_gate",
                    "error_code": code,
                    "safe_error_message": failed_reasons or "文章质量检查未通过",
                    "retryable": code == "ARTICLE_TEXT_RETRY_REQUIRED",
                    "image_usage": {"generation_calls": 0, "paid_calls": 0, "retry_calls": 0, "budget_exceeded": False},
                })
                return _persist(state, store)
            state["text_generation_calls"] = int(article.get("text_generation_calls") or generation_stats.get("text_generation_calls") or (0 if used_fallback else 1))
            state["text_generation_limit"] = int(article.get("text_generation_limit") or generation_stats.get("text_generation_limit") or 1)
            state["text_generation_second_call_reason"] = str(article.get("text_generation_second_call_reason") or generation_stats.get("text_generation_second_call_reason") or "")
            state["text_generation_call_reasons"] = sanitize_json(article.get("text_generation_call_reasons") or generation_stats.get("text_generation_call_reasons") or [])
            for index, reason in enumerate(state["text_generation_call_reasons"], start=1):
                state[f"text_generation_call_{index}_reason"] = str(reason)
            state["fallback_reason"] = str(article.get("fallback_reason") or "")
            state["fallback_kind"] = str(article.get("fallback_kind") or "")
            state["used_local_fallback"] = bool(used_fallback or article.get("used_local_fallback"))
            state["response_parser_mode"] = str(article.get("response_parser_mode") or "")
            state["text_content_type"] = str(article.get("text_content_type") or "")
            state["provider_parser_mode"] = str(article.get("provider_parser_mode") or "")
            state["request_timeout_seconds"] = article.get("request_timeout_seconds")
            article["text_model_name"] = state.get("text_model_name") or ""
            article["text_model_started_at"] = state.get("text_model_started_at")
            article["text_model_finished_at"] = state.get("text_model_finished_at")
            article["provider_error_code"] = state.get("provider_error_code") or ""
            article["provider_error_message"] = state.get("provider_error_message") or ""
            article["fallback_kind"] = article.get("fallback_kind") or state.get("fallback_kind") or ""
            article["fallback_reason"] = article.get("fallback_reason") or state.get("fallback_reason") or ""
            article["used_local_fallback"] = bool(state.get("used_local_fallback"))
            article["response_parser_mode"] = state.get("response_parser_mode") or article.get("response_parser_mode") or ""
            article["source_statement"] = article.get("source_statement") or "\uff1b".join(str(item) for item in article.get("source_list") or [])
            if state["used_local_fallback"]:
                state["fallback_notice"] = state.get("fallback_notice") or "本篇未使用文本模型正式正文，当前展示可编辑基础框架。"
            elif str(article.get("content_warning_code") or "") == "CONTENT_TOO_SHORT" and not state.get("fallback_notice"):
                state["fallback_notice"] = str(article.get("warning_note") or "模型返回正文偏短，建议主动重新生成。")
            elif not used_fallback and article.get("response_format_warning") and not state.get("fallback_notice"):
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
        generate_parameters = inspect.signature(image_provider.generate).parameters
        if "cancel_check" in generate_parameters:
            image_provider.generate(cover_prompt, raw_path, cancel_check=lambda: is_cancel_requested(task["task_id"]))
        else:
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


