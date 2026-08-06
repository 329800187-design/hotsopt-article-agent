"""Customer-facing article, image, fusion, and export workflow state.

The generation status describes background work.  This state describes the
explicit review gates the customer must pass before a deliverable is made.
"""

from __future__ import annotations

from datetime import datetime, timezone
from copy import deepcopy
from typing import Any, Iterable

from providers.text_provider import ProviderError


WORKFLOW_STATES = (
    "article_draft",
    "article_pending_confirmation",
    "article_confirmed",
    "images_pending_generation",
    "images_generating",
    "images_pending_confirmation",
    "fusion_pending",
    "final_draft_pending_preview",
    "final_draft_confirmed",
    "export_ready",
    "exported",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _image_items(state: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    cover = state.get("cover") or {}
    if cover.get("status") == "completed":
        items.append(cover)
    items.extend(item for item in state.get("inline_images") or [] if isinstance(item, dict))
    return items


def completed_image_items(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in _image_items(state) if item.get("status") == "completed" and (item.get("path") or item.get("file_path"))]


def image_workflow_gate(state: dict[str, Any] | None) -> dict[str, Any]:
    """Return an explicit customer-facing image gate instead of hiding actions."""
    value = state or {}
    reasons: list[str] = []
    if not value.get("article"):
        reasons.append("文章正文尚未生成完成")
    if str(value.get("status") or "") not in {"completed", "partial_success", "review_required"}:
        reasons.append("文章任务尚未完成")
    gate = value.get("quality_gate") or {}
    if str(gate.get("status") or "") not in {"passed", "warning"} or int(gate.get("hard_error_count") or 0) > 0:
        reasons.append("文章质量检查尚未通过")
    workflow_state = str(initialize_workflow(value).get("workflow_state") or "")
    if workflow_state == "article_pending_confirmation":
        reasons.append("请先确认文章内容")
    elif workflow_state not in {"article_confirmed", "images_pending_generation", "images_generating", "images_pending_confirmation", "fusion_pending", "final_draft_pending_preview", "final_draft_confirmed", "export_ready", "exported"}:
        reasons.append(f"当前流程阶段为 {workflow_state or '未知'}")
    if workflow_state in {"images_pending_confirmation", "fusion_pending", "final_draft_pending_preview", "final_draft_confirmed", "export_ready", "exported"} and not completed_image_items(value):
        reasons.append("没有可用的已生成图片")
    return {"ready": not reasons, "workflow_state": workflow_state, "reasons": reasons}


def sync_article_images(state: dict[str, Any]) -> dict[str, Any]:
    """Keep the customer-visible article aligned with persisted image slots."""
    article = dict(state.get("article") or {})
    images = completed_image_items(state)
    if images:
        article["images"] = [dict(item) for item in images]
        cover = next((item for item in images if item.get("role") == "cover"), None)
        if cover:
            article["cover"] = dict(cover)
    state["article"] = article
    return state


def set_workflow_state(state: dict[str, Any], value: str, *, at: str | None = None) -> dict[str, Any]:
    if value not in WORKFLOW_STATES:
        raise ValueError(f"unknown workflow state: {value}")
    state["workflow_state"] = value
    state["workflow_updated_at"] = at or _now()
    return state


def initialize_workflow(state: dict[str, Any]) -> dict[str, Any]:
    """Backfill legacy task state without treating old artifacts as accepted."""
    if state.get("workflow_state") in WORKFLOW_STATES:
        return state
    if not state.get("article"):
        return set_workflow_state(state, "article_draft")
    return set_workflow_state(state, "article_pending_confirmation")


def require_workflow(state: dict[str, Any], allowed: Iterable[str], code: str = "WORKFLOW_NOT_READY") -> None:
    current = str(initialize_workflow(state).get("workflow_state") or "")
    if current not in set(allowed):
        raise ProviderError(code, f"当前阶段为 {current}，暂不能执行此操作")


def confirm_article(state: dict[str, Any]) -> dict[str, Any]:
    initialize_workflow(state)
    require_workflow(state, ("article_pending_confirmation", "article_confirmed"), "ARTICLE_CONFIRMATION_REQUIRED")
    state["article_confirmation"] = {"status": "confirmed", "confirmed_at": _now()}
    set_workflow_state(state, "article_confirmed")
    return state


def cancel_article_confirmation(state: dict[str, Any]) -> dict[str, Any]:
    initialize_workflow(state)
    state["article_confirmation"] = {"status": "pending", "updated_at": _now()}
    set_workflow_state(state, "article_pending_confirmation")
    return state


def invalidate_after_article_change(state: dict[str, Any]) -> dict[str, Any]:
    """Editing or restoring text invalidates every downstream customer gate."""
    state["article_confirmation"] = {"status": "pending", "updated_at": _now()}
    state["image_confirmation"] = {"status": "pending", "updated_at": _now()}
    state["fusion_status"] = {"status": "not_started", "updated_at": _now(), "model_calls": 0}
    state["final_draft_status"] = {"status": "not_started", "updated_at": _now()}
    state["export_status"] = {"status": "not_ready", "updated_at": _now()}
    return set_workflow_state(state, "article_pending_confirmation")


def begin_image_generation(state: dict[str, Any]) -> dict[str, Any]:
    require_workflow(state, ("article_confirmed", "images_pending_generation", "images_pending_confirmation"))
    state["image_confirmation"] = {"status": "pending", "updated_at": _now()}
    set_workflow_state(state, "images_generating")
    return state


def finish_image_generation(state: dict[str, Any]) -> dict[str, Any]:
    initialize_workflow(state)
    items = completed_image_items(state)
    if items:
        state["image_confirmation"] = {"status": "pending", "updated_at": _now(), "completed_count": len(items)}
        set_workflow_state(state, "images_pending_confirmation")
    else:
        set_workflow_state(state, "images_pending_generation")
    return state


def confirm_images(state: dict[str, Any], image_ids: Iterable[str] | None = None) -> dict[str, Any]:
    require_workflow(state, ("images_pending_confirmation", "fusion_pending"), "IMAGE_CONFIRMATION_REQUIRED")
    sync_article_images(state)
    available = completed_image_items(state)
    selected = {str(item) for item in (image_ids or []) if str(item).strip()}
    if selected:
        available_ids = {str(item.get("image_id") or item.get("role") or "") for item in available}
        if not selected.issubset(available_ids):
            raise ProviderError("IMAGE_CONFIRMATION_INVALID", "确认的图片中包含不可用图片")
    if not available:
        raise ProviderError("IMAGE_NOT_READY", "没有可确认的已完成图片")
    state["image_confirmation"] = {"status": "confirmed", "confirmed_at": _now(), "image_ids": sorted(selected) or [str(item.get("image_id") or item.get("role") or "") for item in available]}
    set_workflow_state(state, "fusion_pending")
    return state


def prepare_fusion(state: dict[str, Any]) -> dict[str, Any]:
    require_workflow(state, ("fusion_pending", "final_draft_pending_preview", "final_draft_confirmed", "export_ready"), "FUSION_CONFIRMATION_REQUIRED")
    sync_article_images(state)
    final_document = deepcopy(state.get("article") or {})
    available = completed_image_items(state)
    confirmed_ids = set(str(item) for item in ((state.get("image_confirmation") or {}).get("image_ids") or []) if str(item).strip())
    final_images = [item for item in available if not confirmed_ids or str(item.get("image_id") or item.get("slot_id") or "") in confirmed_ids]
    final_document["images"] = [deepcopy(item) for item in final_images]
    final_document["image_counts"] = {
        "successful_generation_count": len(available),
        "confirmed_image_count": len(final_images),
        "final_document_image_count": len(final_images),
    }
    final_document["document_kind"] = "final_document"
    final_document["generated_at"] = _now()
    state["final_document"] = final_document
    state["fusion_status"] = {"status": "preview_ready", "prepared_at": _now(), "model_calls": 0}
    set_workflow_state(state, "final_draft_pending_preview")
    return state


def confirm_final_draft(state: dict[str, Any]) -> dict[str, Any]:
    require_workflow(state, ("final_draft_pending_preview", "final_draft_confirmed", "export_ready"), "FINAL_DRAFT_PREVIEW_REQUIRED")
    state["final_draft_status"] = {"status": "confirmed", "confirmed_at": _now()}
    state["export_status"] = {"status": "ready", "updated_at": _now()}
    set_workflow_state(state, "final_draft_confirmed")
    return state


def require_export_ready(state: dict[str, Any]) -> None:
    require_workflow(state, ("final_draft_confirmed", "export_ready", "exported"), "FINAL_DRAFT_NOT_READY")
    if (state.get("fusion_status") or {}).get("status") != "preview_ready":
        raise ProviderError("FINAL_DRAFT_NOT_READY", "请先生成最终图文稿预览")
    if (state.get("final_draft_status") or {}).get("status") != "confirmed":
        raise ProviderError("FINAL_DRAFT_NOT_READY", "请先确认最终图文稿")
    if not isinstance(state.get("final_document"), dict):
        raise ProviderError("FINAL_DOCUMENT_MISSING", "最终图文稿尚未生成，请重新生成最终图文稿预览")


def mark_exported(state: dict[str, Any], kind: str) -> dict[str, Any]:
    state["export_status"] = {"status": "exported", "kind": kind, "exported_at": _now()}
    set_workflow_state(state, "exported")
    return state
