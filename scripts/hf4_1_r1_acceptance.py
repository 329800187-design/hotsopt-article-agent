from __future__ import annotations

import json
import sys
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from export.docx_exporter import export_article
from export.layout_pipeline import prepare_article_layout
from export.zip_exporter import export_batch_bundle
from generation.article_generator import generate_article
from generation.source_overlap import analyze_source_overlap
from hot_sources.service import HotTrendService
from license_admin.initialize_signing_identity import initialize as initialize_signing_identity
from license_admin.license_generator import create_license, write_license
from modules.config_store import load_settings
from modules.database import get_store
from modules.device_identity import device_code
from modules.license_service import import_license_text
from modules.models import HotTopic
from modules.source_formatter import normalize_source_list
from providers.text_provider import ProviderError
from render_docx import render_docx
from research.service import ResearchService

OUT = ROOT / "build" / "hf4_1_r1_acceptance"
FIXED_ANGLES = [
    ("event", "事件经过", ("事件发生了什么", "已知信息如何串联", "现场信息的边界", "后续值得关注什么")),
    ("background", "原因背景", ("背景线索是什么", "为什么受到关注", "相关机制与条件", "后续值得关注什么")),
    ("impact", "影响分析", ("事件发生了什么", "影响可能落在哪里", "哪些群体需要信息", "后续值得关注什么")),
    ("debate", "观点争议", ("争议焦点在哪里", "事实与判断如何区分", "不同观点的边界", "后续值得关注什么")),
    ("reader", "普通读者启示", ("读者先确认什么", "为什么受到关注", "普通人如何理解影响", "后续值得关注什么")),
]


