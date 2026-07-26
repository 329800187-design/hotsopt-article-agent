from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from export.cover_builder import add_cover_title
from generation.image_prompt_generator import build_cover_prompt
from generation.inline_images import (
    normalize_task_images_for_plan,
    reserve_image_generation_call,
    run_inline_images,
    set_approved_image_budget,
)
from modules.generation_store import generation_task_dir, load_generation_task, save_generation_task
from modules.models import utc_now
from modules.security import sanitize_json
from modules.task_locks import task_lock
from providers.image_provider import OpenAIImageProvider, inspect_image
from providers.text_provider import ProviderError


def _persist(state: dict[str, Any], store: Any) -> dict[str, Any]:
    current = load_generation_task(str(state["task_id"])) or {}
    state["state_version"] = int(current.get("state_version") or state.get("state_version") or 0) + 1
    state["updated_at"] = utc_now()
    save_generation_task(state, expected_version=int(current.get("state_version") or 0) if current else None, allow_terminal_recovery=True)
    store.update_task_status(str(state["task_id"]), str(state.get("status") or "running"))
    return state


def _requested_plan(include_cover: bool, inline_count: int) -> str:
    if inline_count >= 1:
        return "standard"
    if include_cover:
        return "economy"
    return "none"


def _cover_ready(state: dict[str, Any], task_root: Path) -> bool:
    cover = state.get("cover") or {}
    cover_path = str(cover.get("path") or "")
    return bool(cover.get("status") == "completed" and cover_path and (task_root / cover_path).is_file())


def _inline_ready_count(state: dict[str, Any], task_root: Path) -> int:
    ready = 0
    for item in state.get("inline_images") or []:
        if not isinstance(item, dict):
            continue
        path = str(item.get("file_path") or item.get("path") or "")
        if item.get("status") == "completed" and path and (task_root / path).is_file():
            ready += 1
    return ready


def generate_selected_images(task_id: str, image_profile: dict[str, Any], settings: dict[str, Any], store: Any, *, include_cover: bool = True, inline_count: int = 0) -> dict[str, Any]:
    inline_count = max(0, min(1, int(inline_count)))
    image_mode = _requested_plan(include_cover, inline_count)
    task_root = generation_task_dir(task_id)
    with task_lock(task_id):
        state = load_generation_task(task_id)
        if not state or not state.get("article"):
            raise ProviderError("ARTICLE_NOT_AVAILABLE", "article result is missing")
        if state.get("status") not in {"completed", "partial_success"}:
            raise ProviderError("TASK_NOT_READY", "article is not ready for image generation")
        options = dict(state.get("generation_options") or {})
        options.update({"image_plan_mode": image_mode, "image_call_budget_per_article": (1 if include_cover else 0) + inline_count, "image_retry_limit": 0})
        state["generation_options"] = options
        set_approved_image_budget(state, (1 if include_cover else 0) + inline_count)
        state["selected_image_operation"] = {
            "kind": "generate_selected_images",
            "image_mode": image_mode,
            "include_cover": bool(include_cover),
            "inline_count": inline_count,
        }
        state.update({"status": "running", "stage": "generating_selected_images", "progress": 60, "error_code": "", "safe_error_message": ""})
        _persist(state, store)
        state = normalize_task_images_for_plan(task_id, image_mode, store=store, output_root=task_root)
        if (not include_cover or _cover_ready(state, task_root)) and _inline_ready_count(state, task_root) >= inline_count:
            state.update({"status": "completed", "stage": "completed", "progress": 100, "failed_step": None, "error_code": "", "safe_error_message": "", "retryable": False, "inline_operation": False, "completed_at": state.get("completed_at") or utc_now()})
            return _persist(state, store)
    provider = OpenAIImageProvider(image_profile, network_settings=settings.get("network"))
    try:
        if include_cover:
            article = dict(state.get("article") or {})
            if not _cover_ready(state, task_root):
                prompt = build_cover_prompt(article, str(options.get("image_style") or "anime editorial news illustration"))
                work_root = task_root / ".attempts" / f"manual-images-{state.get('state_version', 0)}"
                raw_path = work_root / "cover_raw"
                cover_path = task_root / "images" / "cover.png"
                with task_lock(task_id):
                    state = load_generation_task(task_id) or state
                    reserve_image_generation_call(state)
                    state["stage"] = "generating_cover"
                    _persist(state, store)
                provider.generate(prompt, raw_path)
                inspect_image(raw_path)
                add_cover_title(raw_path, str(article.get("title") or ""), cover_path)
                metadata = inspect_image(cover_path)
                raw_path.unlink(missing_ok=True)
                shutil.rmtree(work_root, ignore_errors=True)
                with task_lock(task_id):
                    state = load_generation_task(task_id) or state
                    cover = {"status": "completed", "path": "images/cover.png", "prompt": prompt, "metadata": metadata, "provider_response_type": provider.last_response_type}
                    state["cover"] = cover
                    article = dict(state.get("article") or {})
                    article["cover"] = cover
                    article["images"] = [{"role": "cover", "path": "images/cover.png", "status": "completed", "metadata": metadata}] + [item for item in article.get("images") or [] if isinstance(item, dict) and item.get("role") == "inline"]
                    state["article"] = sanitize_json(article)
                    _persist(state, store)
        if inline_count:
            with task_lock(task_id):
                state = load_generation_task(task_id) or state
            state = run_inline_images(
                task_id,
                image_profile,
                settings={**settings, "image_plan_mode": image_mode},
                store=store,
                provider=provider,
                replan=True,
                exact_count=inline_count,
                output_root=task_root,
            )
        else:
            with task_lock(task_id):
                state = load_generation_task(task_id) or state
                state.update({"status": "completed", "stage": "completed", "progress": 100, "completed_at": utc_now(), "inline_operation": False})
                _persist(state, store)
        return state
    except Exception as exc:
        with task_lock(task_id):
            state = load_generation_task(task_id) or state
            state.update({"status": "partial_success", "stage": "generating_selected_images", "failed_step": "generating_selected_images", "error_code": getattr(exc, "code", "IMAGE_GENERATION_FAILED"), "safe_error_message": "图片生成失败，文章正文仍然保留", "retryable": False, "inline_operation": False})
            _persist(state, store)
        raise
