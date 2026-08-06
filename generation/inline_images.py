from __future__ import annotations

import hashlib
import inspect
import json
import shutil
import uuid
from pathlib import Path
from typing import Any, Iterable

from generation.image_budget import image_plan_for, normalize_image_plan
from generation.image_prompt_generator import plan_inline_image_assets
from generation.workflow import finish_image_generation
from modules.database import SQLiteStore, get_store
from modules.generation_store import generation_task_dir, load_generation_task, save_generation_task
from modules.models import utc_now
from modules.security import redact_sensitive_text, sanitize_json, sanitize_sensitive_data
from modules.task_locks import task_lock
from providers.errors import is_retryable_error, map_provider_exception
from providers.image_provider import OpenAIImageProvider, inspect_image
from providers.text_provider import ProviderError


INLINE_STATUSES = {"pending", "generating", "completed", "failed", "cancelled"}


def _sha(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _safe_image_id(image_id: str) -> bool:
    return bool(image_id and len(image_id) <= 40 and image_id.startswith("section-") and image_id[8:].isdigit())


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sanitize_json(value), ensure_ascii=False, indent=2), encoding="utf-8")


def _save_state(state: dict[str, Any], store: SQLiteStore | None = None) -> dict[str, Any]:
    current = load_generation_task(str(state["task_id"])) or {}
    version = int(current.get("state_version") or state.get("state_version") or 0)
    state["state_version"] = version + 1
    state["updated_at"] = utc_now()
    save_generation_task(state, expected_version=version)
    if store is not None:
        store.update_task_status(str(state["task_id"]), str(state.get("status") or "queued"))
    return state


def _summary(assets: list[dict[str, Any]]) -> dict[str, int | str]:
    completed = sum(item.get("status") == "completed" for item in assets)
    failed = sum(item.get("status") == "failed" for item in assets)
    pending = sum(item.get("status") in {"pending", "generating"} for item in assets)
    return {"total": len(assets), "completed": completed, "failed": failed, "pending": pending, "status": "completed" if not failed and not pending else "partial_success" if completed else "failed"}


def _target_assets(assets: list[dict[str, Any]], target_ids: Iterable[str] | None, regenerate_all: bool) -> list[dict[str, Any]]:
    if regenerate_all:
        return assets
    requested = list(target_ids or [])
    if not requested:
        requested = [str(item.get("image_id") or "") for item in assets if item.get("status") in {"failed", "pending"}]
    if not requested:
        return []
    if any(not _safe_image_id(item) for item in requested):
        raise ProviderError("INLINE_IMAGE_NOT_FOUND", "inline image does not exist")
    if len(set(requested)) != len(requested):
        raise ProviderError("INLINE_IMAGE_INVALID", "inline image selection is duplicated")
    found = {str(item.get("image_id")): item for item in assets}
    if any(image_id not in found for image_id in requested):
        raise ProviderError("INLINE_IMAGE_NOT_FOUND", "inline image does not exist")
    return [found[image_id] for image_id in requested]


def _output_root(task_root: Path, value: Path | None) -> Path:
    root = (value or task_root).resolve()
    try:
        root.relative_to(task_root.resolve())
    except ValueError as exc:
        raise ProviderError("INLINE_IMAGE_INVALID", "inline image output path is invalid") from exc
    root.mkdir(parents=True, exist_ok=True)
    return root


def _cover_items(article: dict[str, Any], cover: dict[str, Any] | None) -> list[dict[str, Any]]:
    items = [item for item in article.get("images") or [] if isinstance(item, dict) and item.get("role") == "cover"]
    if not items and cover:
        items = [{"role": "cover", **sanitize_sensitive_data(cover)}]
    return items