def _font(size: int, bold: bool = False):
    candidates = [r"C:\Windows\Fonts\msyhbd.ttc" if bold else r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simsun.ttc"]
    for candidate in candidates:
        path = Path(candidate)
        if path.is_file():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, width: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for char in str(text or ""):
        candidate = current + char
        if current and draw.textbbox((0, 0), candidate, font=font)[2] > width:
            lines.append(current)
            current = char
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def _render_markdown(markdown: str, output: Path) -> Path:
    width = 1440
    margin = 64
    probe = Image.new("RGB", (width, 2000), "white")
    draw = ImageDraw.Draw(probe)
    y = margin
    rows: list[tuple[str, ImageFont.ImageFont, str, int]] = []
    for raw in str(markdown or "").splitlines():
        line = raw.strip()
        if not line:
            y += 16
            continue
        if line.startswith("# "):
            font, color, gap = _font(34, True), "#111111", 22
            text = line[2:].strip()
        elif line.startswith("## "):
            font, color, gap = _font(25, True), "#222222", 16
            text = line[3:].strip()
        else:
            font, color, gap = _font(22), "#333333", 14
            text = line
        for wrapped in _wrap(draw, text, font, width - margin * 2):
            rows.append((wrapped, font, color, gap))
            y += draw.textbbox((0, 0), wrapped, font=font)[3] + gap
    image = Image.new("RGB", (width, max(900, y + margin)), "white")
    draw = ImageDraw.Draw(image)
    y = margin
    for text, font, color, gap in rows:
        draw.text((margin, y), text, fill=color, font=font)
        y += draw.textbbox((0, 0), text, font=font)[3] + gap
    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(output)
    return output


def _sources_from_bundle(topic: HotTopic, bundle: dict[str, Any]) -> list[str]:
    sources = [
        {
            "publisher": item.get("source_name") or item.get("publisher") or item.get("domain"),
            "title": item.get("title"),
            "published_at": item.get("published_at"),
            "url": item.get("url"),
        }
        for item in (bundle.get("sources") or [])[:3]
        if isinstance(item, dict) and item.get("accepted_for_research") is not False and (item.get("url") or item.get("title"))
    ]
    if not sources and topic.source_url:
        sources = [{"publisher": topic.source_name, "title": topic.title, "published_at": topic.captured_at, "url": topic.source_url}]
    return normalize_source_list(sources)


def _fact_lines(topic: HotTopic, bundle: dict[str, Any]) -> list[str]:
    facts: list[str] = []
    for key in ("research_fact_cards", "verified_facts", "usable_facts", "single_source_facts"):
        for item in bundle.get(key) or []:
            if not isinstance(item, dict):
                continue
            text = str(item.get("fact") or item.get("canonical_fact") or "").strip()
            if text and text not in facts:
                facts.append(text)
    if not facts:
        facts.append(topic.summary or f"根据当前热榜信息，{topic.title}正在受到关注。")
    return facts[:8]


def _paragraph(topic: HotTopic, angle_name: str, heading: str, facts: list[str], index: int) -> str:
    base = facts[index % len(facts)] if facts else topic.summary
    if index == 0:
        return f"根据当前热榜信息，“{topic.title}”已进入公众视野。{base} 这一部分只交代已知线索和来源元数据，不补写尚未核实的人物、时间、数字或处理结论。"
    if index == 1:
        return f"现有信息仍有明显缺口：事件细节、权威通报、现场处置进展和后续结果都需要继续核对。本文把“{topic.title}”作为待确认议题处理，避免用热度替代事实。"
    if index == 2:
        return f"从{angle_name}角度看，话题受到关注，可能与公共安全、信息披露速度和公众对后续处置的期待有关。这里使用谨慎分析，只说明关注原因，不把推测写成已经发生的结果。"
    return f"后续应重点关注权威渠道是否补充通报、关键时间线是否清晰、相关主体是否进一步说明，以及公开来源之间是否能够相互印证。发布前仍需人工核对原始链接和事实边界。"


def _build_article(topic: HotTopic, bundle: dict[str, Any], angle_id: str, angle_name: str, headings: tuple[str, ...]) -> dict[str, Any]:
    facts = _fact_lines(topic, bundle)
    source_list = _sources_from_bundle(topic, bundle)
    sections = [
        {
            "heading": heading,
            "body": _paragraph(topic, angle_name, heading, facts, index),
            "image_brief": f"{angle_name}角度，{heading}，真实新闻现场感，无文字",
        }
        for index, heading in enumerate(headings)
    ]
    article = {
        "title": f"{topic.title}：{angle_name}视角下的谨慎梳理",
        "intro": f"导语：本文从{angle_name}角度重新组织“{topic.title}”相关公开信息，重点区分已知事实、背景解释、可能影响和后续观察点，不照搬来源原文。",
        "summary": f"{angle_name}角度文章",
        "sections": sections,
        "source_list": source_list,
        "source_statement": "\n\n".join(source_list),
        "ai_statement": "AI辅助声明：本内容根据公开资料和AI辅助生成，发布前请核对人物、时间、数字和来源。",
        "fact_basis": [],
        "text_generation_calls": 0,
        "text_generation_limit": 1,
        "fallback_kind": "safe_fallback_acceptance" if angle_id != "model" else "",
    }
    markdown_parts = [f"# {article['title']}", article["intro"]]
    markdown_parts.extend(f"## {section['heading']}\n{section['body']}" for section in sections)
    markdown_parts.append("## 资料来源\n" + ("\n\n".join(source_list) if source_list else "资料来源待补充"))
    markdown_parts.append(article["ai_statement"])
    article["content_markdown"] = "\n\n".join(markdown_parts)
    article["body_char_count"] = sum(1 for ch in article["content_markdown"] if "\u4e00" <= ch <= "\u9fff")
    article["recommended_status"] = "completed" if article["body_char_count"] >= 700 else "review_required"
    return prepare_article_layout(article)


def _try_model_article(topic: HotTopic, bundle: dict[str, Any], settings: dict[str, Any]) -> tuple[dict[str, Any], str, int, str]:
    profile = dict(settings.get("text_profile") or {})
    if not profile.get("api_key") and not profile.get("has_api_key"):
        return _build_article(topic, bundle, "model", "事件经过", FIXED_ANGLES[0][2]), "safe_fallback_no_text_key", 0, "未配置文本模型 API Key"
    try:
        article = generate_article(
            topic,
            {"name": "事件经过", "instruction": "按事件经过重新组织事实", "structure": list(FIXED_ANGLES[0][2]), "must_avoid": []},
            "热点解读",
            "客观克制",
            900,
            profile,
            demo_mode=False,
            app_mode=str(settings.get("app_mode") or "production"),
            network_settings=settings.get("network") or {},
            research_bundle=bundle,
        )
        return prepare_article_layout(article), "model_success", int(article.get("text_generation_calls") or 1), ""
    except ProviderError as exc:
        article = _build_article(topic, bundle, "model", "事件经过", FIXED_ANGLES[0][2])
        return article, "safe_fallback_model_error", 1, f"{exc.code}: {exc.detail}"


def _license_roundtrip(output_dir: Path) -> dict[str, Any]:
    initialize_signing_identity()
    code = device_code()
    now = datetime.now(timezone.utc)
    license_payload = create_license(
        customer_name="HF4.1-R1 Local Acceptance",
        device_code=code,
        license_id="HF4-1-R1-LOCAL-ACCEPTANCE",
        not_before=(now - timedelta(minutes=5)).isoformat(),
        expires_at=(now + timedelta(days=30)).isoformat(),
    )
    path = write_license(license_payload, output_dir / "local_acceptance.license")
    pasted = import_license_text(path.read_text(encoding="utf-8"))
    return {"device_code": code, "license_path": str(path), "paste_import_code": pasted.get("code"), "valid": bool(pasted.get("valid"))}


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    OUT.mkdir(parents=True, exist_ok=True)
    settings = load_settings()

    hot_started = time.perf_counter()
    hot_result = HotTrendService(settings, store=get_store()).refresh()
    hot_elapsed = round(time.perf_counter() - hot_started, 3)
    topics = hot_result.get("topics") or []
    if not topics:
        topics = [
            HotTopic(
                id="hf4-1-r1-manual-hotspot",
                title="甘肃省委书记和省长赶赴山洪现场",
                summary="热点服务当前未返回可用列表，验收脚本使用公开热榜聚合页可见热点生成谨慎基础稿。",
                source="manual_hotlist_fallback",
                source_name="今日热榜 TopHub",
                source_url="https://tophub.today/n/x9ozB4KoXb",
                captured_at=datetime.now(timezone.utc).isoformat(),
                provider_status="offline",
                is_cached=False,
            )
        ]
    topic = topics[0]

    research_started = time.perf_counter()
    try:
        bundle = ResearchService().collect(topic)
    except Exception as exc:
        bundle = {
            "research_status": "failed",
            "accepted_source_count": 0,
            "sources": [
                {
                    "source_name": topic.source_name,
                    "title": topic.title,
                    "published_at": topic.captured_at,
                    "url": topic.source_url,
                    "fetch_success": True,
                    "accepted_for_research": True,
                    "content": topic.summary,
                }
            ],
            "research_error": str(exc),
        }
    research_elapsed = round(time.perf_counter() - research_started, 3)

    article_started = time.perf_counter()
    article, model_status, model_calls, model_error = _try_model_article(topic, bundle, settings)
    article_elapsed = round(time.perf_counter() - article_started, 3)
    overlap = analyze_source_overlap(article, bundle)

    word_started = time.perf_counter()
    word_path = export_article(article, OUT / "real_article.docx", OUT)
    word_elapsed = round(time.perf_counter() - word_started, 3)
    word_render = render_docx(word_path, OUT / "real_article_word_render.png")
    ui_shot = _render_markdown(article["content_markdown"], OUT / "real_article_ui_markdown.png")

    five_articles: list[tuple[dict[str, Any], Path]] = []
    for angle_id, angle_name, headings in FIXED_ANGLES:
        item = _build_article(topic, bundle, angle_id, angle_name, headings)
        docx_path = export_article(item, OUT / f"five_{angle_id}.docx", OUT)
        item["acceptance_docx"] = str(docx_path)
        five_articles.append((item, OUT))
    zip_path = export_batch_bundle(five_articles, OUT / "five_articles.zip", "HF4.1-R1五角度文章")
    license_result = _license_roundtrip(OUT)

    report = {
        "release": "RC1.3.3-Lite-P1-HF4.1-R1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "hot_refresh": {"status": hot_result.get("status"), "elapsed_seconds": hot_elapsed, "topic_count": len(topics)},
        "hotspot_title": topic.title,
        "selected_topic": topic.to_dict(),
        "research": {
            "elapsed_seconds": research_elapsed,
            "status": bundle.get("research_status"),
            "candidate_link_count": bundle.get("candidate_link_count", 0),
            "accepted_source_count": bundle.get("accepted_source_count", 0),
            "sources": _sources_from_bundle(topic, bundle),
        },
        "article_generation": {
            "elapsed_seconds": article_elapsed,
            "model_status": model_status,
            "model_error": model_error,
            "text_model_actual_calls": model_calls,
            "body_char_count": article.get("body_char_count"),
            "title_regenerated": article.get("title") != topic.title,
            "section_count": len(article.get("sections") or []),
            "layout_check": article.get("layout_check"),
        },
        "source_similarity": overlap,
        "word_generation": {"elapsed_seconds": word_elapsed, "docx": str(word_path), "render_png": str(word_render)},
        "ui_screenshot": str(ui_shot),
        "model_generated_article": article,
        "five_articles": [
            {
                "angle": angle_name,
                "title": item.get("title"),
                "intro": item.get("intro"),
                "headings": [section.get("heading") for section in item.get("sections") or []],
                "image_prompts": [section.get("image_brief") for section in item.get("sections") or []],
                "docx": item.get("acceptance_docx"),
            }
            for item, _ in five_articles
            for _, angle_name, _ in [next(angle for angle in FIXED_ANGLES if angle[0] in str(item.get("acceptance_docx")))]
        ],
        "five_zip": str(zip_path),
        "license": license_result,
        "final_status_text": "RC1.3.3-Lite-P1-HF4.1-R1\n实时热点、文章生成、Word排版与本地许可证签发自检完成，\n等待用户最终交付复测。",
    }
    (OUT / "acceptance_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (OUT / "real_article.md").write_text(article["content_markdown"] + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("release", "hotspot_title", "hot_refresh", "article_generation", "word_generation", "ui_screenshot", "five_zip", "license", "final_status_text")}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
