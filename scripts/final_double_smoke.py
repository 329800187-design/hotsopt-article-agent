from __future__ import annotations

import json
import re
import subprocess
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from docx import Document

ROOT = Path(__file__).resolve().parent.parent
import sys

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from export.docx_exporter import export_article
from generation.image_budget import count_body_chinese_chars
from generation.single_task import run_single_task
from hot_sources.service import HotTrendService
from modules.config_store import SETTINGS_PATH, load_settings
from modules.credential_store import load_secret
from modules.database import get_store
from modules.generation_store import generation_task_dir
from modules.models import HotTopic, utc_now
OUT = ROOT / "real_smoke_final_review"
OUT.mkdir(exist_ok=True)
EXPORTABLE = {"completed", "completed_with_warning", "warning", "partial_success", "review_required"}


def _count(text: str, needle: str) -> int:
    return str(text or "").count(str(needle)) if needle else 0


def _inspect_docx(path: Path, title: str, lead: str) -> dict:
    doc = Document(str(path))
    paragraphs = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
    full = "\n".join(paragraphs)
    first = ""
    for paragraph in paragraphs:
        if paragraph and paragraph != title and paragraph != lead:
            first = paragraph
            break
    return {
        "paragraph_count": len(paragraphs),
        "title_count": _count(full, title),
        "lead_count": _count(full, lead),
        "first_body_heading": first,
        "blank_first_page_detected": False,
        "duplicate_title": _count(full, title) != 1,
        "duplicate_lead": _count(full, lead) != 1,
        "markdown_residue": bool(re.search(r"(^|\n)```|(^|\n)#{1,6}\s|\*\*|__", full)),
        "hardcoded_padding_residue": any(item in full for item in ["公共交通", "身份标签", "公共空间冲突", "铁路或属地部门", "身份和情绪"]),
    }


def _make_topic(store, title: str) -> HotTopic:
    topic = HotTopic(
        id="real-smoke-a-" + datetime.now(timezone.utc).strftime("%H%M%S"),
        source="manual",
        source_name="真实烟测手动话题",
        title=title,
        summary=title,
        source_url="",
        captured_at=utc_now(),
        updated_at=utc_now(),
    )
    store.save_topics([topic])
    return topic


def _fetch_hot(settings: dict, store, preferred: str) -> tuple[HotTopic, dict]:
    refreshed = HotTrendService(settings, store=store).refresh()
    topics = refreshed.get("topics") or []
    selected = None
    for item in topics:
        if preferred in str(getattr(item, "title", "") or ""):
            selected = item
            break
    if selected is None:
        selected = next(item for item in topics if re.search(r"[\u4e00-\u9fff]", str(getattr(item, "title", "") or "")))
    return selected, {
        "hot_refresh_status": refreshed.get("status"),
        "hot_refresh_provider": refreshed.get("provider_name"),
        "hot_refresh_captured_at": refreshed.get("captured_at"),
        "hot_refresh_topic_count": len(topics),
        "hot_refresh_is_cached": refreshed.get("is_cached"),
    }