def _image_usage(state: dict[str, Any]) -> dict[str, Any]:
    usage = dict(state.get("image_usage") or {})
    has_explicit_budget = "approved_image_budget" in state or "approved_budget" in usage
    approved_budget = int(state.get("approved_image_budget") or usage.get("approved_budget") or 0)
    generation_calls = int(state.get("image_generation_calls") or usage.get("generation_calls") or 0)
    if approved_budget <= 0 and not has_explicit_budget:
        inline_count = len([item for item in state.get("inline_images") or [] if isinstance(item, dict)])
        plan_mode = str((state.get("generation_options") or {}).get("image_plan_mode") or "standard")
        plan = image_plan_for(1200, plan_mode)
        approved_budget = max(inline_count, int(plan.get("max_calls") or 0), generation_calls)
        usage["budget_inferred"] = True
    usage["approved_budget"] = max(0, approved_budget)
    usage["generation_calls"] = max(0, generation_calls)
    usage["paid_calls"] = int(usage.get("paid_calls") or 0)
    usage["retry_calls"] = int(usage.get("retry_calls") or 0)
    usage["budget_exceeded"] = bool(usage.get("budget_exceeded"))
    state["approved_image_budget"] = usage["approved_budget"]
    state["image_generation_calls"] = usage["generation_calls"]
    state["image_usage"] = usage
    return usage


def set_approved_image_budget(state: dict[str, Any], approved_budget: int) -> dict[str, Any]:
    usage = _image_usage(state)
    usage["approved_budget"] = max(0, int(approved_budget))
    usage["budget_inferred"] = False
    state["approved_image_budget"] = usage["approved_budget"]
    state["image_usage"] = usage
    return state


def reserve_image_generation_call(state: dict[str, Any], *, retry_call: bool = False) -> dict[str, Any]:
    usage = _image_usage(state)
    approved_budget = int(usage.get("approved_budget") or 0)
    generation_calls = int(usage.get("generation_calls") or 0)
    if generation_calls >= approved_budget:
        if usage.get("budget_inferred"):
            approved_budget = generation_calls + 1
            usage["approved_budget"] = approved_budget
            state["approved_image_budget"] = approved_budget
        else:
            usage["budget_exceeded"] = True
            state["image_usage"] = usage
            state["error_code"] = "IMAGE_BUDGET_EXCEEDED"
            raise ProviderError("IMAGE_BUDGET_EXCEEDED", "image generation budget has already reached the approved limit")
    generation_calls += 1
    usage["generation_calls"] = generation_calls
    usage["paid_calls"] = int(usage.get("paid_calls") or 0) + 1
    if retry_call:
        usage["retry_calls"] = int(usage.get("retry_calls") or 0) + 1
    usage["budget_exceeded"] = False
    state["image_generation_calls"] = generation_calls
    state["image_usage"] = usage
    return state


def _move_to_legacy_unused(root: Path, relative_path: str) -> None:
    if not relative_path:
        return
    source = (root / relative_path).resolve()
    try:
        source.relative_to(root.resolve())
    except ValueError:
        return
    if not source.exists() or not source.is_file():
        return
    legacy_dir = root / "images" / "legacy-unused"
    legacy_dir.mkdir(parents=True, exist_ok=True)
    target = legacy_dir / source.name
    if target.exists():
        stem = target.stem
        suffix = target.suffix
        counter = 1
        while True:
            candidate = legacy_dir / f"{stem}-{counter}{suffix}"
            if not candidate.exists():
                target = candidate
                break
            counter += 1
    source.replace(target)


