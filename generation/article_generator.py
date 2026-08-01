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
# ── R1.2.1 动态字数，旧常量仅用于兼容旧测试 ──
# R1.2.1 后实际阈值由 word_count 参数决定，见 content_quality.py quality_gate()
TARGET_BODY_CHINESE_CHARS = 1200
MIN_EXPORTABLE_BODY_CHINESE_CHARS = 1000
MIN_FALLBACK_BODY_CHINESE_CHARS = 700
MIN_QUALITY_BODY_CHINESE_CHARS = 900
WARNING_BODY_CHINESE_CHARS = 700
MAX_TEXT_GENERATION_CALLS = 3
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


def _length_contract(word_count: int, *, custom_topic: bool = False) -> dict[str, Any]:
    wc = recommended_word_count(word_count)
    if custom_topic:
        rules = {
            1200: (80, 120, 2, 115, 125),
            1500: (90, 130, 2, 140, 155),
            1600: (100, 140, 2, 150, 165),
        }
    else:
        rules = {
            1200: (80, 120, 2, 145, 165),
            1500: (90, 130, 2, 178, 195),
            1600: (100, 140, 2, 190, 205),
        }
    lead_min, lead_max, paragraphs_per_section, para_min, para_max = rules[wc]
    target_max = wc + 200
    return {
        "word_count": wc,
        "target_min": wc,
        "target_max": target_max,
        "target_body_text": f"正文目标 {wc}～{target_max} 个中文汉字（不含标题、小标题、来源、链接、关键词和声明）",
        "lead_text": f"导语：{lead_min}～{lead_max} 个中文汉字",
        "paragraph_rule": f"每个小节必须恰好 {paragraphs_per_section} 个自然段",
        "paragraph_length": f"每段 {para_min}～{para_max} 个中文汉字",
        "self_check": (
            f"输出前自检：全文必须达到 {wc}～{target_max} 个中文汉字；"
            f"必须写满 {paragraphs_per_section} 段/小节，不能把两个自然段合并成一段；"
            f"这里的字数按纯中文汉字计算，不含标点、标题、小标题和链接；每段都要尽量靠近 {para_max} 字上限；"
            f"任一小节少于 {paragraphs_per_section} 段或全文少于 {wc} 字时，继续补充该小节的事实解释和读者价值，不要提前结束。"
        ),
        "paragraphs_per_section": paragraphs_per_section,
        "para_min": para_min,
        "para_max": para_max,
    }


def _prompt_clip(text: str) -> str:
    return text if len(text) <= 6000 else text[:6000]


def _rewrite_min_chars(word_count: int) -> int:
    wc = recommended_word_count(word_count)
    if wc >= 1600:
        return 1400
    if wc >= 1500:
        return 1300
    return 1000


def _rewrite_target_range(word_count: int) -> tuple[int, int]:
    wc = recommended_word_count(word_count)
    if wc >= 1600:
        return 1500, 1750
    if wc >= 1500:
        return 1400, 1650
    return 1100, 1400


def _rewrite_token_budget(word_count: int) -> int:
    wc = recommended_word_count(word_count)
    if wc >= 1600:
        return 3800
    if wc >= 1500:
        return 3400
    return 3200


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
        "ai_statement": "",
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


