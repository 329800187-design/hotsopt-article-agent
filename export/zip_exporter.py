from __future__ import annotations

import re
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from export.docx_exporter import export_article, export_combined, ensure_article_ready_for_docx_export
from modules.security import sanitize_sensitive_data


def safe_filename(value: str, fallback: str = "文章", max_length: int = 80) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(value or "")).strip(" .")
    return (cleaned or fallback)[:max_length]


def export_zip(folder: Path, output_path: Path) -> Path:
    """Create a ZIP using relative POSIX paths only."""
    folder = folder.resolve()
    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(folder.rglob("*")):
            if not path.is_file() or path.resolve() == output_path:
                continue
            archive.write(path, path.relative_to(folder).as_posix())
    return output_path


def export_article_bundle(article: dict[str, Any], task_root: Path, output_path: Path) -> Path:
    """Export one user-facing article package without internal task metadata."""
    ensure_article_ready_for_docx_export(article)
    title = safe_filename(str(article.get("title") or "文章"))
    with tempfile.TemporaryDirectory(prefix="article-export-") as temporary:
        staging = Path(temporary) / title
        staging.mkdir(parents=True, exist_ok=True)
        clean_article = sanitize_sensitive_data(article)
        export_article(clean_article, staging / f"{title}.docx", task_root)
        images = clean_article.get("images") or []
        for index, item in enumerate(images, start=1):
            source = task_root / str(item.get("path") or "")
            if source.is_file() and item.get("status") == "completed":
                name = "cover.png" if item.get("role") == "cover" else f"正文图片{index:02d}.png"
                shutil.copy2(source, staging / name)
        (staging / "使用说明.txt").write_text("本文件由热点图文工作台生成。发布前请复核事实、来源和图片版权。", encoding="utf-8")
        return export_zip(staging, output_path)


def export_batch_bundle(articles: list[tuple[dict[str, Any], Path]], output_path: Path, batch_name: str = "本次创作") -> Path:
    for article, _ in articles:
        ensure_article_ready_for_docx_export(article)
    with tempfile.TemporaryDirectory(prefix="batch-export-") as temporary:
        staging = Path(temporary) / safe_filename(batch_name, "本次创作")
        staging.mkdir(parents=True, exist_ok=True)
        for index, (article, task_root) in enumerate(articles, start=1):
            folder = staging / f"{index:02d}_{safe_filename(str(article.get('title') or '文章'))}"
            folder.mkdir(parents=True, exist_ok=True)
            title = safe_filename(str(article.get("title") or f"文章{index}"))
            export_article(sanitize_sensitive_data(article), folder / f"{title}.docx", task_root)
            for image_index, item in enumerate(article.get("images") or [], start=1):
                source = task_root / str(item.get("path") or "")
                if source.is_file() and item.get("status") == "completed":
                    name = "cover.png" if item.get("role") == "cover" else f"正文图片{image_index:02d}.png"
                    shutil.copy2(source, folder / name)
        combined_articles: list[dict[str, Any]] = []
        for article, task_root in articles:
            combined = sanitize_sensitive_data(article)
            combined["images"] = [
                {**item, "path": str(task_root / str(item.get("path") or ""))}
                for item in combined.get("images") or []
            ]
            combined_articles.append(combined)
        export_combined(combined_articles, staging / "本次创作合集.docx")
        return export_zip(staging, output_path)
