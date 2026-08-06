from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any, Callable

from export.cover_builder import add_cover_title
from export.docx_exporter import export_article, export_combined
from export.zip_exporter import export_zip
from generation.article_generator import generate_article, plan_for_topic
from generation.image_prompt_generator import build_image_assets
from generation.similarity import duplicate_pairs
from modules.models import HotTopic
from modules.network import resolve_network_settings
from modules.task_store import save_task
from providers.demo_provider import DemoImageProvider
from providers.image_provider import OpenAIImageProvider, ProviderError


ROOT = Path(__file__).resolve().parents[1]


def safe_name(value: str, fallback: str = "article") -> str:
    cleaned = re.sub(r'[<>:"/\\|*\x00-\x1f]', "_", value).strip(" .")
    return (cleaned or fallback)[:80]


def _topic_dict(topic: HotTopic | dict[str, Any]) -> HotTopic:
    return topic if isinstance(topic, HotTopic) else HotTopic.from_dict(topic)


def _error_code(error: Exception) -> str:
    return str(getattr(error, "code", "PROVIDER_ERROR"))


def _is_demo_content(article: dict[str, Any]) -> bool:
    title = str(article.get("title") or "")
    return article.get("demo_mode") is True or "【演示模式】" in title or "演示模式" in title


def run_batch(
    topics: list[HotTopic | dict[str, Any]],
    text_profile: dict[str, Any],
    image_profile: dict[str, Any],
    article_count: int,
    article_type: str,
    style: str,
    image_style: str,
    word_count: int,
    progress: Callable[[str], None] | None = None,
    app_mode: str = "production",
    demo_mode: bool = False,
    network_settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    demo_enabled = bool(demo_mode and app_mode == "demo")
    task_id = uuid.uuid4().hex[:12]
    task_dir = ROOT / "outputs" / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    source_topics = [_topic_dict(topic) for topic in topics]
    if len(source_topics) == 1:
        planned = plan_for_topic(article_count)
        jobs = [(source_topics[0], angle) for angle in planned]
    else:
        planned = plan_for_topic(1)
        jobs = [(topic, planned[0]) for topic in source_topics[:article_count]]
    task: dict[str, Any] = {"id": task_id, "status": "running", "is_demo": demo_enabled, "topics": [item.to_dict() for item in source_topics], "articles": [], "output_dir": str(task_dir), "errors": []}
    save_task(task)
    image_provider = DemoImageProvider() if demo_enabled else OpenAIImageProvider(image_profile, network_settings=network_settings)
    generated: list[dict[str, Any]] = []
    for index, (topic, angle) in enumerate(jobs, start=1):
        article_id = f"{index:02d}_{safe_name(topic.title, 'topic')}"
        article_dir = task_dir / article_id
        article_dir.mkdir(parents=True, exist_ok=True)
        article_task: dict[str, Any] = {"id": article_id, "topic": topic.title, "angle": angle["name"], "status": "running", "images": [], "source_urls": [topic.url] if topic.url else []}
        try:
            if progress:
                progress(f"正在生成第 {index}/{len(jobs)} 篇文章：{topic.title}")
            article = generate_article(topic, angle, article_type, style, word_count, text_profile, demo_mode=demo_mode, app_mode=app_mode, network_settings=network_settings)
            if not demo_enabled and _is_demo_content(article):
                raise ProviderError("DEMO_CONTENT_IN_PRODUCTION", "生产模式拒绝演示文章")
            article["id"] = article_id
            article["topic"] = topic.title
            article["angle"] = angle["name"]
            article["article_type"] = article_type
            article["style"] = style
            article["source_urls"] = [topic.url] if topic.url else []
            assets = build_image_assets(article, image_style, max_images=3)
            for image_index, asset in enumerate(assets, start=1):
                raw_path = article_dir / f"image_{image_index:02d}_raw.png"
                final_path = article_dir / f"image_{image_index:02d}.png"
                try:
                    if progress:
                        progress(f"正在生成图片 {index}/{len(jobs)} · {image_index}/{len(assets)}")
                    image_provider.generate(asset.prompt, raw_path)
                    if asset.role == "cover":
                        add_cover_title(raw_path, article.get("title", topic.title), final_path)
                    else:
                        final_path.write_bytes(raw_path.read_bytes())
                    asset.path = str(final_path)
                    asset.status = "completed"
                except Exception as exc:
                    asset.status = "failed"
                    asset.error = str(exc)
                    task["errors"].append({"article_id": article_id, "code": _error_code(exc), "message": str(exc)})
                article.setdefault("images", []).append(asset.to_dict())
            failed_images = [item for item in article.get("images", []) if item.get("status") == "failed"]
            if failed_images:
                raise ProviderError("IMAGE_MODEL_NOT_CONFIGURED" if not image_profile.get("api_key") and not demo_enabled else "IMAGE_GENERATION_FAILED")
            article["docx_path"] = str(export_article(article, article_dir / f"{article_id}.docx"))
            article["status"] = "demo_completed" if demo_enabled else "completed"
            generated.append(article)
        except Exception as exc:
            article_task["status"] = "failed"
            article_task["error_code"] = _error_code(exc)
            article_task["error"] = str(exc)
            task["errors"].append({"article_id": article_id, "code": _error_code(exc), "message": str(exc)})
            generated.append(article_task)
        task["articles"] = generated
        save_task(task)
    completed_articles = [item for item in generated if item.get("status") == "completed"]
    duplicate_titles = duplicate_pairs([item.get("title", "") for item in completed_articles], threshold=0.70)
    task["similarity_pairs"] = [{"left": left, "right": right, "score": round(score, 4)} for left, right, score in duplicate_titles]
    if duplicate_titles:
        task["errors"].append({"code": "SIMILAR_TITLE", "message": "检测到标题相似度过高，请人工检查或重生成"})
    exportable_articles = [item for item in generated if item.get("status") in {"completed", "demo_completed"}]
    task["combined_docx"] = str(export_combined(exportable_articles, task_dir / "文章合集.docx")) if exportable_articles else ""
    task["zip_path"] = str(export_zip(task_dir, task_dir / "全部结果.zip"))
    statuses = [item.get("status") for item in generated]
    if any(status == "failed" for status in statuses):
        task["status"] = "failed"
    elif demo_enabled and statuses and all(status == "demo_completed" for status in statuses):
        task["status"] = "demo_completed"
    elif statuses and all(status == "completed" for status in statuses):
        task["status"] = "completed"
    else:
        task["status"] = "failed"
    if not demo_enabled and any(_is_demo_content(item) for item in generated if item.get("status") != "failed"):
        task["status"] = "failed"
        task["errors"].append({"code": "DEMO_CONTENT_IN_PRODUCTION", "message": "生产任务包含演示内容"})
    save_task(task)
    return task