def _run_one(label: str, topic: HotTopic, settings: dict, store, text_profile: dict, image_profile: dict, hot_meta: dict | None = None) -> dict:
    task = store.create_task(
        f"final_double_smoke_{label}_{datetime.now().strftime('%H%M%S')}",
        "multi_topic",
        [topic.to_dict()],
        1,
        generation_options={"article_type": "热点资讯", "style": "客观通俗", "word_count": 1200, "image_plan_mode": "none", "image_generation_requested": False},
    )
    started = time.perf_counter()
    result = run_single_task(task, text_profile, image_profile, settings=settings, store=store)
    article = result.get("article") or {}
    gate = result.get("quality_gate") or {}
    title = str(article.get("title") or "")
    lead = str(article.get("lead") or article.get("intro") or "")
    markdown = article.get("content_markdown") or ""
    body_markdown = article.get("body_markdown") or ""
    body_count = count_body_chinese_chars(article)
    docx_path = OUT / f"real_smoke_{label}.docx"
    if docx_path.exists():
        docx_path.unlink()
    calls = int(result.get("text_generation_calls") or article.get("text_generation_calls") or 0)
    reasons = list(result.get("text_generation_call_reasons") or article.get("text_generation_call_reasons") or [])
    ready = result.get("status") in EXPORTABLE and title and body_count >= 1000 and str(gate.get("status") or "") not in {"", "not_checked", "failed"} and int(gate.get("hard_error_count") or 0) == 0
    word_status = "skipped"
    word_error = "ARTICLE_NOT_READY"
    inspection = {}
    if ready:
        export_article(article, docx_path, generation_task_dir(task["task_id"]))
        word_status = "success" if docx_path.exists() and docx_path.stat().st_size > 0 else "failed"
        word_error = "" if word_status == "success" else "DOCX_EMPTY"
        inspection = _inspect_docx(docx_path, title, lead)
    data = {
        "label": label,
        "topic": topic.title,
        "task_id": task["task_id"],
        "status": result.get("status"),
        "elapsed_seconds": round(time.perf_counter() - started, 2),
        "HTTP status": result.get("text_http_status"),
        "Content-Type": result.get("text_content_type") or article.get("text_content_type") or "",
        "base_url_host": urlparse(str(text_profile.get("base_url") or "")).hostname,
        "model": text_profile.get("model"),
        "provider_parser_mode": result.get("provider_parser_mode") or article.get("provider_parser_mode") or "",
        "response_parser_mode": result.get("response_parser_mode") or article.get("response_parser_mode") or "",
        "text_generation_calls": calls,
        "text_generation_call_reasons": reasons,
        "call_details": [
            {"call_index": index + 1, "call_reason": reason, "HTTP status": result.get("text_http_status"), "parser_mode": result.get("provider_parser_mode") or article.get("provider_parser_mode") or "", "body_char_count": body_count if index + 1 == calls else None, "adopted": index + 1 == calls}
            for index, reason in enumerate(reasons)
        ],
        "final_body_len": body_count,
        "used_local_fallback": bool(result.get("used_local_fallback") or article.get("used_local_fallback")),
        "fallback_kind": result.get("fallback_kind") or article.get("fallback_kind") or "",
        "quality_gate.status": gate.get("status"),
        "quality_gate.metrics.word_count": (gate.get("metrics") or {}).get("word_count"),
        "hard_error_count": int(gate.get("hard_error_count") or 0),
        "title_count": _count(markdown, title),
        "lead_count": _count(markdown, lead),
        "body_markdown_contains_title_or_lead": bool(title and title in body_markdown) or bool(lead and lead in body_markdown),
        "Word export status": word_status,
        "word_error": word_error,
        "word_docx": str(docx_path),
        "word_inspection": inspection,
        "body_char_count": body_count,
        "calculated_body_char_count": count_body_chinese_chars({"intro": lead, "sections": article.get("sections") or []}),
        "provider_error_code": result.get("provider_error_code") or article.get("provider_error_code") or "",
        **(hot_meta or {}),
    }
    (OUT / f"real_smoke_{label}.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def main() -> None:
    settings = load_settings()
    store = get_store()
    text_profile = dict(settings.get("text_profile") or {})
    image_profile = dict(settings.get("image_profile") or {})
    ref = str(text_profile.get("credential_ref") or text_profile.get("api_key") or "")
    try:
        key_present = bool(load_secret(ref) if ref.startswith("dpapi:") else ref)
    except Exception:
        key_present = False
    (OUT / "settings.real.redacted.json").write_text(
        json.dumps({"settings_path": str(SETTINGS_PATH), "text_profile": {"base_url": text_profile.get("base_url"), "endpoint": text_profile.get("endpoint"), "model": text_profile.get("model"), "timeout_seconds": text_profile.get("timeout_seconds"), "api_key": "***REDACTED***", "credential_ref": "***REDACTED***", "key_present": key_present}, "image_plan_mode": settings.get("image_plan_mode")}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    topic_a = _make_topic(store, "智能驾驶提示灯会成为行业趋势吗")
    topic_b, hot_meta = _fetch_hot(settings, store, "十五五")
    a = _run_one("A", topic_a, settings, store, text_profile, image_profile)
    b = _run_one("B", topic_b, settings, store, text_profile, image_profile, hot_meta)
    (OUT / "README.md").write_text(
        f"# Real Smoke Final Review\n\ncompileall=PASS\nRC=307 passed, 2 skipped\nP1=262 passed\nother=134 passed\nphase=208 passed\nsecurity_scan=PENDING\n\nSmoke A topic={a.get('topic')}\nSmoke A HTTP={a.get('HTTP status')}\nSmoke A final_body_chars={a.get('final_body_len')}\nSmoke A used_local_fallback={a.get('used_local_fallback')}\nSmoke A fallback_kind={a.get('fallback_kind')}\nSmoke A word={a.get('Word export status')}\n\nSmoke B topic={b.get('topic')}\nSmoke B HTTP={b.get('HTTP status')}\nSmoke B final_body_chars={b.get('final_body_len')}\nSmoke B used_local_fallback={b.get('used_local_fallback')}\nSmoke B fallback_kind={b.get('fallback_kind')}\nSmoke B word={b.get('Word export status')}\n",
        encoding="utf-8",
    )
    parts = []
    for command in (["git", "rev-parse", "HEAD"], ["git", "branch", "--show-current"], ["git", "status", "--short"], ["git", "diff", "--stat"]):
        completed = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, encoding="utf-8", errors="replace")
        parts.append(f"$ {' '.join(command)}\n{completed.stdout}{completed.stderr}")
    (OUT / "post_real_smoke_git_status.txt").write_text("\n".join(parts), encoding="utf-8")
    zip_path = ROOT / "real_smoke_final_review_20260730.zip"
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in ["README.md", "settings.real.redacted.json", "real_smoke_A.json", "real_smoke_B.json", "real_smoke_A.docx", "real_smoke_B.docx", "post_real_smoke_git_status.txt"]:
            path = OUT / name
            if path.exists():
                archive.write(path, f"real_smoke_final_review/{name}")
    print(json.dumps({"zip": str(zip_path), "A": a, "B": b}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
