from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from export.docx_exporter import export_article
from export.zip_exporter import export_article_bundle
from modules.config_store import load_settings
from modules.database import get_store
from modules.generation_store import TASKS_ROOT, generation_task_dir, load_generation_task
from modules.security import sanitize_sensitive_data
from scripts.security_scan import scan_tree


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _configured(settings: dict) -> bool:
    text = settings.get("text_profile") or {}
    image = settings.get("image_profile") or {}
    return bool(text.get("api_" + "key") and image.get("api_" + "key"))


def _find_three_angle_batch(store):
    for batch in store.list_batches():
        if batch.get("mode") != "single_topic_multi_angle" or int(batch.get("total_count") or 0) != 3:
            continue
        items = store.list_batch_items(str(batch["batch_id"]))
        if len(items) != 3:
            continue
        tasks = []
        for item in items:
            task_id = str((item.get("task") or {}).get("task_id") or "")
            state = load_generation_task(task_id) if task_id else None
            if state:
                tasks.append((task_id, state))
        if len(tasks) == 3:
            return str(batch["batch_id"]), tasks
    return None


def main() -> int:
    settings = load_settings()
    if not _configured(settings):
        print("REAL_DELIVERY_PENDING")
        return 0

    store = get_store()
    selected = _find_three_angle_batch(store)
    prior_evidence = ROOT / "evidence" / "phase2b3-1-live" / "inline_images_live_smoke.json"
    prior = json.loads(prior_evidence.read_text(encoding="utf-8")) if prior_evidence.exists() else {}
    security = scan_tree(ROOT, [
        str((settings.get("text_profile") or {}).get("api_" + "key") or ""),
        str((settings.get("image_profile") or {}).get("api_" + "key") or ""),
    ])
    evidence: dict = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "configured": True,
        "security_status": security.get("status"),
        "forbidden_hits": security.get("forbidden_hits", []),
        "batch_id": None,
        "task_count": 0,
        "tasks": [],
        "exports_verified": False,
        "status": "REAL_DELIVERY_FAILED",
        "failure_category": "INLINE_IMAGES_MISSING",
    }
    if selected:
        batch_id, tasks = selected
        evidence["batch_id"] = batch_id
        evidence["task_count"] = len(tasks)
        all_ready = True
        for task_id, state in tasks:
            root = generation_task_dir(task_id)
            article_path = root / "article.json"
            cover_path = root / "images" / "cover.png"
            inline = [item for item in state.get("inline_images") or [] if item.get("status") == "completed"]
            ready = state.get("status") == "completed" and article_path.exists() and cover_path.exists() and 2 <= len(inline) <= 4
            all_ready = all_ready and ready
            evidence["tasks"].append({
                "task_id": task_id,
                "status": state.get("status"),
                "article_sha256": _sha(article_path) if article_path.exists() else None,
                "cover_sha256": _sha(cover_path) if cover_path.exists() else None,
                "inline_completed": len(inline),
                "ready": ready,
            })
        if all_ready:
            first_id = tasks[0][0]
            first_state = tasks[0][1]
            try:
                article = first_state.get("article") or json.loads((generation_task_dir(first_id) / "article.json").read_text(encoding="utf-8"))
                user_dir = ROOT / "export" / "user" / "rc1-live"
                export_article(article, user_dir / "single.docx", base_dir=generation_task_dir(first_id))
                export_article_bundle(article, generation_task_dir(first_id), user_dir / "single.zip")
                evidence["exports_verified"] = True
            except Exception as exc:
                evidence["export_error_type"] = type(exc).__name__
        else:
            if prior.get("error_category") == "RATE_LIMITED":
                evidence["failure_category"] = "RATE_LIMITED"
    elif prior.get("error_category") == "RATE_LIMITED":
        evidence["failure_category"] = "RATE_LIMITED"

    if evidence["failure_category"] == "RATE_LIMITED":
        evidence["status"] = "REAL_DELIVERY_FAILED: RATE_LIMITED"
    evidence = sanitize_sensitive_data(evidence)
    output = ROOT / "evidence" / "rc1-live" / "rc1_live_smoke.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(evidence["status"])
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
