from __future__ import annotations

from typing import Any

from modules.models import ImageAsset


def build_cover_prompt(article: dict[str, Any], style: str) -> str:
    first_brief = (article.get("sections") or [{}])[0].get("image_brief", "")
    summary = article.get("summary") or article.get("intro") or ""
    angle_name = article.get("angle_name") or (article.get("angle_plan") or {}).get("angle_name") or (article.get("angle_plan") or {}).get("name") or ""
    return f"{style}; create a Chinese news illustration COVER, wide hero composition, for article title: {article.get('title', '')}. Article angle: {angle_name}. Core subject: {summary}. Scene cue: {first_brief}. Use a distinct cover composition with one clear visual focus, editorial news illustration, no text, no letters, no logo, no watermark."


def build_image_assets(article: dict[str, Any], style: str, max_images: int = 3) -> list[ImageAsset]:
    sections = article.get("sections") or []
    assets = [
        ImageAsset(
            role="cover",
            paragraph_ref=None,
            prompt=f"{style}，围绕文章主题“{article.get('title', '')}”设计社交媒体横版封面，画面主体突出，具有新闻插画叙事感，{article.get('sections', [{}])[0].get('image_brief', '') if article.get('sections') else ''}，无任何文字、字母、Logo、水印。",
        )
    ]
    for index, section in enumerate(sections[: max(0, max_images - 1)], start=1):
        assets.append(
            ImageAsset(
                role="inline",
                paragraph_ref=f"section-{index}",
                prompt=f"{style}，为文章小标题“{section.get('heading', '')}”生成内容匹配的正文配图。画面表达：{section.get('image_brief', '')}。不要复刻真实公众人物，不要文字、字母、Logo、水印。",
            )
        )
    return assets


def plan_inline_image_assets(
    article: dict[str, Any],
    style: str,
    exact_count: int | None = None,
    min_images: int = 2,
    max_images: int = 4,
) -> list[dict[str, Any]]:
    """Plan inline assets without generating any files.

    `exact_count` is used by the formal execution chain to enforce the paid
    image plan precisely. Legacy tests can still omit it and keep the old 2-4
    planning behavior.
    """
    sections = [section for section in article.get("sections") or [] if isinstance(section, dict)]
    if exact_count is not None:
        count = max(0, int(exact_count))
    else:
        count = min(max_images, max(min_images, len(sections) or min_images))
    plans: list[dict[str, Any]] = []
    for index in range(1, count + 1):
        section = sections[index - 1] if index - 1 < len(sections) else {}
        title = str(section.get("heading") or f"正文要点 {index}").strip()
        brief = str(section.get("image_brief") or section.get("body") or article.get("summary") or "表现文章对应的现实场景").strip()
        composition = ["时间线与关键细节", "人物关系和环境", "生活影响与对比", "未来走向与选择"][index - 1]
        prompt = (
            f"{style}，为文章《{article.get('title', '')}》的小标题“{title}”生成正文配图。"
            f"用途：解释或强化本段内容；画面表达：{brief[:240]}。"
            f"采用{composition}构图，与封面使用不同主体、不同景别、不同背景和不同视觉重点；"
            "避免复刻封面构图，不要文字、字母、Logo、水印，不要使用随机占位图。"
        )
        plans.append({
            "image_id": f"section-{index}",
            "slot_id": f"section-{index}",
            "role": "inline",
            "order": index,
            "paragraph_ref": f"section-{index}",
            "section_title": title,
            "insert_after_paragraph": max(1, index * 2),
            "purpose": brief[:240],
            "prompt": prompt,
            "status": "pending",
            "file_path": "",
            "path": "",
            "error_code": "",
            "error": None,
            "attempt_count": 0,
            "metadata": {},
            "fallback_available": False,
        })
    return plans