def _normalize_state_images_for_plan(state: dict[str, Any], image_mode: str, output_root: Path | None = None) -> dict[str, Any]:
    task_root = generation_task_dir(str(state["task_id"]))
    root = _output_root(task_root, output_root)
    normalized_mode = normalize_image_plan(image_mode)
    plan = image_plan_for(1200, normalized_mode)
    keep_cover = bool(plan.get("cover"))
    inline_limit = int(plan.get("inline_count") or 0)
    article = sanitize_json(state.get("article") or {})
    current_inline = [sanitize_sensitive_data(item) for item in state.get("inline_images") or [] if isinstance(item, dict)]
    kept_inline = current_inline[:inline_limit]
    removed_inline = current_inline[inline_limit:]

    if not keep_cover:
        cover_state = state.get("cover") or {}
        cover_path = str(cover_state.get("path") or (article.get("cover") or {}).get("path") or "")
        _move_to_legacy_unused(root, cover_path)
        state["cover"] = None
        article.pop("cover", None)

    for item in removed_inline:
        _move_to_legacy_unused(root, str(item.get("file_path") or item.get("path") or ""))

    kept_ids = {str(item.get("image_id") or "") for item in kept_inline}
    for path in sorted((root / "images").glob("section-*.png")):
        if path.stem not in kept_ids:
            _move_to_legacy_unused(root, str(path.relative_to(root)).replace("\\", "/"))

    state["inline_images"] = kept_inline
    state["article"] = article
    if not keep_cover:
        state["article"]["images"] = []
    sync_inline_image_files(state, root)
    return state


def normalize_task_images_for_plan(
    task_id: str,
    image_mode: str,
    *,
    store: SQLiteStore | None = None,
    output_root: Path | None = None,
) -> dict[str, Any]:
    store = store or get_store()
    with task_lock(task_id):
        state = load_generation_task(task_id)
        if not state:
            raise ProviderError("TASK_NOT_FOUND", "task not found")
        _normalize_state_images_for_plan(state, image_mode, output_root)
        return _save_state(state, store)


def sync_inline_image_files(state: dict[str, Any], output_root: Path | None = None) -> dict[str, Any]:
    """Synchronize task state, article.json and assets.json from one inline asset list."""
    task_root = generation_task_dir(str(state["task_id"]))
    root = _output_root(task_root, output_root)
    assets = [sanitize_sensitive_data(item) for item in state.get("inline_images") or []]
    state["inline_images"] = assets
    state["inline_image_summary"] = _summary(assets)
    article = sanitize_json(state.get("article") or {})
    article["images"] = _cover_items(article, state.get("cover")) + assets
    state["article"] = article
    state.setdefault("paths", {})["inline_assets"] = "images/assets.json"
    _write_json(root / "article.json", article)
    _write_json(root / "images" / "assets.json", {"role": "inline", "assets": assets, "summary": state["inline_image_summary"]})
    return state


def _find_asset(state: dict[str, Any], image_id: str) -> dict[str, Any] | None:
    return next((item for item in state.get("inline_images") or [] if str(item.get("image_id")) == image_id), None)


def _operation_start(state: dict[str, Any], store: SQLiteStore, output_root: Path) -> dict[str, Any]:
    state["status"] = "running"
    state["stage"] = "generating_inline_images"
    state["progress"] = max(75, int(state.get("progress") or 0))
    state["failed_step"] = None
    state["error_code"] = ""
    state["safe_error_message"] = ""
    state["retryable"] = False
    state["inline_operation"] = True
    sync_inline_image_files(state, output_root)
    return _save_state(state, store)


