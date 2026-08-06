from __future__ import annotations

import hashlib
import json
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import sys
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from generation.batch_executor import BatchExecutor, get_batch_executor
from hot_sources.service import HotTrendService
from modules.config_store import load_settings
from modules.database import SQLiteStore, get_store
from modules.generation_store import generation_task_dir, load_generation_task, save_generation_task
from modules.models import HotTopic
from modules.security import sanitize_sensitive_data
from scripts.security_scan import scan_tree


EVIDENCE = ROOT / "evidence" / "phase2b1-live"


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def fake_failure_isolation() -> dict:
    import generation.executor as executor_module
    import modules.generation_store as generation_store_module

    original = executor_module.run_single_task
    with tempfile.TemporaryDirectory(prefix="phase2b1-fake-", ignore_cleanup_errors=True) as directory:
        root = Path(directory)
        old_root = generation_store_module.TASKS_ROOT
        generation_store_module.TASKS_ROOT = root / "tasks"
        try:
            def fake_run(task, text_profile, image_profile, settings=None, store=None, retry_step=None):
                state = load_generation_task(task["task_id"])
                title = str((task.get("selected_topics") or [{}])[0].get("title") or "")
                if title.endswith("0") and retry_step is None:
                    state.update({"status": "failed", "stage": "generating_article", "failed_step": "generating_article", "error_code": "AUTH_FAILED"})
                else:
                    state.update({"status": "completed", "stage": "completed", "progress": 100})
                state["state_version"] = int(state.get("state_version") or 0) + 1
                save_generation_task(state, expected_version=state["state_version"] - 1)
                store.update_task_status(task["task_id"], state["status"])
                return state

            executor_module.run_single_task = fake_run
            store = SQLiteStore(root / "fake.sqlite")
            values = [HotTopic(id=f"fake-{index}", title=f"fake-{index}") for index in range(2)]
            store.save_topics(values)
            batch = store.create_batch("fake isolation", "multi_topic", [value.to_dict() for value in values], {}, 2)
            executor = BatchExecutor(store)
            executor.start_batch(batch["batch_id"])
            initial = None
            for _ in range(150):
                initial = store.refresh_batch(batch["batch_id"])
                if initial["status"] in {"partial_success", "completed", "failed"}:
                    break
                time.sleep(0.02)
            retry_result = executor.retry_failed(batch["batch_id"])
            final = None
            for _ in range(150):
                final = store.refresh_batch(batch["batch_id"])
                if final["status"] == "completed":
                    break
                time.sleep(0.02)
            executor.single_executor.pool.shutdown(wait=True)
            return {"initial_status": initial["status"], "initial_completed": initial["completed_count"], "initial_failed": initial["failed_count"], "retry_submitted": len(retry_result["submitted"]), "final_status": final["status"], "final_completed": final["completed_count"]}
        finally:
            generation_store_module.TASKS_ROOT = old_root
            executor_module.run_single_task = original


def main() -> int:
    settings = load_settings()
    text_key = str((settings.get("text_profile") or {}).get("api_key") or "")
    image_key = str((settings.get("image_profile") or {}).get("api_key") or "")
    if not text_key or not image_key:
        print("REAL_BATCH_PENDING")
        return 0

    store = get_store()
    service = HotTrendService(settings, store=store)
    topics = store.list_topics(limit=2)
    if len(topics) < 2:
        result = service.refresh()
        topics = result.get("topics") or []
    if len(topics) < 2:
        print("REAL_BATCH_BLOCKED: fewer than two real topics available")
        return 2

    existing = next((value for value in store.list_batches() if value.get("mode") == "multi_topic" and value.get("total_count") == 2 and value.get("status") == "completed"), None)
    batch = existing or store.create_batch(
        f"2B.1真实批次_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
        "multi_topic",
        [topic.to_dict() for topic in topics[:2]],
        {"article_type": "热点资讯", "style": "客观通俗", "image_style": "动漫化新闻插画", "word_count": 800},
        2,
    )
    executor = get_batch_executor()
    executor.start_batch(batch["batch_id"])
    final = None
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        final = store.refresh_batch(batch["batch_id"])
        if final["status"] in {"completed", "failed", "partial_success", "cancelled"} and final["running_count"] == 0 and final["queued_count"] == 0:
            break
        time.sleep(2)
    if not final or final["status"] != "completed":
        print(f"REAL_BATCH_FAILED: {final.get('status') if final else 'timeout'}")
        return 3

    task_evidence = []
    for item in final["items"]:
        task_id = item["task"]["task_id"]
        directory = generation_task_dir(task_id)
        article_json = directory / "article.json"
        article_md = directory / "article.md"
        cover = directory / "images" / "cover.png"
        state = load_generation_task(task_id) or {}
        if not article_json.exists() or not article_md.exists() or not cover.exists() or state.get("status") != "completed":
            print(f"REAL_BATCH_FAILED: incomplete task {task_id}")
            return 4
        task_evidence.append({"task_id": task_id, "topic_id": item["topic_id"], "status": state.get("status"), "article_json_sha256": digest(article_json), "article_md_sha256": digest(article_md), "cover_sha256": digest(cover), "article_bytes": article_json.stat().st_size, "cover_bytes": cover.stat().st_size})

    fake = fake_failure_isolation()
    if fake["initial_status"] != "partial_success" or fake["final_status"] != "completed" or fake["initial_completed"] != 1:
        print("REAL_BATCH_FAILED: fake failure isolation")
        return 5
    scan = scan_tree(ROOT, [text_key, image_key])
    if scan.get("status") != "SECURITY_SCAN_PASS":
        print("REAL_BATCH_FAILED: security scan")
        return 6
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    evidence = sanitize_sensitive_data({"status": "REAL_BATCH_PASS", "batch_id": final["batch_id"], "batch_status": final["status"], "total_count": final["total_count"], "completed_count": final["completed_count"], "tasks": task_evidence, "fake_failure_isolation": fake, "security_status": scan["status"], "created_at": datetime.now(timezone.utc).isoformat()})
    (EVIDENCE / "batch_live_smoke.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("REAL_BATCH_PASS")
    print(json.dumps({"batch_id": final["batch_id"], "completed_count": final["completed_count"], "security_status": scan["status"], "fake_failure_isolation": fake}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
