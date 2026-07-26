from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from generation import single_task
from modules.config_store import load_settings
from modules.database import get_store
from modules.generation_store import generation_task_dir, load_generation_task
from modules.security import sanitize_json
from scripts.security_scan import scan_tree


EVIDENCE = ROOT / "evidence" / "phase2a5-live"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""


def file_digests(task_id: str) -> dict[str, str]:
    directory = generation_task_dir(task_id)
    return {
        "article_json": digest(directory / "article.json"),
        "article_md": digest(directory / "article.md"),
        "cover": digest(directory / "images" / "cover.png"),
    }


def article_content_digest(task_id: str) -> str:
    path = generation_task_dir(task_id) / "article.json"
    if not path.exists():
        return ""
    value = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(value, dict):
        value = {key: item for key, item in value.items() if key not in {"cover", "images"}}
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def write_json(name: str, value: object) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / name).write_text(json.dumps(sanitize_json(value), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    settings = load_settings()
    text_profile = dict(settings.get("text_profile") or {})
    image_profile = dict(settings.get("image_profile") or {})
    if not text_profile.get("api_key") or not image_profile.get("api_key"):
        print("RETRY_COVER_PENDING")
        return 0
    store = get_store()
    topics = store.list_topics(limit=1)
    if not topics:
        print("RETRY_COVER_BLOCKED: no topic")
        return 1
    task = store.create_task(
        "2A.5 retry-cover live smoke",
        "multi_topic",
        [topics[0].to_dict()],
        1,
        generation_options={"article_type": "热点资讯", "style": "客观通俗", "image_style": "anime editorial news illustration", "word_count": 800},
    )
    bad_image = copy.deepcopy(image_profile)
    bad_image["model"] = "phase2a5-model-that-does-not-exist"
    calls = {"text": 0}
    original_generate_article = single_task.generate_article

    def counted_generate_article(*args, **kwargs):
        calls["text"] += 1
        return original_generate_article(*args, **kwargs)

    single_task.generate_article = counted_generate_article
    failed = single_task.run_single_task(task, text_profile, bad_image, settings=settings, store=store)
    task_id = task["task_id"]
    after_failure = file_digests(task_id)
    content_before_retry = article_content_digest(task_id)
    text_calls_before_retry = calls["text"]
    if failed.get("status") != "partial_success" or failed.get("failed_step") != "generating_cover":
        print(json.dumps({"status": "RETRY_COVER_FAILED", "failure": failed}, ensure_ascii=False))
        return 1
    retry = single_task.run_single_task(task, text_profile, image_profile, settings=settings, store=store, retry_step="retry-cover")
    text_calls_after_retry = calls["text"]
    after_retry = file_digests(task_id)
    content_after_retry = article_content_digest(task_id)
    task_json = load_generation_task(task_id) or {}
    sqlite_task = store.get_task(task_id) or {}
    evidence = {
        "task_id": task_id,
        "failure_status": failed.get("status"),
        "failure_step": failed.get("failed_step"),
        "failure_error_code": failed.get("error_code"),
        "retry_status": retry.get("status"),
        "text_calls_before_retry": text_calls_before_retry,
        "text_calls_after_retry": text_calls_after_retry,
        "text_call_count_unchanged": text_calls_before_retry == 1 and text_calls_after_retry == 1,
        "article_content_sha_before": content_before_retry,
        "article_content_sha_after": content_after_retry,
        "article_content_unchanged": bool(content_before_retry and content_before_retry == content_after_retry),
        "article_files_retained_after_failure": bool(after_failure["article_json"] and after_failure["article_md"]),
        "hashes_after_failure": after_failure,
        "hashes_after_retry": after_retry,
        "article_markdown_unchanged": after_failure["article_md"] == after_retry["article_md"],
        "sqlite_status": sqlite_task.get("status"),
        "task_json_status": task_json.get("status"),
        "consistent_persistence": sqlite_task.get("status") == task_json.get("status") == "completed",
        "final_model_info": retry.get("model_info"),
        "attempt_history": retry.get("attempt_history", []),
        "status": "RETRY_COVER_PASS" if retry.get("status") == "completed" and text_calls_before_retry == 1 and text_calls_after_retry == 1 and content_before_retry == content_after_retry and after_retry["cover"] and sqlite_task.get("status") == "completed" else "RETRY_COVER_FAILED",
    }
    write_json("retry_cover_evidence.json", evidence)
    article = task_json.get("article") if isinstance(task_json.get("article"), dict) else {}
    write_json("task_redacted.json", {
        "task_id": task_json.get("task_id"),
        "status": task_json.get("status"),
        "stage": task_json.get("stage"),
        "article": {"title": article.get("title"), "summary": article.get("summary"), "status": article.get("status")},
        "article_content_sha256": content_after_retry,
        "model_info": task_json.get("model_info"),
        "attempt_history": task_json.get("attempt_history", []),
        "files": after_retry,
    })
    write_json("article_hashes.json", {"after_failure": after_failure, "after_retry": after_retry})
    write_json("cover_metadata.json", retry.get("cover") or {})
    write_json("security_scan.json", scan_tree(ROOT, [str(text_profile.get("api_key") or ""), str(image_profile.get("api_key") or "")]))
    print(json.dumps(evidence, ensure_ascii=False, indent=2))
    return 0 if evidence["status"] == "RETRY_COVER_PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
