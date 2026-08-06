from __future__ import annotations

import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from generation.angle_planner import plan_angles
from generation.batch_executor import get_batch_executor
from generation.similarity import compare_batch_report
from hot_sources.service import HotTrendService
from modules.config_store import load_settings
from modules.database import get_store
from modules.generation_store import generation_task_dir, load_generation_task
from modules.security import sanitize_sensitive_data
from scripts.security_scan import scan_tree

EVIDENCE = ROOT / "evidence" / "phase2b2-live"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    settings = load_settings()
    text_key = str((settings.get("text_profile") or {}).get("api_key") or "")
    image_key = str((settings.get("image_profile") or {}).get("api_key") or "")
    if not text_key or not image_key:
        print("REAL_MULTI_ANGLE_PENDING")
        return 0

    store = get_store()
    service = HotTrendService(settings, store=store)
    topics = store.list_topics(limit=1)
    if not topics:
        topics = (service.refresh().get("topics") or [])[:1]
    if not topics:
        print("REAL_MULTI_ANGLE_BLOCKED: no real topic available")
        return 2

    angles = plan_angles(3, ["news", "social_observation", "commentary"])
    existing = next((item for item in store.list_batches() if item.get("mode") == "single_topic_multi_angle" and item.get("total_count") == 3 and item.get("status") == "completed"), None)
    batch = existing or store.create_batch(
        f"2B.2真实五角度_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
        "single_topic_multi_angle",
        [topics[0].to_dict()],
        {"article_type": "热点资讯", "style": "客观通俗", "image_style": "动漫化新闻插画", "word_count": 800, "article_count": 3},
        2,
        angles,
    )
    executor = get_batch_executor()
    executor.start_batch(batch["batch_id"])
    final = None
    deadline = time.monotonic() + 1200
    while time.monotonic() < deadline:
        final = store.refresh_batch(batch["batch_id"])
        if final and final.get("status") in {"completed", "failed", "partial_success", "cancelled"} and not final.get("running_count") and not final.get("queued_count"):
            break
        time.sleep(2)
    if not final or final.get("status") != "completed":
        print(f"REAL_MULTI_ANGLE_FAILED: {final.get('status') if final else 'timeout'}")
        return 3

    task_evidence = []
    articles = []
    for item in final.get("items", []):
        task = item["task"]
        task_id = str(task["task_id"])
        directory = generation_task_dir(task_id)
        article_json = directory / "article.json"
        article_md = directory / "article.md"
        cover = directory / "images" / "cover.png"
        state = load_generation_task(task_id) or {}
        if state.get("status") != "completed" or not article_json.exists() or not article_md.exists() or not cover.exists():
            print(f"REAL_MULTI_ANGLE_FAILED: incomplete task {task_id}")
            return 4
        articles.append(state.get("article") or {})
        quality = state.get("quality_evidence") or {}
        task_evidence.append({
            "task_id": task_id,
            "angle_id": item.get("angle_id"),
            "angle_name": item.get("angle_name"),
            "status": state.get("status"),
            "article_json_sha256": digest(article_json),
            "article_md_sha256": digest(article_md),
            "cover_sha256": digest(cover),
            "article_bytes": article_json.stat().st_size,
            "cover_bytes": cover.stat().st_size,
            "cover_metadata_sha256": digest(directory / "images" / "cover.json") if (directory / "images" / "cover.json").exists() else None,
            "similarity_status": state.get("similarity_status"),
            "rewrite_count": state.get("rewrite_count", 0),
            "article_sha_before": quality.get("article_sha_before"),
            "article_sha_after": quality.get("article_sha_after") or digest(article_json),
            "prompt_sha_before": quality.get("prompt_sha_before"),
            "prompt_sha_after": quality.get("prompt_sha_after") or digest(directory / "prompts" / "article_prompt.txt"),
            "cover_prompt_sha": quality.get("cover_prompt_sha") or digest(directory / "prompts" / "cover_prompt.txt"),
        })
    if len({item["task_id"] for item in task_evidence}) != 3 or len({item["cover_sha256"] for item in task_evidence}) != 3:
        print("REAL_MULTI_ANGLE_FAILED: outputs are not independent")
        return 5
    scan = scan_tree(ROOT, [text_key, image_key])
    if scan.get("status") != "SECURITY_SCAN_PASS":
        print("REAL_MULTI_ANGLE_FAILED: security scan")
        return 6
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    evidence = sanitize_sensitive_data({
        "status": "REAL_MULTI_ANGLE_PASS",
        "batch_id": final["batch_id"],
        "batch_status": final["status"],
        "total_count": final["total_count"],
        "completed_count": final["completed_count"],
        "angles": [item["angle_id"] for item in task_evidence],
        "tasks": task_evidence,
        "similarity_evidence": {
            **(load_generation_task(task_evidence[0]["task_id"]) or {}).get("similarity_evidence", {}),
            "recomputed": compare_batch_report(articles),
        },
        "security_status": scan["status"],
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    (EVIDENCE / "multi_angle_live_smoke.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("REAL_MULTI_ANGLE_PASS")
    print(json.dumps({"batch_id": final["batch_id"], "completed_count": final["completed_count"], "security_status": scan["status"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