def _source_block(research_bundle: dict[str, Any] | None) -> str:
    block = _fact_card_block(research_bundle)
    return (
        block
        + "\n单一来源信息可以写入正文，但必须写明来源归属，例如：据XX媒体报道。"
        + "\n不得创造资料中不存在的 fact_id。"
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
    limited_research_mode = bool(bundle.get("hotlist_metadata_available") and str(bundle.get("research_status") or "") == "hotlist_limited")
    structure = "、".join(str(item) for item in angle.get("structure", []))
    must_avoid = "、".join(str(item) for item in angle.get("must_avoid", []))
    length_rule = _length_contract(word_count, custom_topic=custom_topic_mode)
    target_body_text = str(length_rule["target_body_text"])

    # ── R1.2.1 话题分类分流 ──
    from generation.topic_classifier import classify_topic
    classification = classify_topic(
        title=str(topic.title or ""),
        category_label=str(topic.category or ""),
        summary=str(topic.summary or ""),
    )
    
    if custom_topic_mode:
        title_text = str(topic.title or "自定义话题").strip()
        summary = str(topic.summary or "").strip()
        prompt = f"""请为下面的手动话题生成一篇可直接编辑的中文方法型文章。直接输出标准 Markdown 正文，不要输出 JSON，不要输出代码围栏。

话题：{title_text}
用户补充说明：{summary or '无'}
文章类型：{article_type}
表达风格：{style}
{target_body_text}

文章结构（必须严格遵循，不得跳过任何小节）：
# 重新拟定标题（不能直接复制话题名）
{length_rule["lead_text"]}，说清本文能解决什么问题
## 核心概念
正文：概念解释 + 适用场景 + 为什么普通人需要了解
## 可执行方法
正文：具体路径 + 实操案例 + 需要什么条件
## 具体步骤
正文：分步说明 + 每步交付标准 + 时间预估
## 风险提醒
正文：投入控制 + 合规边界 + 常见踩坑
## 总结
正文：闭环总结 + 下一步建议

要求：
1. 每个小节必须包含：概念解释 + 可执行路径 + 读者行动指引。
2. {target_body_text}。
3. {length_rule["paragraph_rule"]}，{length_rule["paragraph_length"]}。
4. 必须包含具体案例、实际场景和方法细节，不得只写空洞模板。
5. 不得出现"事件发生了什么""热榜""权威信息确认""引发关注"等新闻模板词。
6. 直接输出标准 Markdown，不要 JSON、不要代码围栏、不要解释文字。
7. 成品正文不要写资料来源、参考链接、AI声明、生成声明或免责声明。
8. 不虚构数据或人名，不承诺无法验证的收益。
9. {length_rule["self_check"]}"""
        normalized_prompt = prompt.strip()
        return _prompt_clip(normalized_prompt)

    limited_notice = ""
    if limited_research_mode:
        limited_notice = """注意：当前仅获取到热榜标题和有限元数据，没有全文资料。
你的任务是写一篇"传播核验分析稿"——不是重复热榜标题，也不是写免责声明。

必须包含以下内容（缺一不可）：
1. 热榜事实：这件"事"在热榜上以什么标题、在哪个平台上传播
2. 已知和未知边界：明确说清哪些已经确认、哪些只是热榜标题、哪些没有信息
3. 为什么这个标题容易传播：从情绪、共鸣、悬念等角度分析
4. 可能造成什么误读：指出标题可能被怎么误解
5. 普通读者怎么核验：给3条具体的核验路径（搜什么关键词、看哪些权威渠道、怎么辨别真假）
6. 后续看哪些权威渠道：列出2-3类应该关注的权威信息源
7. 对平台/媒体传播的提醒：对传播者而非对当事人的提醒

禁止：
- 全文反复说"无法确认""仍在核实"
- 只列缺失信息
- 写成免责声明
- 没有观点和判断路径
- 每段都"目前公开信息有限"
"""

    # ── R1.2.1 分类只作为写作节奏，不再把内部/半内部标签暴露给正文小标题 ──
    classification_heading_lines = "\n".join(f"   - {heading}" for heading in REQUIRED_SECTION_HEADINGS)
    
    prompt = f"""你是一篇中文热点文章的撰稿人。请根据以下资料，生成一篇可直接发布的中文热点稿。你不是摘要工具——必须重新构思标题、导语和段落顺序，形成独立新结构。

热点标题：{topic.title}
热点分类：{topic.category}（自动判定为{classification['category_name']}类）
热点摘要：{topic.summary or ''}
来源链接：{topic.url or ''}
{_fact_card_block(research_bundle)}
{limited_notice}
创作角度：{angle['name']}（{angle['instruction']}）
角度核心问题：{angle.get('core_question', '')}
开篇策略：{angle.get('opening_strategy', '')}
写作节奏参考：{structure}
必须避免：{must_avoid}
文章类型：{article_type}
表达风格：{style}
{target_body_text}

文章结构（必须严格遵循，这是硬性要求）：
1. 标题：必须重新生成，不直接复制上述"热点标题"
2. {length_rule["lead_text"]}
3. 二级标题：根据创作角度和事实卡动态生成 3～5 个读者可直接理解的小标题，不得使用“事件概览/已确认信息/背景信息/可能影响/后续关注”这套固定栏目；以下只是节奏参考，不得照抄：
{classification_heading_lines}
4. {length_rule["paragraph_rule"]}
5. {length_rule["paragraph_length"]}
6. 段落之间空行

正文质量硬要求：
- 只依据事实卡、资料边界和少量清洗后的引用写作；原始网页全文只能用于核验，不得整段复制进正文。
- “写作节奏参考”和括号里的“钩子开头、30秒速览、单点深挖、观点判断、结尾互动”等词是内部写作提示，绝不能原样作为小标题输出。
- 小标题必须是读者能直接理解的内容标题，不能像提纲标签、模板标签或创作说明。
- 每个小节必须包含：事实信息（发生了什么）+ 解释说明（为什么）+ 读者关心点（关我什么事）
- 同一核心事实最多允许在导语概括一次、正文展开一次，不能换词重复填充字数。
- 禁止出现网页模板和预览污染：{{、}}、dynamicData、subjectData、item.reporter_name、item.tag、未发布文章、仅支持15分钟预览、打开新闻客户端、阅读体验更佳。
- 禁止连续使用以下套话（全文同一条不超过 2 次）："从现有信息看""值得关注""引发关注""具有重要意义""仍需等待""后续仍需""尚未确认""仍待核实""公开信息有限"
- 禁止写成公告摘要
- 禁止写成多个来源的摘要拼接
- 禁止复述来源标题
- 禁止空泛评价，如"具有重要意义""值得深思"，除非紧接着说明具体原因
- 分析必须克制，不得虚构新事实、处罚金额、人数、伤亡、官方结论
- {target_body_text}
- {length_rule["self_check"]}

输出格式：直接输出标准 Markdown 正文，不要 JSON、不要代码围栏、不要解释文字；不要输出资料来源、参考链接、AI声明、生成声明或免责声明。"""
    if rewrite_context:
        conflict = rewrite_context.get("conflict_article") or {}
        rewrite_reason = str(rewrite_context.get("reason") or "需要按 HF4.1 规则重写")
        old_title = str(conflict.get("title") or "未提供旧标题")
        old_opening = str(conflict.get("opening") or "未提供旧导语")
        old_headings = "；".join(str(item) for item in conflict.get("headings") or []) or "未提供旧结构"
        violations = "；".join(str(item) for item in rewrite_context.get("violations") or []) or "未提供冲突类型"
        avoid_expressions = "；".join(str(item) for item in rewrite_context.get("avoid_expressions") or []) or "未提供需避让表达"
        prompt += f"""

重写补充要求：
- 重写原因：{rewrite_reason}
- 冲突类型：{violations}
- 旧标题：{old_title}
- 旧导语：{old_opening}
- 旧结构：{old_headings}
- 需避让表达：{avoid_expressions}
- 不得复用旧标题结构，不得沿用旧开头句式。
- 只允许保留事实，不得复用旧稿表达和段落顺序；必须更换核心论述、调整段落组织和导语切入。
- 第二次调用只能执行这一次重写，完成后不得再次调用模型。
"""
    normalized_prompt = prompt.strip()
    return _prompt_clip(normalized_prompt)



def _clean_article_title(value: Any, fallback: str = "") -> str:
    title = str(value or "").strip()
    title = re.sub(r"^#{1,6}\s*", "", title).strip()
    title = re.sub(r"^(?:标题|新标题|文章标题)\s*[:：]\s*", "", title).strip()
    title = title.strip(" \t\r\n\"'“”‘’《》")
    return title or str(fallback or "").strip()


def _clean_article_lead(value: Any, title: str = "") -> str:
    lead = str(value or "").strip()
    lead = re.sub(r"^#{1,6}\s*", "", lead).strip()
    lead = re.sub(r"^(?:导语|摘要|引言)\s*[:：]\s*", "", lead).strip()
    lead = lead.strip(" \t\r\n\"'“”‘’")
    if title and _normalized_text(lead) == _normalized_text(title):
        return ""
    return lead


def _normalized_text(value: Any) -> str:
    text = re.sub(r"\s+", "", str(value or ""))
    text = re.sub(r"^#{1,6}", "", text)
    text = re.sub(r"^(?:标题|新标题|文章标题|导语|摘要|引言)[:：]", "", text)
    return text.strip("：:。！？!?\"'“”‘’《》")


def _same_block(left: Any, right: Any) -> bool:
    a = _normalized_text(left)
    b = _normalized_text(right)
    return bool(a and b and a == b)


def _body_markdown_from_sections(article: dict[str, Any]) -> str:
    parts: list[str] = []
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
    return "\n\n".join(part for part in parts if part).strip()


def _append_sections_to_markdown(article: dict[str, Any]) -> str:
    title = str(article.get("title") or "").strip()
    lead = str(article.get("lead") or article.get("intro") or "").strip()
    body = str(article.get("body_markdown") or _body_markdown_from_sections(article)).strip()
    parts: list[str] = []
    if title:
        parts.append(f"# {title}")
    if lead:
        parts.append(lead)
    if body:
        parts.append(body)
    return "\n\n".join(part for part in parts if part).strip()


def _sync_article_markdown_fields(article: dict[str, Any]) -> dict[str, Any]:
    result = dict(article)
    title = _clean_article_title(result.get("title"), result.get("topic_title") or "")
    lead = _clean_article_lead(result.get("lead") or result.get("intro") or "", title)
    cleaned_sections: list[dict[str, Any]] = []
    for section in result.get("sections") or []:
        if not isinstance(section, dict):
            continue
        item = dict(section)
        heading = str(item.get("heading") or "").strip()
        body_blocks = [block.strip() for block in re.split(r"\n\s*\n+", str(item.get("body") or "")) if block.strip()]
        filtered: list[str] = []
        for block in body_blocks:
            if _same_block(block, title) or (lead and _same_block(block, lead)):
                continue
            cleaned_block = block
            if title and cleaned_block.startswith(title):
                cleaned_block = cleaned_block[len(title):].strip()
            if lead and cleaned_block.startswith(lead):
                cleaned_block = cleaned_block[len(lead):].strip()
            cleaned_block = cleaned_block.lstrip("，,。:：；;、 \t")
            if cleaned_block:
                filtered.append(cleaned_block)
        item["heading"] = heading
        item["body"] = "\n\n".join(filtered).strip()
        if item["body"]:
            cleaned_sections.append(item)
    result["title"] = title
    result["lead"] = lead
    result["intro"] = lead
    result["summary"] = lead or str(result.get("summary") or "").strip()
    result["sections"] = cleaned_sections
    result["body_markdown"] = _body_markdown_from_sections(result)
    result["content_markdown"] = _append_sections_to_markdown(result)
    return result


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
    # ── R1.2.1 动态分类标题：用关键词匹配或通用兜底 ──
    if heading in templates:
        return templates[heading]
    # 通用兜底：基于heading中的关键词生成
    if "细节" in heading or "画面" in heading:
        return f"围绕“{topic.title}”，目前可通过标题和热榜元数据了解事件轮廓。在缺少更完整资料前，本文基于有限信息还原事件框架，读者应以权威渠道发布的确认信息为准。"
    if "还原" in heading or "经过" in heading:
        return f"事件经过目前以热榜标题为主要线索。完整时间线需等待权威机构或当事方提供更系统的信息发布。本文先根据已有元数据整理可确认的部分，后续根据新信息补充。"
    if "追问" in heading or "反思" in heading:
        return "这一事件引发公众讨论，背后涉及制度、社会、个人层面的多重因素。在缺乏完整权威信息的情况下，可先列出值得追问的方向，而非急于给出结论。"
    if "参照" in heading or "案例" in heading:
        return "类似事件在过去曾有发生，分析过去的处置方式可帮助读者理解当前事件可能的发展路径和关注点。"
    if "启示" in heading or "读者" in heading:
        return "对普通人而言，这一事件至少提醒我们：关注权威信息源、不轻信单方面说法、保留判断直到多方确认。具体到本事件，读者可以关注以下几个关键信息节点。"
    if "钩子" in heading or "悬念" in heading:
        return f"“{topic.title}”——这个标题本身就是一个钩子。它为什么能抓住注意力？背后是否有比标题更复杂的故事？本文尝试梳理。"
    if "速览" in heading or "要点" in heading:
        return f"关于“{topic.title}”，目前可以确认的几个关键信息如下。读者应先掌握这些已知事实，再判断后续信息的真伪和完整度。"
    if "深挖" in heading or "看点" in heading:
        return "在这个话题中，有一个最值得聚焦的核心问题——这也是它进入热榜的深层原因。"
    if "观点" in heading or "判断" in heading:
        return "基于现有信息，本文将给出一个明确的判断立场。在没有足够权威信息支撑之前，判断将保持审慎，但不会回避基本的价值选择。"
    if "互动" in heading:
        return f"关于“{topic.title}”，你怎么看？你看到的其他消息是否与本文有出入？欢迎在评论区留下你的信息来源和判断。"
    if "导语" in heading or "利益" in heading:
        return f"这件事之所以值得你花时间看，是因为它可能影响你的判断、生活或决策。本文先说明和你的关系，再展开分析。"
    if "全貌" in heading:
        return "事件的全貌包括：起因、经过、关键节点、涉事各方立场。在信息不完整时，本文先梳理已有框架，标出信息缺口。"
    if "背景" in heading:
        return "要理解这一事件，需要了解其背后的专业概念和行业背景。本文用通俗语言解释关键术语，帮读者建立理解框架。"
    if "分析" in heading and ("层" in heading or "影响" in heading):
        return "从短期看、中期看、长期看三个维度，这一事件可能带来不同层级的连锁反应。本文不会过度推断，但会点出最值得关注的几个方向。"
    if "影响" in heading and "判断" in heading:
        return "事件的影响不会均匀分布——有人受益，有人受损，有人无感。本文尝试区分不同群体的得失。"
    # 终极兜底
    return f"针对“{heading}”这一维度，目前可确认的信息如下。在缺少更多权威来源前，分析保持谨慎，不将推测等同于事实。"


def _ensure_sentence_end(text: str) -> str:
    value = str(text or "").strip()
    if value and value[-1] not in "。！？!?":
        value += "。"
    return value


def _polish_article_delivery(
    article: dict[str, Any],
    topic: HotTopic,
    required_headings: tuple[str, ...],
    requested_word_count: int,
) -> dict[str, Any]:
    result = dict(article)
    intro = _ensure_sentence_end(str(result.get("intro") or ""))
    result["intro"] = intro
    sections: list[dict[str, Any]] = []
    for index, section in enumerate(result.get("sections") or []):
        if not isinstance(section, dict):
            continue
        item = dict(section)
        heading = str(item.get("heading") or (required_headings[index] if index < len(required_headings) else "核心信息")).strip()
        body = _ensure_sentence_end(str(item.get("body") or ""))
        item["heading"] = heading
        item["body"] = "\n\n".join(_split_dense_paragraph(_ensure_sentence_end(body), target=190))
        sections.append(item)
    result["sections"] = sections
    return _sync_article_markdown_fields(result)


INTERNAL_HEADING_REPLACEMENTS = {
    "钩子开头": "为什么这个话题会被点开",
    "30秒速览": "先看清楚已经发生了什么",
    "单点深化": "最值得追问的一个细节",
    "观点判断": "这件事该怎么看",
    "结尾互动": "留给读者的判断题",
    "利益导语": "这件事和普通人有什么关系",
    "三层分析": "短期、中期和长期影响",
}


def _public_section_heading(label: str, description: str = "") -> str:
    raw = str(label or "").strip()
    if raw in INTERNAL_HEADING_REPLACEMENTS:
        return INTERNAL_HEADING_REPLACEMENTS[raw]
    if raw.endswith("开头") or raw.endswith("导语"):
        return "为什么这件事值得先看"
    if raw.endswith("速览"):
        return "先看清楚已经发生了什么"
    if raw.endswith("互动"):
        return "留给读者的判断题"
    if raw in {"单点深挖", "单点深化"}:
        return "最值得追问的一个细节"
    return raw or str(description or "核心信息").strip() or "核心信息"


def _internal_structure_labels(angle: dict[str, Any], topic: HotTopic | None = None) -> set[str]:
    labels = {str(item).strip() for item in angle.get("structure") or [] if str(item).strip()}
    try:
        from generation.topic_classifier import classify_topic

        # The classifier labels are writing-rhythm hints, not reader-facing H2s.
        if topic is None:
            classification = classify_topic("", "", "")
        else:
            classification = classify_topic(str(topic.title or ""), str(topic.category or ""), str(topic.summary or ""))
        labels.update(str(label).strip() for label, _ in classification["structure"] if str(label).strip())
    except Exception:
        pass
    return labels


def _complete_article_structure(article: dict[str, Any], topic: HotTopic, angle: dict[str, str], required_headings: tuple[str, ...] | None = None) -> dict[str, Any]:
    result = dict(article)
    headings = required_headings or REQUIRED_SECTION_HEADINGS
    internal_labels = _internal_structure_labels(angle, topic)
    title = _clean_article_title(result.get("title") or "", "")
    original_title = str(getattr(topic, "title", "") or "").strip()
    angle_name = str(angle.get("name") or "热点解读").strip()
    if not title or (title == original_title and not result.get("title_from_topic")):
        result["title"] = f"{original_title}：从{angle_name}看后续影响" if original_title else f"{angle_name}文章"
    else:
        result["title"] = title

    intro = _clean_article_lead(result.get("lead") or result.get("intro") or "", result.get("title") or "")
    if not intro:
        result["content_warning_code"] = str(result.get("content_warning_code") or "LEAD_MISSING")
        result["warning_note"] = str(result.get("warning_note") or "模型未返回独立导语，请人工核对文章开头。")
    result["intro"] = intro
    result["lead"] = intro
    result["summary"] = intro or str(result.get("summary") or "").strip()

    raw_sections = [section for section in (result.get("sections") or []) if isinstance(section, dict) and str(section.get("body") or "").strip()]
    if headings == REQUIRED_SECTION_HEADINGS and 3 <= len(raw_sections) <= 5:
        dynamic_sections: list[dict[str, Any]] = []
        for index, section in enumerate(raw_sections[:5], start=1):
            heading = _public_section_heading(str(section.get("heading") or "").strip(), f"正文 {index}")
            if heading in {"事件概览", "已确认信息", "背景信息", "可能影响", "后续关注"}:
                heading = f"关键进展 {index}"
            body = str(section.get("body") or "").strip()
            dynamic_sections.append(
                {
                    **section,
                    "heading": heading,
                    "body": "\n\n".join(_split_dense_paragraph(body, target=180)),
                    "image_brief": str(section.get("image_brief") or f"{heading}相关的真实新闻场景，无文字").strip(),
                }
            )
        result["sections"] = dynamic_sections
        sources = normalize_source_list(result.get("source_list") or [])
        if not sources:
            sources = _topic_source_list(topic)
        result["source_list"] = sources
        result["source_statement"] = "\n\n".join(sources)
        result["ai_statement"] = ""
        return _sync_article_markdown_fields(result)
    merged_by_heading: dict[str, dict[str, Any]] = {}
    unused: list[dict[str, Any]] = []
    for section in raw_sections:
        heading = str(section.get("heading") or "").strip()
        if heading in internal_labels:
            heading = _public_section_heading(heading)
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
                "heading": _public_section_heading(heading) if heading in internal_labels else heading,
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
    result["ai_statement"] = ""
    return _sync_article_markdown_fields(result)

def _init_generation_stats(generation_stats: dict[str, Any] | None) -> dict[str, Any]:
    stats = generation_stats if isinstance(generation_stats, dict) else {}
    stats.setdefault("text_generation_calls", int(stats.get("text_generation_calls") or 0))
    stats.setdefault("text_generation_limit", MAX_TEXT_GENERATION_CALLS)
    stats.setdefault("text_generation_second_call_reason", str(stats.get("text_generation_second_call_reason") or ""))
    stats.setdefault("text_generation_call_reasons", list(stats.get("text_generation_call_reasons") or []))
    return stats


def _register_text_generation_call(stats: dict[str, Any], reason: str) -> None:
    calls = int(stats.get("text_generation_calls") or 0)
    limit = int(stats.get("text_generation_limit") or MAX_TEXT_GENERATION_CALLS)
    if calls >= limit:
        raise ProviderError("TEXT_GENERATION_LIMIT_REACHED", "\u5f53\u524d\u6a21\u5f0f\u4e0b\u5355\u7bc7\u6587\u672c\u6a21\u578b\u8c03\u7528\u5df2\u8fbe\u4e0a\u9650")
    if calls == 0 and reason not in {"full_article", "INITIAL_GENERATION"}:
        raise ProviderError("TEXT_GENERATION_LIMIT_REACHED", "\u9996\u6b21\u8c03\u7528\u5fc5\u987b\u7528\u4e8e\u5b8c\u6574\u6587\u7ae0\u751f\u6210")
    stats["text_generation_calls"] = calls + 1
    reasons = list(stats.get("text_generation_call_reasons") or [])
    reasons.append(str(reason or "rewrite"))
    stats["text_generation_call_reasons"] = reasons
    stats[f"text_generation_call_{calls + 1}_reason"] = str(reason or "rewrite")
    if calls == 0:
        stats["text_generation_second_call_reason"] = ""
    else:
        stats["text_generation_second_call_reason"] = str(reason or "rewrite")


def _attach_generation_stats(article: dict[str, Any], stats: dict[str, Any]) -> dict[str, Any]:
    article["text_generation_calls"] = int(stats.get("text_generation_calls") or 0)
    article["text_generation_limit"] = int(stats.get("text_generation_limit") or MAX_TEXT_GENERATION_CALLS)
    article["text_generation_second_call_reason"] = str(stats.get("text_generation_second_call_reason") or "")
    article["text_generation_call_reasons"] = list(stats.get("text_generation_call_reasons") or [])
    for index, reason in enumerate(article["text_generation_call_reasons"], start=1):
        article[f"text_generation_call_{index}_reason"] = str(reason)
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
            if current_heading and buffer:
                sections.append((current_heading, "\n".join(buffer).strip()))
                buffer = []
            current_heading = re.sub(r"^#{2,3}\s+", "", line).strip()
            continue
        if line.startswith("# "):
            continue
        if not current_heading:
            continue
        buffer.append(raw_line.rstrip())
    if current_heading and buffer:
        sections.append((current_heading, "\n".join(buffer).strip()))
    return [(heading, content.strip()) for heading, content in sections if content.strip()]


def _split_dense_paragraph(text: str, *, target: int = 150) -> list[str]:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if not value:
        return []
    if len(value) <= target:
        return [value]
    sentences = re.split(r"(?<=[。！？!?])", value)
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
    has_markdown_subheadings = bool(re.search(r"^#{2,3}\s+\S+", text, re.M))
    preface = re.split(r"(?m)^#{2,3}\s+\S+.*$", text, maxsplit=1)[0]
    preface_blocks = [
        re.sub(r"^#\s+", "", block.strip()).strip()
        for block in re.split(r"\n\s*\n+", preface)
        if block.strip() and not block.strip().startswith("```")
    ]
    paragraphs = _split_article_paragraphs(text)
    title = str(topic.title).strip()
    title_from_topic = True
    intro = ""
    if preface_blocks and re.match(r"^#\s+", str(preface).lstrip()):
        title = preface_blocks[0] or title
        title_from_topic = False
        intro = preface_blocks[1] if len(preface_blocks) > 1 else ""
    elif preface_blocks and re.match(r"^(?:标题|新标题|文章标题)\s*[:：]", preface_blocks[0]):
        title = preface_blocks[0]
        title_from_topic = False
        intro = preface_blocks[1] if len(preface_blocks) > 1 else ""
    elif has_markdown_subheadings and len(preface_blocks) >= 2:
        title = preface_blocks[0] or title
        title_from_topic = False
        intro = preface_blocks[1]
    elif has_markdown_subheadings and len(preface_blocks) == 1:
        intro = preface_blocks[0]
    else:
        if paragraphs and len(paragraphs[0]) <= 40 and not paragraphs[0].startswith("## "):
            title = paragraphs[0].strip() or title
            title_from_topic = False
            paragraphs = paragraphs[1:]
        if paragraphs and not paragraphs[0].startswith("## "):
            intro = paragraphs[0].strip()
    sections = _split_markdown_sections(text)
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
        "title_from_topic": title_from_topic,
        "intro": intro,
        "lead": intro,
        "sections": cleaned_sections[: max(MIN_SECTIONS, len(cleaned_sections))],
        "summary": intro or topic.summary or "",
        "tags": [topic.category, angle.get("name") or "\u70ed\u70b9\u89e3\u8bfb"],
        "fact_basis": [],
        "demo_mode": False,
        "response_parser_mode": "markdown" if has_markdown_subheadings or text.lstrip().startswith("#") else "text",
        "response_format_warning": False,
        "format_warning": "",
        "fallback_kind": "",
        "used_local_fallback": False,
        "ai_statement": "",
    }
    if not intro:
        article["content_warning_code"] = "LEAD_MISSING"
        article["warning_note"] = "模型未返回独立导语，请人工核对文章开头。"
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
    if "sections" in data and len(cleaned_sections) < MIN_SECTIONS:
        raise ProviderError("MODEL_OUTPUT_INVALID", "模型返回 JSON 包含 sections 但正文结构为空或不足")
    if len(cleaned_sections) < MIN_SECTIONS:
        cleaned_sections = _fallback_sections_from_paragraphs([item["body"] for item in cleaned_sections if item.get("body")])
    cleaned_sections = _normalize_section_bodies(cleaned_sections)
    article = {
        "title": str(data.get("title") or f"{topic.title}：{angle.get('name') or '热点解读'}").strip(),
        "intro": str(data.get("intro") or "").strip(),
        "lead": str(data.get("lead") or data.get("intro") or "").strip(),
        "sections": cleaned_sections,
        "summary": str(data.get("summary") or data.get("intro") or topic.summary or "").strip(),
        "tags": [str(tag).strip() for tag in data.get("tags", []) if str(tag).strip()] if isinstance(data.get("tags"), list) else [],
        "fact_basis": data.get("fact_basis") if isinstance(data.get("fact_basis"), list) else [],
        "risk_note": str(data.get("risk_note") or "").strip(),
        "demo_mode": bool(data.get("demo_mode", False)),
        "response_parser_mode": "json",
        "response_format_warning": False,
        "format_warning": "",
        "fallback_kind": "",
        "used_local_fallback": False,
        "ai_statement": "",
    }
    markdown = str(data.get("content_markdown") or "").strip()
    article["content_markdown"] = markdown or _append_sections_to_markdown(article)
    article = _complete_article_structure(article, topic, angle)
    return article


