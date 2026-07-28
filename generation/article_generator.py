from __future__ import annotations

import re
from typing import Any

from generation.angle_planner import plan_angles
from generation.image_budget import count_body_chinese_chars, recommended_word_count
from modules.models import HotTopic
from modules.source_formatter import normalize_source_list
from providers.text_provider import OpenAITextProvider, ProviderError, parse_json_response
from providers.contracts import ArticleGenerationRequest


MIN_SECTIONS = 3
TARGET_BODY_CHINESE_CHARS = 700
MIN_EXPORTABLE_BODY_CHINESE_CHARS = 700
MIN_FALLBACK_BODY_CHINESE_CHARS = 300
MAX_TEXT_GENERATION_CALLS = 1
_FACT_NORMALIZE_RE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff]+")
REQUIRED_SECTION_HEADINGS = (
    "事件发生了什么",
    "为什么受到关注",
    "可能带来哪些影响",
    "后续值得关注什么",
)
CUSTOM_TOPIC_SECTION_HEADINGS = (
    "核心概念",
    "可执行方法",
    "具体步骤",
    "风险提醒",
    "总结",
)


def _demo_article(topic: HotTopic, angle: dict[str, str], style: str, word_count: int) -> dict[str, Any]:
    title = f"【演示模式】{topic.title}：从{angle['name']}看热点"
    intro = f"【演示模式】本文围绕“{topic.title}”，从“{angle['name']}”角度进行结构展示，仅用于本地流程演示。"
    sections = [
        {
            "heading": "事件概览",
            "body": f"当前话题为：{topic.title}。{topic.summary or '当前公开信息仍在变化，具体细节应以原始来源为准。'}",
            "image_brief": "与话题相关的真实场景，无文字",
        },
        {
            "heading": f"{angle['name']}视角",
            "body": f"从“{angle['name']}”视角看，这个话题更适合梳理公开信息与现实影响，表达风格为“{style}”。",
            "image_brief": f"体现{topic.title}现实影响的新闻画面，无文字",
        },
        {
            "heading": "后续关注",
            "body": "后续信息出现前，应保留来源链接，区分已证实内容和网络讨论，不根据单一标题直接下结论。",
            "image_brief": "读者查看信息并进行理性判断的场景，无文字",
        },
    ]
    article = {
        "title": title,
        "intro": intro,
        "sections": sections,
        "summary": topic.summary or intro,
        "tags": [topic.category, angle["name"], "热点解读"],
        "demo_mode": True,
        "text_generation_calls": 0,
        "text_generation_limit": MAX_TEXT_GENERATION_CALLS,
        "text_generation_second_call_reason": "",
        "fact_basis": [],
        "source_list": [],
        "ai_statement": "AI辅助声明：本文基于公开资料整理生成，发布前请再次核对关键信息。",
    }
    article = _complete_article_structure(article, topic, angle)
    article["body_char_count"] = count_body_chinese_chars(article)
    article["recommended_status"] = "completed"
    return article


def _normalize_fact(value: str) -> str:
    return _FACT_NORMALIZE_RE.sub("", str(value or "")).lower()