def _operation_finish(state: dict[str, Any], store: SQLiteStore, output_root: Path) -> dict[str, Any]:
    summary = _summary(state.get("inline_images") or [])
    state["inline_image_summary"] = summary
    cancellation_requested = bool(state.get("cancellation_requested"))
    if cancellation_requested:
        for item in state.get("inline_images") or []:
            if item.get("status") in {"pending", "generating"}:
                item.update({"status": "cancelled", "error_code": "TASK_CANCELLED", "error": "task cancellation requested", "retryable": False})
        state["inline_image_summary"] = _summary(state.get("inline_images") or [])
        state.update({"status": "cancelled", "stage": "cancelled", "progress": 100, "failed_step": None, "error_code": "TASK_CANCELLED", "safe_error_message": "task cancellation requested", "retryable": False, "inline_operation": False, "cancelled_at": state.get("cancelled_at") or utc_now()})
    elif state["inline_image_summary"]["status"] == "completed":
        state.update({"status": "completed", "stage": "completed", "progress": 100, "failed_step": None, "error_code": "", "safe_error_message": "", "retryable": False, "inline_operation": False, "completed_at": state.get("completed_at") or utc_now()})
    else:
        failed = next((item for item in state.get("inline_images") or [] if item.get("status") == "failed"), {})
        state.update({"status": "partial_success", "stage": "generating_inline_images", "progress": 85, "failed_step": "generating_inline_images", "error_code": str(failed.get("error_code") or "INLINE_IMAGES_INCOMPLETE"), "safe_error_message": "some inline images failed and can be retried", "retryable": bool(failed.get("retryable", True)), "inline_operation": False})
    sync_inline_image_files(state, output_root)
    finish_image_generation(state)
    return _save_state(state, store)


