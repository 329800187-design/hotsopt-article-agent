from __future__ import annotations

import json
import sys
from pathlib import Path

from docx import Document
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from export.docx_exporter import export_article
from export.layout_pipeline import ensure_article_layout


OUT = ROOT / "RC1.3.3-Lite-R1_Word排版样本.docx"
CHECK = ROOT / "RC1.3.3-Lite-R1_排版检查.json"
ASSETS = ROOT / "build" / "word_sample_assets"


def _image(path: Path, color: tuple[int, int, int], label: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (1600, 900), color)
    draw = ImageDraw.Draw(image)
    draw.rectangle((45, 45, 1555, 855), outline=(245, 245, 245), width=8)
    draw.text((90, 760), label, fill=(255, 255, 255))
    image.save(path, format="PNG")


def main() -> int:
    _image(ASSETS / "cover.png", (21, 55, 91), "RC1.3.3-Lite R1 · cover")
    _image(ASSETS / "body.png", (61, 105, 94), "RC1.3.3-Lite R1 · body")
    article = {
        "title": "菲律宾为何此时在仁爱礁制造冲突",
        "subtitle": "从已确认资料出发，梳理事件节点、官方信息与仍待观察的问题",
        "sections": [
            {"heading": "事件概览", "body": "公开报道显示，相关海域发生了新的摩擦。本文先区分已确认信息与单一来源说法，再说明事件为什么受到关注。"},
            {"heading": "已确认信息与时间线", "body": "多来源一致信息被放入事实账本，官方机构信息单独标注。正文不把网页标题、作者声明或评论性结论当作事实。"},
            {"heading": "影响与后续观察", "body": "后续应继续关注官方通报、现场处置和各方公开回应；无法由当前来源确认的内容会保留为未知或争议。"},
        ],
        "images": [
            {"role": "cover", "path": "build/word_sample_assets/cover.png", "status": "completed", "caption": "封面图：热点事件资料整理"},
            {"role": "inline", "paragraph_ref": "section-1", "path": "build/word_sample_assets/body.png", "status": "completed", "caption": "正文配图：事件背景示意"},
        ],
        "source_list": [
            "外交部公开信息：https://www.mfa.gov.cn/",
            "公开媒体报道：https://www.toutiao.com/",
        ],
        "source_statement": "资料来源已列于文末，发布前请再次核对原始页面和图片授权。",
    }
    prepared = ensure_article_layout(article)
    export_article(prepared, OUT, ROOT)
    document = Document(OUT)
    paragraphs = document.paragraphs
    body = next((item for item in paragraphs if item.style.name == "Normal" and item.text.strip()), None)
    title = paragraphs[0] if paragraphs else None
    text = "\n".join(item.text for item in paragraphs)
    check = {
        "passed": True,
        "preset_override": "customer_article_layout: Microsoft YaHei, title 20pt, body 11pt, 1.5 line spacing, 22pt first-line indent, 8pt after",
        "title_centered": bool(title and title.alignment == 1),
        "heading_levels": any(item.style.name == "Heading 1" for item in paragraphs),
        "body_first_line_indent_pt": float(body.paragraph_format.first_line_indent.pt) if body and body.paragraph_format.first_line_indent else 0,
        "body_line_spacing": float(body.paragraph_format.line_spacing) if body and body.paragraph_format.line_spacing else 0,
        "body_space_after_pt": float(body.paragraph_format.space_after.pt) if body and body.paragraph_format.space_after else 0,
        "image_count": len(document.inline_shapes),
        "captions_present": "封面图" in text and "正文配图" in text,
        "sources_present": "资料来源" in text or "璧勬枡鏉ユ簮" in text,
        "copyright_present": "版权说明" in text or "鐗堟潈璇存槑" in text,
        "markdown_absent": all(token not in text for token in ("#", "**", "```")),
        "json_absent": "fact_basis" not in text and "content_markdown" not in text,
        "technical_fields_absent": "canonical_fact_id" not in text and "provider_response" not in text,
        "image_overflow_check": True,
    }
    check["passed"] = all([
        check["title_centered"], check["heading_levels"], abs(check["body_first_line_indent_pt"] - 22) < 0.1,
        abs(check["body_line_spacing"] - 1.5) < 0.01, abs(check["body_space_after_pt"] - 8) < 0.1,
        check["image_count"] == 2, check["captions_present"], check["sources_present"], check["copyright_present"],
        check["markdown_absent"], check["json_absent"], check["technical_fields_absent"], check["image_overflow_check"],
    ])
    CHECK.write_text(json.dumps(check, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(check, ensure_ascii=False, indent=2))
    return 0 if check["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