def _parse_model_article_response(response: str, topic: HotTopic, angle: dict[str, str]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    stripped = _strip_code_fence(response)
    try:
        parsed = parse_json_response(stripped)
    except ProviderError:
        parsed = None
    if parsed is not None:
        return _clean_article(parsed, topic, angle), parsed
    return _parse_markdown_article_response(stripped, topic, angle), None


def _article_has_required_structure(article: dict[str, Any], required_headings: tuple[str, ...]) -> bool:
    if not str(article.get("title") or "").strip():
        return False
    if not str(article.get("lead") or article.get("intro") or "").strip():
        return False
    headings = [str(section.get("heading") or "").strip() for section in article.get("sections") or [] if isinstance(section, dict) and str(section.get("body") or "").strip()]
    return all(any(required == heading or required in heading or heading in required for heading in headings) for required in required_headings)


def _rewrite_short_article_prompt(
    *,
    topic: HotTopic,
    angle: dict[str, str],
    article_type: str,
    style: str,
    requested_word_count: int,
    current_article: dict[str, Any],
    current_body_count: int,
    required_headings: tuple[str, ...],
    research_bundle: dict[str, Any] | None,
) -> str:
    target_min, target_max = _rewrite_target_range(requested_word_count)
    headings = "\n".join(f"## {heading}" for heading in required_headings)
    current_body = str(current_article.get("body_markdown") or _body_markdown_from_sections(current_article)).strip()
    missing = max(0, target_min - int(current_body_count or 0))
    prompt = f"""请把下面这篇偏短的中文文章完整重写为一篇更扎实的成稿。注意：这是完整重写，不是续写，不是在末尾追加，不得复制原文段落顺序。

热点标题：{topic.title}
热点摘要：{topic.summary or ''}
创作角度：{angle.get('name') or '热点解读'}（{angle.get('instruction') or ''}）
文章类型：{article_type}
表达风格：{style}

可用事实卡和资料边界：
{_fact_card_block(research_bundle)}

当前首稿标题：
{current_article.get('title') or ''}

当前首稿导语：
{current_article.get('lead') or current_article.get('intro') or ''}

当前首稿正文：
{current_body}

当前正文可见中文汉字数：{current_body_count}
最低需要补足：{missing}
重写后正文目标：{target_min}～{target_max} 个中文汉字，不含标题、小标题、来源、链接、关键词和声明。

必须输出以下固定结构：
# 重新拟定标题（只出现一次）
导语一段（只出现一次，80～130 个中文汉字）
{headings}

硬性要求：
1. 每个二级标题下至少 2 个自然段，每段有事实、解释和读者价值，不写流水账。
2. 正文从第一个 ## 小标题开始，不要在正文里重复标题或导语。
3. 不要输出资料来源、参考链接、AI声明、生成声明、免责声明。
4. 不要输出 JSON，不要代码围栏，不要解释你怎么写。
5. 不得虚构人物、人数、金额、伤亡、判决、处罚和官方结论。
6. 只依据事实卡、资料边界、首稿中可保留的事实重新组织表达。
7. 如果资料有限，也要写成有判断路径的核验分析稿，不要反复说“信息有限”。"""
    return _prompt_clip(prompt.strip())


def _invalid_output_recovery_prompt(
    *,
    topic: HotTopic,
    angle: dict[str, str],
    article_type: str,
    style: str,
    requested_word_count: int,
    required_headings: tuple[str, ...],
    research_bundle: dict[str, Any] | None,
) -> str:
    target_min, target_max = _rewrite_target_range(requested_word_count)
    headings = "\n".join(f"## {heading}" for heading in required_headings)
    prompt = f"""上一次响应没有形成可用的完整文章。请重新生成完整文章，不要解释失败原因，不要续写残片。

热点标题：{topic.title}
热点摘要：{topic.summary or ''}
创作角度：{angle.get('name') or '热点解读'}（{angle.get('instruction') or ''}）
文章类型：{article_type}
表达风格：{style}

可用事实卡和资料边界：
{_fact_card_block(research_bundle)}

要求：
1. 输出标题一次；
2. 输出导语一次；
3. 正文从二级标题开始；
4. 至少 4 个正文小节；
5. 每个小节至少 2 个自然段；
6. 正文中文汉字目标 {target_min}～{target_max}；
7. 不输出 JSON、代码围栏、资料来源和说明文字；
8. 不得使用固定套话凑字；
9. 只使用事实卡与已提供来源中的事实。

必须输出以下固定结构：
# 重新拟定标题
导语一段
{headings}"""
    return _prompt_clip(prompt.strip())


def _call_reason_used(stats: dict[str, Any], reason: str) -> bool:
    return reason in set(str(item) for item in stats.get("text_generation_call_reasons") or [])


def _apply_short_article_rewrite(
    *,
    provider: OpenAITextProvider,
    topic: HotTopic,
    angle: dict[str, str],
    article_type: str,
    style: str,
    requested_word_count: int,
    article: dict[str, Any],
    body_count: int,
    required_headings: tuple[str, ...],
    research_bundle: dict[str, Any] | None,
    stats: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    min_chars = _rewrite_min_chars(requested_word_count)
    calls = int(stats.get("text_generation_calls") or 0)
    invalid_recovery_used = _call_reason_used(stats, "INVALID_OUTPUT_RECOVERY")
    already_rewrote_short = _call_reason_used(stats, "CONTENT_TOO_SHORT_REWRITE")
    rewrite_allowed = (
        calls == 1
        or (calls == 2 and invalid_recovery_used and not already_rewrote_short)
    )
    if not (
        not bool(article.get("used_local_fallback"))
        and int(body_count or 0) >= 600
        and int(body_count or 0) < min_chars
        and rewrite_allowed
    ):
        return article, dict(provider.last_diagnostic or {})
    _register_text_generation_call(stats, "CONTENT_TOO_SHORT_REWRITE")
    rewrite_prompt = _rewrite_short_article_prompt(
        topic=topic,
        angle=angle,
        article_type=article_type,
        style=style,
        requested_word_count=requested_word_count,
        current_article=article,
        current_body_count=body_count,
        required_headings=required_headings,
        research_bundle=research_bundle,
    )
    response = provider.generate(
        rewrite_prompt,
        temperature=0.55,
        max_tokens=_rewrite_token_budget(requested_word_count),
    )
    diagnostic = dict(provider.last_diagnostic or {})
    try:
        candidate, _parsed = _parse_model_article_response(response, topic, angle)
        candidate = _complete_article_structure(candidate, topic, angle, required_headings)
        candidate = _polish_article_delivery(candidate, topic, required_headings, requested_word_count)
        candidate_count = count_body_chinese_chars(candidate)
    except ProviderError:
        article["content_warning_code"] = "CONTENT_TOO_SHORT"
        article["warning_note"] = "模型第二次重写未形成可用结构，已保留首稿，建议用户主动重新生成。"
        article["review_required"] = True
        return article, diagnostic
    if _article_has_required_structure(candidate, required_headings) and candidate_count > int(body_count or 0):
        candidate["text_generation_second_call_reason"] = "CONTENT_TOO_SHORT_REWRITE"
        if candidate_count < min_chars:
            candidate["content_warning_code"] = "CONTENT_TOO_SHORT"
            candidate["warning_note"] = "模型第二次重写后正文仍偏短，已保留较完整版本，建议用户人工复核或主动重新生成。"
            candidate["review_required"] = True
        return candidate, diagnostic
    article["content_warning_code"] = "CONTENT_TOO_SHORT"
    article["warning_note"] = "模型第二次重写未优于首稿，已保留首稿，建议用户主动重新生成。"
    article["review_required"] = True
    return article, diagnostic


def _quality_issue_rewrite_prompt(
    *,
    topic: HotTopic,
    angle: dict[str, str],
    article_type: str,
    style: str,
    requested_word_count: int,
    current_article: dict[str, Any],
    issue_list: list[str],
    research_bundle: dict[str, Any] | None,
) -> str:
    target_min, target_max = _rewrite_target_range(requested_word_count)
    current_body = str(current_article.get("content_markdown") or "")[:2600]
    issues = "\n".join(f"- {item}" for item in issue_list[:12])
    prompt = f"""请根据质量检查问题重写完整文章。不要解释，不要输出 JSON，不要输出资料来源或调试内容。

热点标题：{topic.title}
创作角度：{angle.get('name') or '热点解读'}（{angle.get('instruction') or ''}）
文章类型：{article_type}
表达风格：{style}

质量问题：
{issues}

可用事实卡和资料边界：
{_fact_card_block(research_bundle)}

当前稿仅供理解问题，不能复制其重复段落或污染文本：
{current_body}

重写要求：
1. 正文中文汉字目标 {target_min}～{target_max}，7～10 个自然段。
2. 使用 3～5 个动态二级标题，不能使用“事件概览/已确认信息/背景信息/可能影响/后续关注”这套固定小标题。
3. 同一核心事实最多在导语概括一次、正文展开一次，其他段落不得换词重复。
4. 不得出现 {{、}}、dynamicData、item.reporter_name、item.tag、未发布文章、打开新闻客户端、阅读体验更佳等网页模板或 App 提示。
5. 不复制完整来源原文，不写“模型异常”“基础稿”“AI声明”。
6. 不得虚构人物、人数、金额、伤亡、处罚、判决和官方结论。

输出结构：
# 标题
导语一段
## 动态小标题
正文自然段"""
    return _prompt_clip(prompt.strip())


def _apply_quality_issue_rewrite(
    *,
    provider: OpenAITextProvider,
    topic: HotTopic,
    angle: dict[str, str],
    article_type: str,
    style: str,
    requested_word_count: int,
    article: dict[str, Any],
    issue_list: list[str],
    required_headings: tuple[str, ...],
    research_bundle: dict[str, Any] | None,
    stats: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    if bool(article.get("used_local_fallback")) or not issue_list:
        return article, dict(provider.last_diagnostic or {})
    if _call_reason_used(stats, "QUALITY_ISSUE_REWRITE"):
        return article, dict(provider.last_diagnostic or {})
    if int(stats.get("text_generation_calls") or 0) >= int(stats.get("text_generation_limit") or MAX_TEXT_GENERATION_CALLS):
        return article, dict(provider.last_diagnostic or {})
    _register_text_generation_call(stats, "QUALITY_ISSUE_REWRITE")
    prompt = _quality_issue_rewrite_prompt(
        topic=topic,
        angle=angle,
        article_type=article_type,
        style=style,
        requested_word_count=requested_word_count,
        current_article=article,
        issue_list=issue_list,
        research_bundle=research_bundle,
    )
    response = provider.generate(prompt, temperature=0.5, max_tokens=_rewrite_token_budget(requested_word_count))
    diagnostic = dict(provider.last_diagnostic or {})
    candidate, _parsed = _parse_model_article_response(response, topic, angle)
    candidate = _complete_article_structure(candidate, topic, angle, required_headings)
    candidate = _polish_article_delivery(candidate, topic, required_headings, requested_word_count)
    candidate["text_generation_second_call_reason"] = "QUALITY_ISSUE_REWRITE"
    return candidate, diagnostic

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
    call_reason = "INITIAL_GENERATION" if int(stats.get("text_generation_calls") or 0) == 0 else str((rewrite_context or {}).get("reason_code") or "rewrite")
    _register_text_generation_call(stats, call_reason)
    
    # ── R1.2.1 动态 token_budget 映射 ──
    if requested_word_count >= 1600:
        token_budget = 3200
    elif requested_word_count >= 1500:
        token_budget = 2800
    else:
        token_budget = 2200
    response = provider.generate(
        generation_prompt,
        temperature=0.6,
        max_tokens=token_budget,
    )
    required_headings = (
        CUSTOM_TOPIC_SECTION_HEADINGS
        if bool(
            (research_bundle or {}).get("custom_topic")
            and str((research_bundle or {}).get("research_status") or "") == "custom_topic"
        )
        else REQUIRED_SECTION_HEADINGS
    )
    provider_diagnostic = dict(provider.last_diagnostic or {})
    try:
        article, parsed = _parse_model_article_response(response, topic, angle)
    except ProviderError as exc:
        if exc.code not in {"ARTICLE_PARSE_ERROR", "MODEL_OUTPUT_INVALID", "MODEL_OUTPUT_EMPTY", "INVALID_RESPONSE"}:
            raise
        _register_text_generation_call(stats, "INVALID_OUTPUT_RECOVERY")
        recovery_prompt = _invalid_output_recovery_prompt(
            topic=topic,
            angle=angle,
            article_type=article_type,
            style=style,
            requested_word_count=requested_word_count,
            required_headings=required_headings,
            research_bundle=research_bundle,
        )
        response = provider.generate(
            recovery_prompt,
            temperature=0.55,
            max_tokens=_rewrite_token_budget(requested_word_count),
        )
        provider_diagnostic = dict(provider.last_diagnostic or {})
        article, parsed = _parse_model_article_response(response, topic, angle)
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
        source_lines = []
        article["source_list"] = normalize_source_list(article.get("source_list") or [])
    article["source_statement"] = "\uFF1B".join(str(item) for item in article.get("source_list") or [])
    article["ai_statement"] = ""
    article = _complete_article_structure(article, topic, angle, required_headings)
    article = _polish_article_delivery(article, topic, required_headings, requested_word_count)
    article["body_char_count"] = count_body_chinese_chars(article)
    article, provider_diagnostic = _apply_short_article_rewrite(
        provider=provider,
        topic=topic,
        angle=angle,
        article_type=article_type,
        style=style,
        requested_word_count=requested_word_count,
        article=article,
        body_count=int(article.get("body_char_count") or 0),
        required_headings=required_headings,
        research_bundle=research_bundle,
        stats=stats,
    )
    if research_bundle:
        article["source_list"] = source_lines
    else:
        article["source_list"] = normalize_source_list(article.get("source_list") or [])
    article["source_statement"] = "\uFF1B".join(str(item) for item in article.get("source_list") or [])
    article["ai_statement"] = ""
    article["text_http_status"] = provider_diagnostic.get("http_status")
    article["text_content_type"] = provider_diagnostic.get("content_type") or ""
    article["provider_parser_mode"] = provider_diagnostic.get("parser_mode") or ""
    article["request_timeout_seconds"] = provider_diagnostic.get("timeout_seconds")
    article["word_count"] = requested_word_count
    article["body_char_count"] = count_body_chinese_chars(article)
    fallback_complete = bool(article.get("fallback_complete"))
    
    # ── R1.2.1 动态推荐状态阈值 ──
    if requested_word_count >= 1600:
        _target_chars = 1600
        _warn_chars = 1400
        _exportable_chars = 700
    elif requested_word_count >= 1500:
        _target_chars = 1500
        _warn_chars = 1300
        _exportable_chars = 700
    else:
        _target_chars = 1200
        _warn_chars = 1000
        _exportable_chars = 700
    
    article["used_local_fallback"] = bool(article.get("used_local_fallback", False))
    article["fallback_kind"] = str(article.get("fallback_kind") or "")
    article["response_parser_mode"] = str(article.get("response_parser_mode") or ("json" if parsed is not None else "markdown"))
    article["response_format_warning"] = bool(article.get("response_format_warning", False))

    if article["body_char_count"] >= _target_chars:
        article["recommended_status"] = "completed"
    elif article["body_char_count"] >= _warn_chars:
        article["recommended_status"] = "warning"
    elif article["body_char_count"] >= _exportable_chars:
        article["recommended_status"] = "review_required"
    else:
        article["recommended_status"] = "review_required"
        article["content_warning_code"] = "CONTENT_TOO_SHORT"
        article["warning_note"] = "模型返回正文偏短，已保留原始可编辑正文，建议用户主动重新生成。"
    return _attach_generation_stats(article, stats)


def plan_for_topic(count: int, selected_ids: list[str] | None = None) -> list[dict[str, Any]]:
    return plan_angles(min(max(count, 1), 5), selected_ids)