def run_inline_images(
    task_id: str,
    image_profile: dict[str, Any],
    settings: dict[str, Any] | None = None,
    store: SQLiteStore | None = None,
    target_ids: Iterable[str] | None = None,
    regenerate_all: bool = False,
    persist_article: bool = True,
    provider: Any | None = None,
    replan: bool = False,
    exact_count: int | None = None,
    output_root: Path | None = None,
) -> dict[str, Any]:
    """Generate inline images without holding the task lock during network I/O."""
    del persist_article
    settings = settings or {}
    store = store or get_store()
    task_root = generation_task_dir(task_id)
    with task_lock(task_id):
        state = load_generation_task(task_id)
        if not state:
            raise ProviderError("TASK_NOT_FOUND", "task not found")
        article = state.get("article") or {}
        if not article:
            raise ProviderError("ARTICLE_NOT_AVAILABLE", "article result is missing")
        root = _output_root(task_root, output_root)
        style = str((state.get("generation_options") or {}).get("image_style") or "anime editorial news illustration")
        current_assets = [sanitize_sensitive_data(item) for item in state.get("inline_images") or []]
        has_explicit_plan = "image_plan_mode" in (state.get("generation_options") or {}) or "image_plan_mode" in settings
        requested_mode = str((state.get("generation_options") or {}).get("image_plan_mode") or settings.get("image_plan_mode") or "standard")
        if has_explicit_plan:
            _normalize_state_images_for_plan(state, requested_mode, root)
        current_assets = [sanitize_sensitive_data(item) for item in state.get("inline_images") or []]
        if replan or not current_assets:
            current_assets = plan_inline_image_assets(article, style, exact_count=exact_count)
        elif regenerate_all:
            planned = plan_inline_image_assets(article, style, exact_count=exact_count)
            old_by_id = {str(item.get("image_id")): item for item in current_assets}
            for item in planned:
                old = old_by_id.get(str(item.get("image_id"))) or {}
                item["attempt_count"] = int(old.get("attempt_count") or 0)
                item["file_path"] = old.get("file_path") or old.get("path") or ""
                item["path"] = item["file_path"]
                item["fallback_available"] = bool(item["file_path"] and (task_root / item["file_path"]).exists())
            current_assets = planned
        state["inline_images"] = current_assets
        targets = _target_assets(current_assets, target_ids, regenerate_all)
        if not targets:
            sync_inline_image_files(state, root)
            return _operation_finish(state, store, root)
        state = _operation_start(state, store, root)
    provider = provider or OpenAIImageProvider(image_profile, network_settings=settings.get("network"))
    target_ids_order = [str(item.get("image_id") or "") for item in targets]
    attempt_id = uuid.uuid4().hex[:10]
    for image_id in target_ids_order:
        with task_lock(task_id):
            state = load_generation_task(task_id) or state
            asset = _find_asset(state, image_id)
            if not asset:
                continue
            if state.get("cancellation_requested"):
                asset.update({"status": "cancelled", "error_code": "TASK_CANCELLED", "error": "task cancellation requested", "retryable": False})
                sync_inline_image_files(state, root)
                _save_state(state, store)
                break
            if asset.get("status") == "generating":
                raise ProviderError("INLINE_IMAGE_ALREADY_RUNNING", "inline image is already running")
            formal_path = root / "images" / f"{image_id}.png"
            previous_path = str(asset.get("file_path") or asset.get("path") or "")
            try:
                reserve_image_generation_call(state, retry_call=bool(previous_path))
            except ProviderError:
                asset.update(
                    {
                        "status": "failed",
                        "error_code": "IMAGE_BUDGET_EXCEEDED",
                        "error": "image generation budget has already reached the approved limit",
                        "retryable": False,
                    }
                )
                sync_inline_image_files(state, root)
                _save_state(state, store)
                raise
            asset.update({"status": "generating", "attempt_count": int(asset.get("attempt_count") or 0) + 1, "error_code": "", "error": None, "fallback_available": bool(previous_path and (task_root / previous_path).exists())})
            sync_inline_image_files(state, root)
            _save_state(state, store)
        temp_root = task_root / ".attempts" / f"inline-{attempt_id}-{image_id}-{asset['attempt_count']}"
        raw_path = temp_root / "raw"
        final_tmp = temp_root / f"{image_id}.png"
        try:
            generate_parameters = inspect.signature(provider.generate).parameters
            if "cancel_check" in generate_parameters:
                provider.generate(
                    str(asset.get("prompt") or ""),
                    raw_path,
                    cancel_check=lambda: bool((load_generation_task(task_id) or {}).get("cancellation_requested")),
                )
            else:
                provider.generate(str(asset.get("prompt") or ""), raw_path)
            inspect_image(raw_path)
            with task_lock(task_id):
                state = load_generation_task(task_id) or state
                asset = _find_asset(state, image_id)
                if not asset:
                    continue
                if state.get("cancellation_requested"):
                    asset.update({"status": "cancelled", "error_code": "TASK_CANCELLED", "error": "task cancellation requested", "retryable": False})
                    sync_inline_image_files(state, root)
                    _save_state(state, store)
                    break
                final_tmp.parent.mkdir(parents=True, exist_ok=True)
                raw_path.replace(final_tmp)
                inspect_image(final_tmp)
                formal_path.parent.mkdir(parents=True, exist_ok=True)
                final_tmp.replace(formal_path)
                metadata = inspect_image(formal_path)
                asset.update({"status": "completed", "file_path": f"images/{image_id}.png", "path": f"images/{image_id}.png", "metadata": metadata, "fallback_available": False, "provider_response_type": getattr(provider, "last_response_type", "")})
                sync_inline_image_files(state, root)
                _save_state(state, store)
        except Exception as exc:
            mapped = map_provider_exception(exc)
            code = str(getattr(mapped, "code", "PROVIDER_ERROR"))
            with task_lock(task_id):
                state = load_generation_task(task_id) or state
                asset = _find_asset(state, image_id)
                if asset:
                    asset.update({"status": "failed", "error_code": code, "error": redact_sensitive_text(str(getattr(mapped, "detail", mapped))), "file_path": previous_path, "path": previous_path, "fallback_available": bool(previous_path and (task_root / previous_path).exists()), "retryable": is_retryable_error(code)})
                    sync_inline_image_files(state, root)
                    _save_state(state, store)
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)
    with task_lock(task_id):
        state = load_generation_task(task_id) or state
        return _operation_finish(state, store, root)


def get_inline_images(task_id: str) -> dict[str, Any]:
    state = load_generation_task(task_id)
    if not state:
        raise ProviderError("TASK_NOT_FOUND", "task not found")
    return {"items": sanitize_sensitive_data(state.get("inline_images") or []), "summary": sanitize_sensitive_data(state.get("inline_image_summary") or {})}
