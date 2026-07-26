from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from generation.inline_images import run_inline_images
from generation.single_task import run_single_task
from modules.config_store import load_settings
from modules.database import get_store
from modules.generation_store import TASKS_ROOT, generation_task_dir, load_generation_task, save_generation_task
from modules.security import sanitize_sensitive_data
from providers.image_provider import inspect_image
from scripts.security_scan import scan_tree


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _image_hashes(task_id: str, state: dict) -> dict[str, str]:
    root = generation_task_dir(task_id)
    return {
        str(item.get("image_id")): _sha(root / str(item.get("path") or item.get("file_path")))
        for item in state.get("inline_images") or []
        if item.get("status") == "completed" and (root / str(item.get("path") or item.get("file_path"))).exists()
    }


def _find_task(store) -> tuple[str, dict] | None:
    candidates: list[tuple[str, dict]] = []
    task_ids = {str(item.get("task_id")) for item in store.list_tasks()}
    for path in sorted(TASKS_ROOT.glob("*/task.json"), key=lambda item: item.stat().st_mtime, reverse=True):
        if path.parent.name not in task_ids:
            continue
        state = load_generation_task(path.parent.name)
        if state and state.get("article") and state.get("status") in {"completed", "partial_success"}:
            candidates.append((path.parent.name, state))
    return candidates[0] if candidates else None


def _prepare_rewrite(task_id: str, store, state: dict) -> dict:
    previous = {
        "article": state.get("article"),
        "cover": state.get("cover"),
        "inline_images": state.get("inline_images") or [],
        "inline_image_summary": state.get("inline_image_summary") or {},
        "article_sha": _sha(generation_task_dir(task_id) / "article.json"),
        "prompt_sha": _sha(generation_task_dir(task_id) / "prompts" / "article_prompt.txt") if (generation_task_dir(task_id) / "prompts" / "article_prompt.txt").exists() else None,
        "cover_prompt_sha": _sha(generation_task_dir(task_id) / "prompts" / "cover_prompt.txt") if (generation_task_dir(task_id) / "prompts" / "cover_prompt.txt").exists() else None,
    }
    version = int(state.get("state_version") or 0)
    state.update({"status": "queued", "stage": "queued", "progress": 0, "completed_at": None, "failed_step": None, "error_code": "", "safe_error_message": "", "rewrite_requested": True, "previous_result": previous, "state_version": version + 1})
    save_generation_task(state, expected_version=version)
    store.update_task_status(task_id, "queued")
    return previous


def main() -> int:
    task_id = ""
    settings = load_settings()
    image_profile = dict(settings.get("image_profile") or {})
    text_profile = dict(settings.get("text_profile") or {})
    if not image_profile.get("api_key") or not image_profile.get("enabled", True) or not text_profile.get("api_key"):
        print("REAL_INLINE_IMAGES_PENDING")
        return 0
    store = get_store()
    selected = _find_task(store)
    if not selected:
        print("REAL_INLINE_IMAGES_PENDING")
        return 0
    task_id, before_state = selected
    task = store.get_task(task_id)
    if not task:
        print("REAL_INLINE_IMAGES_PENDING")
        return 0
    root = generation_task_dir(task_id)
    try:
        before_article_sha = _sha(root / "article.json")
        before_article_md_sha = _sha(root / "article.md") if (root / "article.md").exists() else None
        before_cover_sha = _sha(root / "images" / "cover.png")
        previous = _prepare_rewrite(task_id, store, before_state)
        rewritten = run_single_task(task, text_profile, image_profile, settings=settings, store=store, retry_step="retry-article")
        if rewritten.get("status") != "completed":
            raise RuntimeError("full rewrite did not complete")
        rewritten_items = rewritten.get("inline_images") or []
        if not 2 <= len(rewritten_items) <= 4 or any(item.get("status") != "completed" for item in rewritten_items):
            raise RuntimeError("full rewrite inline images are incomplete")
        after_rewrite_hashes = _image_hashes(task_id, rewritten)
        for item in rewritten_items:
            inspect_image(root / str(item.get("path") or item.get("file_path")))
        rewritten_article_sha = _sha(root / "article.json")
        rewritten_article_md_sha = _sha(root / "article.md")
        rewritten_cover_sha = _sha(root / "images" / "cover.png")
        target_id = str(rewritten_items[0]["image_id"])
        other_before = {key: value for key, value in after_rewrite_hashes.items() if key != target_id}
        article_content_before = str((rewritten.get("article") or {}).get("content_markdown") or "")
        retry_state = run_inline_images(task_id, image_profile, settings=settings, store=store, target_ids=[target_id])
        if retry_state.get("status") != "completed":
            raise RuntimeError("single inline image retry did not complete")
        after_retry_hashes = _image_hashes(task_id, retry_state)
        if any(after_retry_hashes.get(key) != value for key, value in other_before.items()):
            raise RuntimeError("single image retry changed another image")
        if str((retry_state.get("article") or {}).get("content_markdown") or "") != article_content_before:
            raise RuntimeError("single image retry changed article content")
        security = scan_tree(ROOT, [str(image_profile.get("api_key") or ""), str(text_profile.get("api_key") or "")])
        if security.get("forbidden_hits"):
            raise RuntimeError("security scan found forbidden hits")
        evidence = sanitize_sensitive_data({
            "status": "REAL_INLINE_IMAGES_PASS",
            "task_id": task_id,
            "model": image_profile.get("model"),
            "rewrite": {"status": rewritten.get("status"), "article_sha_before": before_article_sha, "article_sha_after": rewritten_article_sha, "article_md_sha_before": before_article_md_sha, "article_md_sha_after": rewritten_article_md_sha, "cover_sha_before": before_cover_sha, "cover_sha_after": rewritten_cover_sha, "previous_inline_count": len(previous.get("inline_images") or []), "new_inline_count": len(rewritten_items), "new_inline_hashes": after_rewrite_hashes},
            "single_image_retry": {"target_id": target_id, "status": retry_state.get("status"), "other_images_unchanged": True, "article_content_unchanged": True, "hashes_after": after_retry_hashes},
            "attempt_history_count": len(rewritten.get("attempt_history") or []),
            "security": {"status": security.get("status"), "forbidden_hits": security.get("forbidden_hits", [])},
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        evidence_path = ROOT / "evidence" / "phase2b3-1-live" / "inline_images_live_smoke.json"
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        evidence_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        print("REAL_INLINE_IMAGES_PASS")
        return 0
    except Exception as exc:
        evidence_path = ROOT / "evidence" / "phase2b3-1-live" / "inline_images_live_smoke.json"
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        failed_state = load_generation_task(task_id) if task_id else {}
        failed_code = str((failed_state or {}).get("error_code") or "")
        category = "RATE_LIMITED" if failed_code == "RATE_LIMITED" or "rate" in str(exc).lower() else "PROVIDER_ERROR"
        evidence_path.write_text(json.dumps(sanitize_sensitive_data({"status": "REAL_INLINE_IMAGES_FAILED", "task_id": task_id, "error_type": type(exc).__name__, "error_category": category, "created_at": datetime.now(timezone.utc).isoformat()}), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"REAL_INLINE_IMAGES_FAILED: {type(exc).__name__}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
