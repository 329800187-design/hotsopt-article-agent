from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.local_api_token import get_or_create_token

API_BASE = "http://127.0.0.1:8506"
OUT = ROOT / "data" / "logs" / "p1_fresh_hotspot_direct_repro_transcript.json"


def _request(method: str, path: str, payload: dict[str, Any] | None = None, timeout: int = 180) -> tuple[int, dict[str, Any]]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        API_BASE + path,
        data=data,
        method=method,
        headers={
            "Content-Type": "application/json",
            "X-Hotspot-Token": get_or_create_token(),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, json.loads(raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw)
        except Exception:
            body = {"raw": raw}
        return exc.code, body


def _scan_article(text: str) -> dict[str, Any]:
    patterns = [
        "钩子开头",
        "30秒速览",
        "单点深挖",
        "单点深化",
        "观点判断",
        "结尾互动",
        "，、",
        "、，",
        "，，",
        "；，",
        "，；",
        "资料来源",
        "AI辅助",
        "AI声明",
        "免责声明",
    ]
    return {
        "body_chars": len(re.findall(r"[\u4e00-\u9fff]", text)),
        "bad_patterns": [item for item in patterns if item in text],
        "headings": re.findall(r"^##\s+(.+)$", text, re.M),
    }


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    transcript: dict[str, Any] = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "api_base": API_BASE,
    }

    status, body = _request("GET", "/api/health", timeout=20)
    transcript["health"] = {"status": status, "body": body}
    if status != 200:
        OUT.write_text(json.dumps(transcript, ensure_ascii=False, indent=2), encoding="utf-8")
        return 1

    status, body = _request("POST", "/api/hotspots/refresh", timeout=120)
    transcript["refresh"] = {"status": status, "body": body}
    topics = ((body.get("data") or {}).get("topics") or []) if isinstance(body, dict) else []
    if status != 200 or not topics:
        OUT.write_text(json.dumps(transcript, ensure_ascii=False, indent=2), encoding="utf-8")
        return 2

    keyword = str(os.environ.get("P1_TOPIC_KEYWORD") or "").strip()
    topic = topics[0]
    if keyword:
        topic = next((item for item in topics if keyword in str(item.get("title") or "")), topic)
        transcript["selection_keyword"] = keyword
    transcript["selected_topic_from_refresh_body"] = topic

    payload = {
        "batch_name": "P1真实热点质量验证_" + datetime.now().strftime("%H%M%S"),
        "mode": "multi_topic",
        "topics": [topic],
        "article_count": 1,
        "client_request_id": "p1-fresh-hotspot-" + datetime.now().strftime("%Y%m%d%H%M%S"),
        "generation_options": {
            "article_type": "热点资讯",
            "style": "客观通俗",
            "image_plan_mode": "none",
            "target_words": 1200,
            "confirm_paid": False,
        },
    }
    started = time.perf_counter()
    status, body = _request("POST", "/api/batches", payload, timeout=60)
    transcript["create_batch"] = {"status": status, "body": body}
    batch_id = str(((body.get("data") or {}).get("batch_id") or "")) if isinstance(body, dict) else ""
    if status not in {200, 201} or not batch_id:
        OUT.write_text(json.dumps(transcript, ensure_ascii=False, indent=2), encoding="utf-8")
        return 3

    polls: list[dict[str, Any]] = []
    batch_body: dict[str, Any] = {}
    for _ in range(80):
        time.sleep(3)
        poll_status, poll_body = _request("GET", f"/api/batches/{batch_id}", timeout=30)
        batch_body = poll_body
        data = poll_body.get("data") or {}
        polls.append(
            {
                "status_code": poll_status,
                "batch_status": data.get("status"),
                "completed_count": data.get("completed_count"),
                "failed_count": data.get("failed_count"),
                "updated_at": data.get("updated_at"),
            }
        )
        if data.get("status") in {"completed", "completed_with_warning", "warning", "failed", "cancelled"}:
            break
    transcript["polls"] = polls
    transcript["elapsed_seconds"] = round(time.perf_counter() - started, 2)

    items_status, items_body = _request("GET", f"/api/batches/{batch_id}/items", timeout=30)
    transcript["items"] = {"status": items_status, "body": items_body}
    items_data = (items_body.get("data") or []) if isinstance(items_body, dict) else []
    if isinstance(items_data, dict):
        items = items_data.get("items") or items_data.get("batch_items") or []
    else:
        items = items_data
    task_id = ""
    if isinstance(items, list) and items:
        first_item = items[0] or {}
        nested_task = first_item.get("task") if isinstance(first_item.get("task"), dict) else {}
        task_id = str(first_item.get("task_id") or nested_task.get("task_id") or "")
    if not task_id:
        final_data = (batch_body.get("data") or {}) if isinstance(batch_body, dict) else {}
        final_items = final_data.get("items") or []
        if isinstance(final_items, list) and final_items:
            first_item = final_items[0] or {}
            nested_task = first_item.get("task") if isinstance(first_item.get("task"), dict) else {}
            task_id = str(first_item.get("task_id") or nested_task.get("task_id") or "")
    transcript["task_id"] = task_id
    transcript["final_batch_body"] = batch_body

    if task_id:
        article_status, article_body = _request("GET", f"/api/tasks/{task_id}/article", timeout=30)
        transcript["article_endpoint"] = {"status": article_status, "body": article_body}
        task_dir = ROOT / "data" / "data" / "tasks" / task_id
        article_json_path = task_dir / "article.json"
        article_md_path = task_dir / "article.md"
        transcript["article_files"] = {
            "article_json": str(article_json_path),
            "article_md": str(article_md_path),
            "article_json_exists": article_json_path.exists(),
            "article_md_exists": article_md_path.exists(),
        }
        if article_md_path.exists():
            markdown = article_md_path.read_text(encoding="utf-8", errors="replace")
            transcript["article_markdown"] = markdown
            transcript["article_scan"] = _scan_article(markdown)
        if article_json_path.exists():
            transcript["article_json"] = json.loads(article_json_path.read_text(encoding="utf-8"))

    transcript["finished_at"] = datetime.now(timezone.utc).isoformat()
    OUT.write_text(json.dumps(transcript, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "out": str(OUT),
        "batch_id": batch_id,
        "task_id": task_id,
        "selected_title": topic.get("title"),
        "elapsed_seconds": transcript["elapsed_seconds"],
        "article_scan": transcript.get("article_scan"),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
