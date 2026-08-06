from __future__ import annotations

import hashlib
import json
import gc
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from export.docx_exporter import export_article, export_combined
from export.zip_exporter import export_article_bundle, export_batch_bundle
from generation.editor import restore_article_version, save_article, save_article_draft
from generation.angle_planner import plan_angles
from modules.database import SQLiteStore
from modules.generation_store import generation_task_dir, load_generation_task, save_generation_task
from modules.security import sanitize_sensitive_data
import modules.generation_store as generation_store


EVIDENCE = ROOT / "evidence" / "rc1-3-3-live"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="rc1-3-3-five-article-") as temporary:
        work = Path(temporary)
        db_path = work / "data" / "hotspot_agent.db"
        tasks_root = work / "data" / "tasks"
        exports_root = work / "exports"
        generation_store.TASKS_ROOT = tasks_root
        store = SQLiteStore(db_path)
        topic = {"id": "rc1-3-3-topic", "title": "本地五篇模拟热点", "summary": "公开摘要", "source": "local", "source_name": "本地兼容模拟", "source_url": "https://example.com/local", "captured_at": "2026-07-20T00:00:00+08:00"}
        batch = store.create_batch(
            "RC1.3.3 五篇本地兼容模拟",
            "single_topic_multi_angle",
            [topic],
            {"article_type": "热点资讯", "style": "客观通俗", "image_style": "动漫化新闻插画", "word_count": 800, "article_count": 5},
            2,
            plan_angles(5),
        )
        articles: list[tuple[dict, Path]] = []
        for index, item in enumerate(batch["items"], start=1):
            task_id = str(item["task"]["task_id"])
            task_root = generation_task_dir(task_id)
            image_root = task_root / "images"
            image_root.mkdir(parents=True, exist_ok=True)
            assets = [{"image_id": "cover", "role": "cover", "status": "completed", "path": "images/cover.png"}]
            for image_index in (1, 2):
                image_path = image_root / f"section-{image_index}.png"
                Image.new("RGB", (1024, 1024), (40 * index, 40 * image_index, 120)).save(image_path)
                assets.append({"image_id": f"section-{image_index}", "role": "inline", "status": "completed", "paragraph_ref": f"section-{image_index}", "path": f"images/section-{image_index}.png"})
            Image.new("RGB", (1024, 1024), (120, 40 * index, 40)).save(image_root / "cover.png")
            article = {
                "title": f"本地模拟文章 {index}",
                "summary": f"第 {index} 个角度摘要",
                "intro": f"第 {index} 个角度导语",
                "content_markdown": f"# 本地模拟文章 {index}\n\n这是第 {index} 个角度的独立内容。",
                "sections": [{"heading": f"角度 {index} 观察", "body": f"这是第 {index} 篇文章正文。"}, {"heading": "读者提示", "body": "发布前复核公开事实。"}],
                "images": assets,
                "keywords": [f"角度{index}"],
            }
            state = load_generation_task(task_id) or item["task"]
            state.update({"status": "completed", "stage": "completed", "progress": 100, "article": article, "cover": assets[0], "inline_images": assets[1:], "inline_image_summary": {"status": "completed", "completed": 2, "total": 2}, "similarity_status": "passed", "completed_at": datetime.now(timezone.utc).isoformat()})
            save_generation_task(state, expected_version=int(state.get("state_version") or 0))
            store.update_task_status(task_id, "completed")
            articles.append((article, task_root))
        store.update_batch_quality(batch["batch_id"], "passed")
        final = store.refresh_batch(batch["batch_id"]) or {}
        if final.get("total_count") != 5 or final.get("completed_count") != 5 or not final.get("final_ready"):
            raise RuntimeError("five-article quality gate did not become final_ready")
        edited_task_id = str(batch["items"][0]["task"]["task_id"])
        save_article_draft(edited_task_id, {"title": "本地模拟文章 1（已编辑）"}, store)
        save_article(edited_task_id, store=store)
        versions = load_generation_task(edited_task_id).get("version_commit") or {}
        version_id = versions.get("version_id")
        if version_id:
            restore_article_version(edited_task_id, str(version_id), store)
        export_root = exports_root
        export_root.mkdir(parents=True, exist_ok=True)
        export_article(articles[0][0], export_root / "single.docx", articles[0][1])
        export_combined([article for article, _ in articles], export_root / "five.docx", articles[0][1])
        export_article_bundle(articles[0][0], articles[0][1], export_root / "single.zip")
        export_batch_bundle(articles, export_root / "five.zip", "五篇合集")
        restarted_store = SQLiteStore(db_path)
        restarted = restarted_store.refresh_batch(batch["batch_id"]) or {}
        del restarted_store
        gc.collect()
        if restarted.get("completed_count") != 5 or not restarted.get("final_ready"):
            raise RuntimeError("restart recovery did not preserve five completed articles")
        evidence = sanitize_sensitive_data({
            "status": "RC1_3_3_FIVE_ARTICLE_SIMULATION_PASS",
            "mode": "single_topic_multi_angle",
            "article_count": 5,
            "completed_count": restarted.get("completed_count"),
            "quality_status": restarted.get("quality_status"),
            "final_ready": restarted.get("final_ready"),
            "inline_images_per_article": 2,
            "editing": True,
            "history_restore": bool(version_id),
            "exports": {name: digest(export_root / name) for name in ("single.docx", "five.docx", "single.zip", "five.zip")},
            "restarted_status": restarted.get("status"),
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / "five_article_simulation.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("RC1_3_3_FIVE_ARTICLE_SIMULATION_PASS")
    print(json.dumps({"article_count": 5, "final_ready": True, "restarted_status": "completed"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