def _trim_text(value: str, max_chars: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def _accepted_sources(research_bundle: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not research_bundle:
        return []
    limited_mode = bool(research_bundle.get("hotlist_metadata_available") and str(research_bundle.get("research_status") or "") == "hotlist_limited")
    custom_topic_mode = bool(research_bundle.get("custom_topic") and str(research_bundle.get("research_status") or "") == "custom_topic")
    return [
        item
        for item in research_bundle.get("sources") or []
        if isinstance(item, dict)
        and item.get("fetch_success")
        and (item.get("accepted_for_research") or (limited_mode and item.get("limited_metadata")) or (custom_topic_mode and item.get("custom_topic_input")))
        and not item.get("duplicate_of")
    ]


def _compact_facts(research_bundle: dict[str, Any] | None, limit: int = 10) -> list[dict[str, Any]]:
    bundle = research_bundle or {}
    source_map = {
        str(item.get("source_id")): item
        for item in _accepted_sources(bundle)
        if item.get("source_id")
    }
    candidates = list(bundle.get("verified_facts") or []) + list(bundle.get("usable_facts") or [])
    seen: set[str] = set()
    ranked: list[tuple[tuple[int, int, int, int], dict[str, Any]]] = []
    topic_title = str(bundle.get("topic_title") or "").strip()
    for index, item in enumerate(candidates):
        if not isinstance(item, dict):
            continue
        fact = str(item.get("canonical_fact") or item.get("fact") or "").strip()
        normalized = _normalize_fact(fact)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        source_ids = [str(value) for value in item.get("supporting_source_ids") or item.get("source_ids") or [] if str(value)]
        sources = [source_map.get(source_id) for source_id in source_ids if source_map.get(source_id)]
        official = any(str(source.get("source_level") or "") == "official" for source in sources)
        multi_source = 1 if len(set(source_ids)) >= 2 else 0
        title_related = 1 if topic_title and any(token and token in fact for token in re.findall(r"[\u4e00-\u9fffA-Za-z0-9]{2,}", topic_title)[:4]) else 0
        ranked.append(((-int(official), -multi_source, -title_related, index), item))
    ranked.sort(key=lambda item: item[0])
    return [dict(item) for _, item in ranked[:limit]]


def _fact_card_block(research_bundle: dict[str, Any] | None) -> str:
    if not research_bundle:
        return "\u516c\u5f00\u8d44\u6599\uff1a\u672a\u63d0\u4f9b\u3002\u751f\u4ea7\u6a21\u5f0f\u4e0b\u4e0d\u5f97\u4ec5\u4f9d\u636e\u70ed\u70b9\u6807\u9898\u751f\u6210\u6587\u7ae0\u3002"
    sources = _accepted_sources(research_bundle)[:3]
    fact_cards = [item for item in research_bundle.get("research_fact_cards") or [] if isinstance(item, dict)][:10]
    if not fact_cards:
        fact_cards = [
            {
                "fact_id": str(item.get("fact_id") or f"fact-{index}"),
                "subject": "",
                "action": "",
                "object": "",
                "time": "",
                "location": "",
                "number": "",
                "source_name": "",
                "source_url": "",
                "reliability": "",
                "fact": str(item.get("canonical_fact") or item.get("fact") or "").strip(),
            }
            for index, item in enumerate(_compact_facts(research_bundle, limit=10), start=1)
        ]
    background_cards = [item for item in research_bundle.get("background_fact_cards") or [] if isinstance(item, dict)][:5]
    missing_value = "\u672a\u63d0\u53d6"
    unlabeled = "\u672a\u6807\u6ce8"
    unknown_source = "\u672a\u77e5\u6765\u6e90"
    untitled = "\u672a\u547d\u540d\u6807\u9898"
    unknown = "\u672a\u77e5"
    none_value = "\u65e0"

    def _line(card: dict[str, Any]) -> str:
        fact_text = str(card.get("fact") or card.get("canonical_fact") or "").strip()
        parts = [
            f"subject={str(card.get('subject') or '').strip() or missing_value}",
            f"action={str(card.get('action') or '').strip() or missing_value}",
            f"object={str(card.get('object') or '').strip() or missing_value}",
            f"time={str(card.get('time') or '').strip() or missing_value}",
            f"location={str(card.get('location') or '').strip() or missing_value}",
            f"number={str(card.get('number') or '').strip() or missing_value}",
            f"source={str(card.get('source_name') or '').strip() or unlabeled}",
            f"reliability={str(card.get('reliability') or '').strip() or unlabeled}",
        ]
        if fact_text:
            parts.append(f"fact={fact_text}")
        return f"- {str(card.get('fact_id') or 'fact')}: " + "\uff1b".join(parts)

    source_lines = "\n".join(
        f"- [{index}] {item.get('source_name') or item.get('publisher') or item.get('domain') or unknown_source}\uff1a\u300a{item.get('title') or untitled}\u300b\uff0c\u65e5\u671f\uff1a{item.get('published_at') or unknown}\uff0c\u94fe\u63a5\uff1a{item.get('url') or none_value}"
        for index, item in enumerate(sources, start=1)
    )
    fact_lines = "\n".join(_line(card) for card in fact_cards)
    background_lines = "\n".join(_line(card) for card in background_cards)
    timeline = "\uff1b".join(str(item) for item in (research_bundle.get("timeline") or [])[:6]) or none_value
    people = "\uff1b".join(str(item) for item in (research_bundle.get("key_people") or [])[:5]) or none_value
    orgs = "\uff1b".join(str(item) for item in (research_bundle.get("key_organizations") or [])[:5]) or none_value
    notice = str(research_bundle.get("limited_research_notice") or "").strip()
    custom_notice = str(research_bundle.get("custom_topic_notice") or "").strip()
    return (
        f"\u516c\u5f00\u8d44\u6599\u6574\u7406\u72b6\u6001\uff1a{research_bundle.get('research_status')}\n"
        f"{notice + chr(10) if notice else ''}"
        f"{custom_notice + chr(10) if custom_notice else ''}"
        f"\u5173\u952e\u4e8b\u5b9e\u5361\uff08\u6700\u591a10\u6761\uff09\uff1a\n{fact_lines or none_value}\n"
        f"\u80cc\u666f\u4e8b\u5b9e\u5361\uff08\u6700\u591a5\u6761\uff09\uff1a\n{background_lines or none_value}\n"
        f"\u8d44\u6599\u6765\u6e90\u76ee\u5f55\uff08\u6700\u591a3\u6761\uff0c\u4ec5\u4f9b\u7f72\u540d\uff0c\u4e0d\u5f97\u590d\u8ff0\u539f\u6587\uff09\uff1a\n{source_lines or none_value}\n"
        f"\u65f6\u95f4\u7ebf\uff08\u6700\u591a6\u9879\uff09\uff1a{timeline}\n"
        f"\u5173\u952e\u4eba\u7269\uff08\u6700\u591a5\u4e2a\uff09\uff1a{people}\n"
        f"\u5173\u952e\u673a\u6784\uff08\u6700\u591a5\u4e2a\uff09\uff1a{orgs}"
    )


def _prompt(
    topic: HotTopic,
    angle: dict[str, str],
    article_type: str,
    style: str,
    word_count: int,
    rewrite_context: dict[str, Any] | None = None,
    research_bundle: dict[str, Any] | None = None,
) -> str:
    bundle = research_bundle or {}
    custom_topic_mode = bool(bundle.get("custom_topic") and str(bundle.get("research_status") or "") == "custom_topic")
    structure = "、".join(str(item) for item in angle.get("structure", []))
    must_avoid = "、".join(str(item) for item in angle.get("must_avoid", []))
    requested_chars = max(700, min(int(word_count or 800), 1600))
    target_length_text = "700 到 1000 个中文汉字" if requested_chars <= 1000 else f"约 {requested_chars} 个中文汉字"

    if custom_topic_mode:
        title_text = str(topic.title or "自定义话题").strip()
        summary = str(topic.summary or "").strip()
        prompt = f"""请为下面的手动话题生成一篇可直接编辑的中文文章。直接输出标准 Markdown 正文，不要输出 JSON，不要输出代码围栏。

话题：{title_text}
用户补充说明：{summary or '无'}
文章类型：{article_type}
表达风格：{style}
目标字数：{target_length_text}

文章结构（固定）：
# 新标题
导语
## 核心概念或事件概览
正文
## 可执行方法或背景原因
正文
## 具体步骤或影响分析
正文
## 风险提醒或后续关注
正文
## 总结
正文

要求：
1. 必须包含具体案例、实际场景和方法细节，不得只写空洞模板。
2. 正文 700～1000 个中文汉字。
3. 直接输出标准 Markdown，不要 JSON、不要代码围栏、不要解释文字。
4. 不虚构数据或人名，不承诺无法验证的收益。"""
        normalized_prompt = prompt.strip()
        return normalized_prompt[:3500]

    prompt = f"""请为下面的热点生成一篇可直接编辑的中文文章，严格只使用已提供事实卡，不得虚构人物、时间、数字、金额或事件进展。
你不是摘要工具，也不是改写原文工具。你必须根据事实卡重新构思一篇新的文章，而不是拼接、复述或压缩来源原文。

热点标题：{topic.title}
热点分类：{topic.category}
热点摘要：{topic.summary or ''}
来源链接：{topic.url or ''}
{_fact_card_block(research_bundle)}

创作角度：{angle['name']}（{angle['instruction']}）
角度核心问题：{angle.get('core_question', '')}
开篇策略：{angle.get('opening_strategy', '')}
建议结构：{structure}
必须避免：{must_avoid}
文章类型：{article_type}
表达风格：{style}
目标字数：{target_length_text}

要求：
1. 必须重写标题、导语、段落顺序和核心表达，形成独立新结构。
2. 必须包含：导语、事件概览、背景或原因、影响或意义、后续关注。
3. 正文使用 Markdown：1 个主标题、1 段导语、3 到 5 个二级标题；每段建议 80 到 180 个汉字，并保留空行。
4. 直接输出标准 Markdown 正文，不要输出 JSON、不要代码围栏、不要调试字段。
5. 如果无法返回标准 Markdown，也可以直接返回纯文本正文。"""
    if rewrite_context:
        conflict = rewrite_context.get("conflict_article") or {}
        rewrite_reason = str(rewrite_context.get("reason") or "需要按 HF4.1 规则重写")
        old_title = str(conflict.get("title") or "未提供旧标题")
        old_opening = str(conflict.get("opening") or "未提供旧导语")
        old_headings = "；".join(str(item) for item in conflict.get("headings") or []) or "未提供旧结构"
        prompt += f"""

重写补充要求：
- 重写原因：{rewrite_reason}
- 旧标题：{old_title}
- 旧导语：{old_opening}
- 旧结构：{old_headings}
- 只允许保留事实，不得复用旧稿表达和段落顺序。
- 第二次调用只能执行这一次重写，完成后不得再次调用模型。
"""
    normalized_prompt = prompt.strip()
    return normalized_prompt[:3500]



def _append_sections_to_markdown(article: dict[str, Any]) -> str:
    title = str(article.get("title") or "").strip()
    intro = str(article.get("intro") or "").strip()
    parts: list[str] = []
    if title:
        parts.append(f"# {title}")
    if intro:
        parts.append(intro)
    for section in article.get("sections") or []:
        if not isinstance(section, dict):
            continue
        heading = str(section.get("heading") or "").strip()
        body = str(section.get("body") or "").strip()
        if not body:
            continue
        if heading:
            parts.append(f"## {heading}\n{body}".strip())
        else:
            parts.append(body)
    source_list = normalize_source_list(article.get("source_list") or [])
    if source_list:
        parts.append("## 资料来源\n" + "\n\n".join(source_list))
    ai_statement = str(
        article.get("ai_statement")
        or "AI辅助声明：本文基于公开资料整理生成，发布前请再次核对关键信息。"
    ).strip()
    if ai_statement:
        parts.append(ai_statement)
    return "\n\n".join(part for part in parts if part).strip()


def _topic_source_list(topic: HotTopic) -> list[str]:
    url = str(getattr(topic, "url", "") or getattr(topic, "source_url", "") or "").strip()
    if not url:
        return []
    return normalize_source_list(
        [
            {
                "publisher": getattr(topic, "source_name", "") or getattr(topic, "source", "") or "热榜来源",
                "title": getattr(topic, "title", "") or "热点标题",
                "published_at": getattr(topic, "captured_at", "") or "发布日期未知",
                "url": url,
            }
        ]
    )


def _paragraphs_from_sections(sections: list[dict[str, Any]]) -> list[str]:
    paragraphs: list[str] = []
    for section in sections:
        body = str(section.get("body") or "").strip()
        paragraphs.extend(block.strip() for block in re.split(r"\n\s*\n+", body) if block.strip())
    return paragraphs


def _generic_paragraph(topic: HotTopic, heading: str) -> str:
    templates = {
        "事件发生了什么": f"根据当前热榜信息，{topic.title}已经形成公众关注。现有公开信息有限，本文先围绕已知标题、摘要和来源元数据进行谨慎整理，不补写未经确认的人物、数字、处罚、伤亡或官方结论。",
        "为什么受到关注": "这一话题受到关注，通常与事件本身的信息密度、涉及群体以及后续解释空间有关。从现有信息看，读者需要先区分已经披露的事实、仍待核实的细节和网络讨论中的判断。",
        "可能带来哪些影响": "在缺少更多权威信息前，影响分析应保持克制。它可能影响公众对相关议题的理解，也可能推动更多机构、媒体或当事方补充说明，但具体结果仍需等待后续公开材料确认。",
        "后续值得关注什么": "后续可重点关注权威渠道是否发布进一步说明，原始来源是否补充时间、地点、责任边界和处理进展。发布前仍建议核对人物、时间、数字和来源链接。",
        "核心概念": f"围绕“{topic.title}”，需要先说明它能解决什么问题、适合什么人，以及最终应交付什么结果。",
        "可执行方法": "可以从低成本、小范围、可复用的方向入手，把工具能力转化为服务、内容或流程，而不是只停留在概念介绍。",
        "具体步骤": "建议先选择一个具体场景，做出样例，找到目标用户，完成一次小规模交付，再根据反馈调整流程和报价。",
        "风险提醒": "需要控制投入成本、交付承诺和合规边界。涉及数据、版权、合同或平台规则时，应保留人工核对环节。",
        "总结": "先跑通一个小闭环，再逐步扩大投入。后续可继续补充案例、工具清单和真实成本。",
    }
    return templates.get(heading, "目前公开信息有限，后续仍需等待权威信息确认。")


def _complete_article_structure(article: dict[str, Any], topic: HotTopic, angle: dict[str, str], required_headings: tuple[str, ...] | None = None) -> dict[str, Any]:
    result = dict(article)
    headings = required_headings or REQUIRED_SECTION_HEADINGS
    title = str(result.get("title") or "").strip()
    original_title = str(getattr(topic, "title", "") or "").strip()
    angle_name = str(angle.get("name") or "热点解读").strip()
    if not title or title == original_title:
        result["title"] = f"{original_title}：从{angle_name}看后续影响" if original_title else f"{angle_name}文章"

    intro = str(result.get("intro") or result.get("summary") or "").strip()
    if not intro:
        intro = f"围绕“{original_title}”，本文根据公开资料和当前热榜信息重新梳理事件脉络、关注原因、可能影响与后续观察点。资料有限处将保持谨慎表述，避免把未经确认的推测写成事实。"
    result["intro"] = intro
    result["summary"] = str(result.get("summary") or intro).strip()

    raw_sections = [section for section in (result.get("sections") or []) if isinstance(section, dict) and str(section.get("body") or "").strip()]
    merged_by_heading: dict[str, dict[str, Any]] = {}
    unused: list[dict[str, Any]] = []
    for section in raw_sections:
        heading = str(section.get("heading") or "").strip()
        matched = next((required for required in headings if required in heading or heading in required), "")
        if matched and matched not in merged_by_heading:
            item = dict(section)
            item["heading"] = matched
            merged_by_heading[matched] = item
        else:
            unused.append(dict(section))

    paragraphs = _paragraphs_from_sections(raw_sections)
    completed: list[dict[str, Any]] = []
    for index, heading in enumerate(headings):
        section = dict(merged_by_heading.get(heading) or {})
        body = str(section.get("body") or "").strip()
        if not body and index < len(unused):
            body = str(unused[index].get("body") or "").strip()
        if not body and index < len(paragraphs):
            body = paragraphs[index]
        if not body:
            body = _generic_paragraph(topic, heading)
        section.update(
            {
                "heading": heading,
                "body": "\n\n".join(_split_dense_paragraph(body, target=180)),
                "image_brief": str(section.get("image_brief") or f"{heading}相关的真实新闻场景，无文字").strip(),
            }
        )
        completed.append(section)
    result["sections"] = completed

    sources = normalize_source_list(result.get("source_list") or [])
    if not sources:
        sources = _topic_source_list(topic)
    result["source_list"] = sources
    result["source_statement"] = "\n\n".join(sources)
    result["ai_statement"] = str(
        result.get("ai_statement")
        or "AI辅助声明：本内容根据公开资料和AI辅助生成，发布前请核对人物、时间、数字和来源。"
    ).strip()
    result["content_markdown"] = _append_sections_to_markdown(result)
    return result

def _init_generation_stats(generation_stats: dict[str, Any] | None) -> dict[str, Any]:
    stats = generation_stats if isinstance(generation_stats, dict) else {}
    stats.setdefault("text_generation_calls", int(stats.get("text_generation_calls") or 0))
    stats.setdefault("text_generation_limit", MAX_TEXT_GENERATION_CALLS)
    stats.setdefault("text_generation_second_call_reason", str(stats.get("text_generation_second_call_reason") or ""))
    return stats


def _register_text_generation_call(stats: dict[str, Any], reason: str) -> None:
    calls = int(stats.get("text_generation_calls") or 0)
    limit = int(stats.get("text_generation_limit") or MAX_TEXT_GENERATION_CALLS)
    if calls >= limit:
        raise ProviderError("TEXT_GENERATION_LIMIT_REACHED", "\u5f53\u524d\u6a21\u5f0f\u4e0b\u5355\u7bc7\u6587\u672c\u6a21\u578b\u8c03\u7528\u5df2\u8fbe\u4e0a\u9650")
    if calls == 0 and reason != "full_article":
        raise ProviderError("TEXT_GENERATION_LIMIT_REACHED", "\u9996\u6b21\u8c03\u7528\u5fc5\u987b\u7528\u4e8e\u5b8c\u6574\u6587\u7ae0\u751f\u6210")
    stats["text_generation_calls"] = calls + 1
    if calls == 0:
        stats["text_generation_second_call_reason"] = ""
    else:
        stats["text_generation_second_call_reason"] = str(reason or "rewrite")


def _attach_generation_stats(article: dict[str, Any], stats: dict[str, Any]) -> dict[str, Any]:
    article["text_generation_calls"] = int(stats.get("text_generation_calls") or 0)
    article["text_generation_limit"] = int(stats.get("text_generation_limit") or MAX_TEXT_GENERATION_CALLS)
    article["text_generation_second_call_reason"] = str(stats.get("text_generation_second_call_reason") or "")
    return article

def _strip_code_fence(value: str) -> str:
    text = str(value or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", text, count=1)
        text = re.sub(r"\s*```$", "", text, count=1)
    return text.strip()


def _split_markdown_sections(body: str) -> list[tuple[str, str]]:
    sections: list[tuple[str, str]] = []
    current_heading = ""
    buffer: list[str] = []
    for raw_line in str(body or "").splitlines():
        line = raw_line.strip()
        if re.match(r"^#{2,3}\s+", line):
            if buffer:
                sections.append((current_heading, "\n".join(buffer).strip()))
                buffer = []
            current_heading = re.sub(r"^#{2,3}\s+", "", line).strip()
            continue
        if line.startswith("# "):
            continue
        buffer.append(raw_line.rstrip())
    if buffer:
        sections.append((current_heading, "\n".join(buffer).strip()))
    return [(heading, content.strip()) for heading, content in sections if content.strip()]


def _split_dense_paragraph(text: str, *, target: int = 150) -> list[str]:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if not value:
        return []
    if len(value) <= target:
        return [value]
    sentences = re.split(r"(<=[。！？!])", value)
    chunks: list[str] = []
    buffer = ""
    for sentence in sentences:
        item = sentence.strip()
        if not item:
            continue
        if buffer and len(buffer) + len(item) > target:
            chunks.append(buffer.strip())
            buffer = item
        else:
            buffer = f"{buffer}{item}"
    if buffer.strip():
        chunks.append(buffer.strip())
    return chunks or [value]


def _normalize_section_bodies(sections: list[dict[str, str]]) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    for section in sections:
        body = str(section.get("body") or "").strip()
        paragraphs = _split_dense_paragraph(body)
        item = dict(section)
        item["body"] = "\n\n".join(paragraphs)
        normalized.append(item)
    return normalized

def _split_article_paragraphs(text: str) -> list[str]:
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    blocks = [block.strip() for block in re.split(r"\n\s*\n+", normalized) if block.strip()]
    if len(blocks) >= 2:
        return blocks
    lines = [line.strip() for line in normalized.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if len(lines) >= 2:
        return lines
    return blocks or lines


def _readable_paragraphs(paragraphs: list[str]) -> list[str]:
    return [item for item in paragraphs if len(re.sub(r"\s+", "", item)) >= 30]


def _fallback_response_complete(text: str, title: str, paragraphs: list[str], has_markdown_subheadings: bool) -> bool:
    chinese_chars = sum(1 for ch in str(text or "") if "\u4e00" <= ch <= "\u9fff")
    readable = _readable_paragraphs(paragraphs)
    continuous_body = len(re.sub(r"\s+", "", "\n".join(readable))) >= 220 and bool(readable)
    return bool(
        chinese_chars >= MIN_FALLBACK_BODY_CHINESE_CHARS
        or (bool(title.strip()) and len(readable) >= 2)
        or has_markdown_subheadings
        or continuous_body
    )


def _fallback_sections_from_paragraphs(paragraphs: list[str]) -> list[dict[str, str]]:
    titles = ["\u4e8b\u4ef6\u6982\u89c8", "\u80cc\u666f\u8865\u5145", "\u5f71\u54cd\u4e0e\u89c2\u5bdf"]
    if not paragraphs:
        paragraphs = ["\u516c\u5f00\u8d44\u6599\u8f83\u5c11\uff0c\u5f53\u524d\u4ee5\u5df2\u786e\u8ba4\u4fe1\u606f\u4e3a\u57fa\u7840\u6574\u7406\u3002"]
    chunk_count = max(MIN_SECTIONS, min(len(paragraphs), 3))
    chunks: list[list[str]] = [[] for _ in range(chunk_count)]
    for index, paragraph in enumerate(paragraphs):
        chunks[index % chunk_count].append(paragraph)
    sections: list[dict[str, str]] = []
    for index, chunk in enumerate(chunks):
        body = "\n\n".join(item for item in chunk if item).strip()
        if not body:
            continue
        sections.append(
            {
                "heading": titles[index] if index < len(titles) else f"\u8865\u5145\u5206\u6790 {index + 1}",
                "body": body,
                "image_brief": "\u4e0e\u8be5\u6bb5\u4fe1\u606f\u76f8\u5173\u7684\u73b0\u5b9e\u65b0\u95fb\u573a\u666f\uff0c\u65e0\u6587\u5b57",
            }
        )
    while len(sections) < MIN_SECTIONS:
        sections.append(
            {
                "heading": titles[len(sections)] if len(sections) < len(titles) else f"\u8865\u5145\u5206\u6790 {len(sections) + 1}",
                "body": "\u7ed3\u5408\u73b0\u6709\u516c\u5f00\u8d44\u6599\uff0c\u76f8\u5173\u7ec6\u8282\u4ecd\u503c\u5f97\u6301\u7eed\u5173\u6ce8\u3002",
                "image_brief": "\u4e0e\u8be5\u6bb5\u4fe1\u606f\u76f8\u5173\u7684\u73b0\u5b9e\u65b0\u95fb\u573a\u666f\uff0c\u65e0\u6587\u5b57",
            }
        )
    return sections


def _parse_markdown_article_response(
    response: str,
    topic: HotTopic,
    angle: dict[str, str],
) -> dict[str, Any]:
    text = _strip_code_fence(response)
    if not text:
        raise ProviderError("ARTICLE_PARSE_ERROR", "模型未返回可读正文")
    paragraphs = _split_article_paragraphs(text)
    title = str(topic.title).strip()
    intro = ""
    if paragraphs and paragraphs[0].startswith("# "):
        title = paragraphs[0][2:].strip() or title
        paragraphs = paragraphs[1:]
    elif paragraphs and len(paragraphs[0]) <= 40 and not paragraphs[0].startswith("## "):
        title = paragraphs[0].strip() or title
        paragraphs = paragraphs[1:]
    if paragraphs and not paragraphs[0].startswith("## "):
        intro = paragraphs[0].strip()
    sections = _split_markdown_sections(text)
    has_markdown_subheadings = bool(re.search(r"^#{2,3}\s+\S+", text, re.M))
    if has_markdown_subheadings:
        cleaned_sections = [
            {
                "heading": heading or f"\u6838\u5fc3\u4fe1\u606f {index + 1}",
                "body": body,
                "image_brief": "\u4e0e\u8be5\u6bb5\u4fe1\u606f\u76f8\u5173\u7684\u73b0\u5b9e\u65b0\u95fb\u573a\u666f\uff0c\u65e0\u6587\u5b57",
            }
            for index, (heading, body) in enumerate(sections)
            if body.strip()
        ]
    else:
        content_paragraphs = [block for block in paragraphs[1:] if block.strip()] if intro else paragraphs
        cleaned_sections = _fallback_sections_from_paragraphs(content_paragraphs)
    cleaned_sections = _normalize_section_bodies(cleaned_sections)
    fallback_angle_name = angle.get("name") or "\u70ed\u70b9\u89e3\u8bfb"
    article = {
        "title": title or f"{topic.title}\uff1a{fallback_angle_name}",
        "intro": intro or f"\u56f4\u7ed5\u201c{topic.title}\u201d\uff0c\u672c\u6587\u57fa\u4e8e\u516c\u5f00\u8d44\u6599\u6574\u7406\u76ee\u524d\u53ef\u4ee5\u786e\u8ba4\u7684\u4fe1\u606f\u3002",
        "sections": cleaned_sections[: max(MIN_SECTIONS, len(cleaned_sections))],
        "summary": intro or topic.summary or "",
        "tags": [topic.category, angle.get("name") or "\u70ed\u70b9\u89e3\u8bfb"],
        "fact_basis": [],
        "demo_mode": False,
        "response_format_warning": True,
        "format_warning": "\u6587\u7ae0\u5df2\u751f\u6210\uff0c\u4f46\u6a21\u578b\u8fd4\u56de\u683c\u5f0f\u4e0d\u6807\u51c6\uff0c\u5df2\u81ea\u52a8\u8f6c\u6362\u4e3a\u53ef\u7f16\u8f91\u6587\u7ae0\u3002",
        "fallback_kind": "markdown_fallback" if has_markdown_subheadings or text.lstrip().startswith("#") else "plain_text_fallback",
        "ai_statement": "AI\u8f85\u52a9\u58f0\u660e\uff1a\u672c\u6587\u57fa\u4e8e\u516c\u5f00\u8d44\u6599\u6574\u7406\u751f\u6210\uff0c\u53d1\u5e03\u524d\u8bf7\u518d\u6b21\u6838\u5bf9\u5173\u952e\u4fe1\u606f\u3002",
    }
    article = _complete_article_structure(article, topic, angle)
    article["fallback_complete"] = _fallback_response_complete(text, article["title"], paragraphs, has_markdown_subheadings)
    if not article["fallback_complete"]:
        raise ProviderError("ARTICLE_PARSE_ERROR", "模型返回内容不足，无法整理为可编辑文章")
    return article


def _clean_article(
    data: dict[str, Any],
    topic: HotTopic,
    angle: dict[str, str],
) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ProviderError("ARTICLE_PARSE_ERROR", "模型返回内容不是有效对象")
    raw_sections = data.get("sections")
    if not isinstance(raw_sections, list):
        raise ProviderError("ARTICLE_PARSE_ERROR", "sections 字段不是有效列表")
    cleaned_sections: list[dict[str, str]] = []
    for index, section in enumerate(raw_sections):
        if not isinstance(section, dict):
            continue
        body = str(section.get("body") or "").strip()
        if not body:
            continue
        cleaned_sections.append(
            {
                "heading": str(section.get("heading") or f"正文 {index + 1}").strip(),
                "body": body,
                "image_brief": str(section.get("image_brief") or "与该段信息相关的真实新闻场景，无文字").strip(),
            }
        )
    if len(cleaned_sections) < MIN_SECTIONS:
        cleaned_sections = _fallback_sections_from_paragraphs([item["body"] for item in cleaned_sections if item.get("body")])
    cleaned_sections = _normalize_section_bodies(cleaned_sections)
    article = {
        "title": str(data.get("title") or f"{topic.title}：{angle.get('name') or '热点解读'}").strip(),
        "intro": str(data.get("intro") or "").strip(),
        "sections": cleaned_sections,
        "summary": str(data.get("summary") or data.get("intro") or topic.summary or "").strip(),
        "tags": [str(tag).strip() for tag in data.get("tags", []) if str(tag).strip()] if isinstance(data.get("tags"), list) else [],
        "fact_basis": data.get("fact_basis") if isinstance(data.get("fact_basis"), list) else [],
        "risk_note": str(data.get("risk_note") or "").strip(),
        "demo_mode": bool(data.get("demo_mode", False)),
        "ai_statement": str(
            data.get("ai_statement")
            or "AI辅助声明：本文基于公开资料整理生成，发布前请再次核对关键信息。"
        ).strip(),
    }
    markdown = str(data.get("content_markdown") or "").strip()
    article["content_markdown"] = markdown or _append_sections_to_markdown(article)
    article = _complete_article_structure(article, topic, angle)
    return article

def generate_article(
    topic: HotTopic,
    angle: dict[str, str],
    article_type: str,
    style: str,
    word_count: int,
    profile: dict[str, Any],
    demo_mode: bool = False,
    app_mode: str = "production",
    network_settings: dict[str, Any] | None = None,
    rewrite_context: dict[str, Any] | None = None,
    research_bundle: dict[str, Any] | None = None,
    generation_stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    requested_word_count = recommended_word_count(word_count)
    demo_enabled = bool(demo_mode and app_mode == "demo")
    stats = _init_generation_stats(generation_stats)
    if not profile.get("api_key") and str(profile.get("auth_type") or "bearer").lower() != "none":
        if not demo_enabled:
            raise ProviderError("MODEL_NOT_CONFIGURED", "\u751f\u4ea7\u6a21\u5f0f\u672a\u914d\u7f6e\u6587\u672c\u6a21\u578b API Key")
        return _demo_article(topic, angle, style, word_count)
    provider = OpenAITextProvider(profile, network_settings=network_settings)
    generation_prompt = _prompt(
        topic,
        angle,
        article_type,
        style,
        requested_word_count,
        rewrite_context,
        research_bundle,
    )
    call_reason = "full_article" if int(stats.get("text_generation_calls") or 0) == 0 else str((rewrite_context or {}).get("reason_code") or "rewrite")
    _register_text_generation_call(stats, call_reason)
    token_budget = 1600 if requested_word_count <= 1000 else 2000
    response = provider.generate(
        generation_prompt,
        temperature=0.6,
        max_tokens=token_budget,
    )
    stripped = _strip_code_fence(response)
    parsed: dict[str, Any] | None = None
    try:
        parsed = parse_json_response(stripped)
    except ProviderError:
        parsed = None
    if parsed is not None:
        try:
            article = _clean_article(parsed, topic, angle)
        except ProviderError:
            article = _parse_markdown_article_response(stripped, topic, angle)
    else:
        article = _parse_markdown_article_response(stripped, topic, angle)
    if research_bundle:
        source_lines = normalize_source_list(
            [
                {
                    "publisher": source.get("source_name") or source.get("publisher") or source.get("domain"),
                    "title": source.get("title"),
                    "published_at": source.get("published_at"),
                    "url": source.get("url"),
                }
                for source in _accepted_sources(research_bundle)[:3]
            ]
        )
        article["source_list"] = source_lines
    else:
        article["source_list"] = normalize_source_list(article.get("source_list") or [])
    article["source_statement"] = "\uFF1B".join(str(item) for item in article.get("source_list") or [])
    article["ai_statement"] = str(
        article.get("ai_statement")
        or "AI\u8f85\u52a9\u58f0\u660e\uff1a\u672c\u6587\u57fa\u4e8e\u516c\u5f00\u8d44\u6599\u6574\u7406\u751f\u6210\uff0c\u53d1\u5e03\u524d\u8bf7\u518d\u6b21\u6838\u5bf9\u5173\u952e\u4fe1\u606f\u3002"
    ).strip()
    required_headings = (
        CUSTOM_TOPIC_SECTION_HEADINGS
        if bool(
            (research_bundle or {}).get("custom_topic")
            and str((research_bundle or {}).get("research_status") or "") == "custom_topic"
        )
        else REQUIRED_SECTION_HEADINGS
    )
    article = _complete_article_structure(article, topic, angle, required_headings)
    article["word_count"] = requested_word_count
    article["body_char_count"] = count_body_chinese_chars(article)
    fallback_complete = bool(article.get("fallback_complete"))
    if article.get("response_format_warning") and fallback_complete:
        article["recommended_status"] = "review_required"
    elif article["body_char_count"] >= TARGET_BODY_CHINESE_CHARS:
        article["recommended_status"] = "completed"
    elif article["body_char_count"] >= MIN_EXPORTABLE_BODY_CHINESE_CHARS:
        article["recommended_status"] = "review_required"
    else:
        article["recommended_status"] = "too_short"
    return _attach_generation_stats(article, stats)


def plan_for_topic(count: int, selected_ids: list[str] | None = None) -> list[dict[str, Any]]:
    return plan_angles(min(max(count, 1), 5), selected_ids)





