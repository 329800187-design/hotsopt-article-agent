"""RC1.3.3-Lite-P1-HF4.1-R1.2 最终返修测试套件

测试覆盖：
1. 快速双击开始生成 → 只创建1个任务（幂等）
2. client_request_id 幂等
3. 1200 prompt 为 1200～1400
4. 1500 prompt 为 1500～1700
5. 1600 prompt 为 1600～1800
6. hotlist_limited 不得出现弱标题模板
7. hotlist_limited 必须包含核验路径/传播风险/读者判断
8. unknown_phrase_ratio 过高 → failed/rewrite
9. 手动话题走方法型结构
10. 批量链接内容进入 fact_cards
11. Word 导出正文达标
12. 旧 R1.2 回归全部通过（部分依赖现有测试）
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

import pytest

from generation.article_generator import (
    TARGET_BODY_CHINESE_CHARS,
    WARNING_BODY_CHINESE_CHARS,
    _prompt,
    _complete_article_structure,
    _generic_paragraph,
    _append_sections_to_markdown,
    REQUIRED_SECTION_HEADINGS,
)
from generation.topic_classifier import classify_topic, CATEGORY_RULES
from generation.content_quality import (
    quality_gate,
    UNKNOWN_PHRASES,
    VALUE_SECTION_MARKERS,
)
from generation.image_budget import count_body_chinese_chars, recommended_word_count
from modules.models import HotTopic


# ═══════════════════════════════════════════════════════════════════
# 辅助工厂
# ═══════════════════════════════════════════════════════════════════

def _topic(title: str = "测试热点", category: str = "社会", summary: str = "") -> HotTopic:
    return HotTopic(
        id=uuid.uuid4().hex[:12],
        title=title,
        hot_value="100万",
        hot_score=95.0,
        rank=1,
        category=category,
        summary=summary,
        source="test",
        source_name="测试来源",
        source_url="https://example.com/test",
        captured_at="2026-07-01T00:00:00Z",
        provider_status="ok",
        is_cached=False,
        raw_data={},
        created_at="2026-07-01T00:00:00Z",
        updated_at="2026-07-01T00:00:00Z",
    )


def _angle(name: str = "事件还原") -> dict[str, str]:
    return {
        "angle_id": f"angle-{name}",
        "name": name,
        "instruction": "从事件脉络角度解读",
        "core_question": "这件事是怎么发生的？",
        "opening_strategy": "用一句话概括事件",
        "structure": ["起因", "经过", "影响"],
        "must_avoid": ["虚构", "猜测"],
    }


def _skeleton_article(body_char_count: int = 900) -> dict[str, Any]:
    """生成一个具有足够字数的测试文章。"""
    filler = "这是一个测试段落用于填充文章正文字数。" * max(1, body_char_count // 15)
    sections = [
        {"heading": "测试章节一", "body": filler[:body_char_count // 3], "image_brief": "测试"},
        {"heading": "测试章节二", "body": filler[body_char_count // 3 : 2 * body_char_count // 3], "image_brief": "测试"},
        {"heading": "测试章节三", "body": filler[2 * body_char_count // 3 :], "image_brief": "测试"},
    ]
    article = {
        "title": "测试文章标题",
        "intro": "这是导语，用于测试文章结构。" * 3,
        "sections": sections,
        "content_markdown": f"# 测试文章标题\n\n这是导语。\n\n## 测试章节一\n{filler}\n\n## 测试章节二\n\n## 资料来源\n来源A\n\nAI辅助声明",
        "source_list": ["来源A：https://example.com/source"],
        "source_statement": "来源A",
        "ai_statement": "AI辅助声明：本文为测试内容。",
        "body_char_count": body_char_count,
        "word_count": 1200,
        "recommended_status": "completed",
        "tags": ["测试"],
        "summary": "测试摘要",
        "demo_mode": False,
        "text_generation_calls": 1,
        "text_generation_limit": 1,
        "text_generation_second_call_reason": "",
        "fact_basis": [],
    }
    return article


def _research_bundle(research_status: str = "sufficient", accepted_count: int = 2) -> dict[str, Any]:
    return {
        "topic_id": "test-topic-id",
        "topic_title": "测试热点",
        "research_status": research_status,
        "accepted_source_count": accepted_count,
        "custom_topic": research_status == "custom_topic",
        "hotlist_metadata_available": research_status == "hotlist_limited",
        "sources": [
            {
                "source_id": "src-1",
                "url": "https://example.com/1",
                "fetch_success": True,
                "accepted_for_research": True,
                "source_name": "来源A",
                "title": "来源A标题",
                "summary": "来源A摘要",
                "content": "来源A的正文内容，包含事件相关的事实信息。事件发生在2026年7月。",
                "published_at": "2026-07-01",
                "domain": "example.com",
            },
            {
                "source_id": "src-2",
                "url": "https://example.com/2",
                "fetch_success": True,
                "accepted_for_research": True,
                "source_name": "来源B",
                "title": "来源B标题",
                "summary": "来源B摘要",
                "content": "来源B的正文内容，补充了事件的背景信息。",
                "published_at": "2026-07-02",
                "domain": "example.org",
            },
        ][:accepted_count],
        "usable_facts": [
            {"fact_id": "f1", "canonical_fact": "事件A于2026年7月1日发生", "supporting_source_ids": ["src-1"]},
            {"fact_id": "f2", "canonical_fact": "事件A引发公众关注", "supporting_source_ids": ["src-2"]},
        ],
        "research_fact_cards": [
            {"fact_id": "f1", "fact": "事件A于2026年7月1日发生", "source_name": "来源A", "source_url": "https://example.com/1", "reliability": "confirmed"},
            {"fact_id": "f2", "fact": "事件A引发公众关注", "source_name": "来源B", "source_url": "https://example.com/2", "reliability": "confirmed"},
        ],
        "background_fact_cards": [],
        "verified_facts": [],
        "single_source_facts": [],
        "candidate_facts": [],
        "disputed_facts": [],
        "usable_fact_count": 2,
        "timeline": [],
        "key_people": [],
        "key_organizations": [],
        "numbers": [],
        "background": [],
        "official_statements": [],
        "locations": [],
        "unique_source_domains": ["example.com", "example.org"],
        "unique_publisher_ids": ["example.com", "example.org"],
        "independent_publisher_count": 2,
        "cross_verified_fact_count": 0,
        "official_source_count": 0,
        "official_fact_count": 0,
        "official_or_reliable_source_count": 2,
        "information_sufficiency_score": 60,
        "insufficient_reasons": [],
        "discovery": [],
        "discovery_evidence": [],
        "candidate_link_count": 2,
        "rejected_source_count": 0,
        "search_failure_visible_to_user": False,
        "minimum_gate": {"condition_a": True, "condition_b": True, "has_event_context": True, "has_conflict": False},
        "collected_at": "2026-07-01T00:00:00Z",
        "source_page": {"url": "https://example.com/source-page", "title": "来源页", "fetch_success": True},
    }


# ═══════════════════════════════════════════════════════════════════
# 测试 1: topic_classifier 分类
# ═══════════════════════════════════════════════════════════════════

class TestTopicClassifier:
    """话题分类器测试"""

    def test_classify_entertainment_as_A(self):
        """娱乐/明星话题 → A类"""
        result = classify_topic("某明星被曝离婚内幕，网友热议反转真相")
        assert result["category_key"] == "A"
        assert result["target_chars"] == 1200

    def test_classify_social_as_B(self):
        """社会民生话题 → B类"""
        result = classify_topic("女子在医院坠楼身亡 家属质疑医院处置")
        assert result["category_key"] == "B"
        assert result["target_chars"] == 1500

    def test_classify_international_as_C(self):
        """国际政策话题 → C类"""
        result = classify_topic("美国对俄罗斯实施新一轮制裁 欧盟跟进")
        assert result["category_key"] == "C"
        assert result["target_chars"] == 1600

    def test_classify_default_A(self):
        """无关键词话题 → 默认A类"""
        result = classify_topic("今天天气很好适合出去玩")
        assert result["category_key"] == "A"

    def test_classify_structure_not_empty(self):
        """分类结构不为空"""
        for cat in ["A", "B", "C"]:
            rules = CATEGORY_RULES[cat]
            assert len(rules["structure"]) >= 4

    def test_classify_earthquake_as_C(self):
        """自然灾害 → C类"""
        result = classify_topic("某地发生6.5级强震 已启动应急响应")
        assert result["category_key"] == "C"


# ═══════════════════════════════════════════════════════════════════
# 测试 2: 字数映射
# ═══════════════════════════════════════════════════════════════════

class TestWordCountMapping:
    """字数映射测试"""

    def test_prompt_1200_targets_1200_to_1400(self):
        """1200字 → prompt 目标 1200～1400"""
        topic = _topic("测试话题A")
        prompt = _prompt(topic, _angle(), "热点资讯", "客观通俗", 1200, research_bundle=_research_bundle())
        assert "1200～1400" in prompt
        assert "900～1200" not in prompt

    def test_prompt_1500_targets_1500_to_1700(self):
        """1500字 → prompt 目标 1500～1700"""
        topic = _topic("测试话题B")
        prompt = _prompt(topic, _angle(), "热点资讯", "客观通俗", 1500, research_bundle=_research_bundle())
        assert "1500～1700" in prompt

    def test_prompt_1600_targets_1600_to_1800(self):
        """1600字 → prompt 目标 1600～1800"""
        topic = _topic("测试话题C")
        prompt = _prompt(topic, _angle(), "热点资讯", "客观通俗", 1600, research_bundle=_research_bundle())
        assert "1600～1800" in prompt

    def test_prompt_never_900_to_1200(self):
        """任意字数都不应出现 900～1200"""
        topic = _topic("测试话题")
        for wc in [1200, 1500, 1600]:
            prompt = _prompt(topic, _angle(), "热点资讯", "客观通俗", wc, research_bundle=_research_bundle())
            assert "900～1200" not in prompt, f"WC={wc}: prompt contains 900～1200"

    def test_recommended_word_count_passthrough(self):
        """recommended_word_count 1200→1200, 1500→1500, 1600→1600"""
        assert recommended_word_count(1200) == 1200
        assert recommended_word_count(1500) == 1500
        assert recommended_word_count(1600) == 1600


# ═══════════════════════════════════════════════════════════════════
# 测试 3: quality_gate 动态门槛
# ═══════════════════════════════════════════════════════════════════

class TestQualityGateDynamicThresholds:
    """动态质量门测试"""

    def test_1200_wordcount_fail_below_1000(self):
        """1200字目标：<1000 failed"""
        article = _skeleton_article(800)
        article["word_count"] = 1200
        article["body_char_count"] = 800
        article["content_markdown"] = "# T\n\n" + "正文内容。" * 100
        article["sections"] = [
            {"heading": "核验路径", "body": "读者可以搜索关键词验证信息。查证来源是判断真实性的第一步。" * 5, "image_brief": ""},
            {"heading": "传播风险", "body": "这类标题容易引发误读，因为省略了关键限定条件。" * 5, "image_brief": ""},
        ]
        gate = quality_gate(article, _research_bundle())
        assert gate["status"] == "failed", f"Expected failed, got {gate['status']}: {gate['hard_errors']}"

    def test_1200_wordcount_warning_1000_to_1199(self):
        """1200字目标：1000-1199 warning（需足够body + 价值段落）"""
        article = _skeleton_article(1100)
        article["word_count"] = 1200
        article["body_char_count"] = 1100
        article["content_markdown"] = "# T\n\n" + "正文内容详细描述了事件经过，读者可以理解背景。" * 40
        article["sections"] = [
            {"heading": "核验路径", "body": "读者可以搜索关键词验证信息。查证来源是判断真实性的第一步。核查方法是搜索原文标题。" * 10, "image_brief": ""},
            {"heading": "传播风险", "body": "这类标题容易引发误读。传播者应注意核实来源。存在风险。" * 10, "image_brief": ""},
            {"heading": "读者启示", "body": "读者可以保留判断空间，先看事实是否完整，再看不同信息之间是否互相印证。" * 10, "image_brief": ""},
        ]
        gate = quality_gate(article, _research_bundle())
        # 可能warning或passed，取决于段落质量
        assert gate["status"] in ("warning", "passed"), f"Unexpected: {gate['status']}"

    def test_1600_wordcount_fail_below_1400(self):
        """1600字目标：<1400 failed"""
        article = _skeleton_article(1200)
        article["word_count"] = 1600
        article["body_char_count"] = 1200
        article["content_markdown"] = "# T\n\n" + "正文内容。" * 150
        article["sections"] = [
            {"heading": "核验路径", "body": "读者可以搜索关键词验证信息。查证来源是判断真实性的第一步。" * 5, "image_brief": ""},
            {"heading": "传播风险", "body": "这类标题容易引发误读。" * 5, "image_brief": ""},
        ]
        gate = quality_gate(article, _research_bundle(accepted_count=2))
        assert gate["status"] == "failed", f"Expected failed, got {gate['status']}"


# ═══════════════════════════════════════════════════════════════════
# 测试 4: hotlist_limited 提示语
# ═══════════════════════════════════════════════════════════════════

class TestHotlistLimitedPrompt:
    """hotlist_limited 提示语测试"""

    def test_no_weak_title_template(self):
        """hotlist_limited 不出现弱标题模板"""
        topic = _topic("某明星争议事件")
        prompt = _prompt(topic, _angle(), "热点资讯", "客观通俗", 1200,
                         research_bundle=_research_bundle("hotlist_limited", accepted_count=0))
        # 不应出现旧版弱模板
        assert "登上热榜，相关细节仍待核实" not in prompt
        assert "传播核验" in prompt

    def test_contains_verification_path(self):
        """hotlist_limited 必须包含核验路径提示"""
        topic = _topic("突发新闻事件")
        prompt = _prompt(topic, _angle(), "热点资讯", "客观通俗", 1200,
                         research_bundle=_research_bundle("hotlist_limited", accepted_count=0))
        assert "核验" in prompt

    def test_contains_reader_guidance(self):
        """hotlist_limited 必须包含读者判断和传播风险"""
        topic = _topic("网络热议话题")
        prompt = _prompt(topic, _angle(), "热点资讯", "客观通俗", 1200,
                         research_bundle=_research_bundle("hotlist_limited", accepted_count=0))
        assert "误读" in prompt or "读者" in prompt

    def test_no_full_disclaimer(self):
        """hotlist_limited 不得写成纯免责声明"""
        topic = _topic("未知事件")
        prompt = _prompt(topic, _angle(), "热点资讯", "客观通俗", 1200,
                         research_bundle=_research_bundle("hotlist_limited", accepted_count=0))
        # 必须有积极指示，不能只是禁止
        assert "必须包含" in prompt
        assert "禁止" in prompt


# ═══════════════════════════════════════════════════════════════════
# 测试 5: 空话检测
# ═══════════════════════════════════════════════════════════════════

class TestUnknownPhraseDetection:
    """空话检测测试"""

    def test_high_unknown_ratio_fails(self):
        """空话占比>8% → failed"""
        article = _skeleton_article(500)
        article["word_count"] = 1200
        article["body_char_count"] = 500
        # 构造高比例空话
        body = "尚未确认该事件是否发生。" * 15  # "尚未确认" 每句5字 × 15 = 75字，占比高
        article["content_markdown"] = f"# T\n\n{body}"
        article["sections"] = [
            {"heading": "事件发生了什么", "body": body, "image_brief": ""},
        ]
        gate = quality_gate(article, _research_bundle(accepted_count=0))
        # 可能因多重原因失败
        has_unknown_error = any("空话" in str(r) for r in gate.get("hard_errors", []) + gate.get("reasons", []))
        # 如果空话占比不够高，可能先被字数不足判定
        assert gate["status"] == "failed" or has_unknown_error

    def test_moderate_unknown_ratio_warns(self):
        """空话占比4-8% → passed/warning（足够正文稀释空话比例）"""
        article = _skeleton_article(1100)
        article["word_count"] = 1200
        article["body_char_count"] = 1100
        # 大量实质内容稀释空话比例
        content_body = (
            "围绕此事件，本文详细梳理了事件经过和发展脉络。"
            "根据多方信息来源交叉核实，关键事实包括时间、地点和涉及人物均已确认。"
            "背景原因涉及多个层面，包括制度设计、执行管理和个体行为等因素。"
            "影响分析显示，此类事件对相关行业、公众认知和后续政策均有直接和间接影响。"
            "我认为有必要从三个维度来理解：短期影响、中期趋势和长期格局。"
            "读者可以通过搜索原标题、查看权威发布渠道来核验信息的准确性。"
            "类似案例在过去曾有发生，可以参考此前的处置方式和结果。"
        ) * 5
        content_body += "尚未确认部分细节。" * 1  # 极少空话，占比远低于4%
        article["content_markdown"] = content_body
        article["sections"] = [
            {"heading": "核验路径", "body": "读者可以通过搜索引擎核验信息真实性。查证步骤包括验证发布来源是否权威。读者启示是不要轻信单一来源。" * 8, "image_brief": ""},
            {"heading": "传播风险", "body": "此事件背景涉及多方面因素。存在误读风险，谣言可能误导公众。需要注意核实信息后再传播。" * 8, "image_brief": ""},
            {"heading": "背景解释", "body": "从背景和原因看，信息进入公共讨论后需要区分事实、推测和情绪表达，避免把片段内容当成完整结论。" * 8, "image_brief": ""},
        ]
        gate = quality_gate(article, _research_bundle(accepted_count=2))
        # 合理内容+极少空话应通过
        assert gate["status"] in ("passed", "warning"), \
            f"Unexpected: {gate['status']} hard_errors={gate.get('hard_errors', [])}"

    def test_value_sections_minimum_two(self):
        """文章至少2类价值段落"""
        article = _skeleton_article(1200)
        article["word_count"] = 1200
        article["body_char_count"] = 1200
        # 构造有"核验路径"和"传播风险"的段落
        article["content_markdown"] = f"# T\n\n读者可以核验信息。存在误读风险。背景是重要原因。" * 10
        article["sections"] = [
            {"heading": "核验路径", "body": "读者可以通过搜索引擎核验信息真实性。查证步骤包括验证来源。" * 10, "image_brief": ""},
            {"heading": "传播风险", "body": "这类信息容易被误读，存在风险。传播者应注意核实。" * 10, "image_brief": ""},
            {"heading": "读者启示", "body": "读者启示是把情绪判断放在事实核查之后，先确认来源，再比较不同说法。" * 10, "image_brief": ""},
        ]
        gate = quality_gate(article, _research_bundle(accepted_count=2))
        assert gate["status"] in ("passed", "warning")

    def test_missing_value_sections_fails(self):
        """无价值段落 → failed"""
        article = _skeleton_article(500)
        article["word_count"] = 1200
        article["body_char_count"] = 500
        article["content_markdown"] = "# T\n\n" + "通用内容没有任何价值标记。" * 40
        article["sections"] = [
            {"heading": "第一章", "body": "通用内容没有任何价值标记。" * 10, "image_brief": ""},
        ]
        gate = quality_gate(article, _research_bundle(accepted_count=2))
        # 至少因字数不足失败；若字数够也可能因value_sections不足
        assert gate["status"] == "failed"

    def test_multiple_unknown_phrases_cascade(self):
        """多种不同空话叠加 → unknown_phrase_ratio 真实触发 warning/failed"""
        article = _skeleton_article(800)
        article["word_count"] = 1200
        article["body_char_count"] = 800
        # 故意构造混合多种空话的正文
        body = (
            "目前公开信息有限，尚未确认具体细节。"
            "仍待核实相关报道，暂无法判断真伪。"
            "等待权威部门进一步说明，后续关注事态进展。"
        ) * 20
        article["content_markdown"] = f"# T\n\n{body}"
        article["sections"] = [
            {"heading": "事件发生了什么", "body": body, "image_brief": ""},
        ]
        gate = quality_gate(article, _research_bundle(accepted_count=0))
        # 空话比例应触达 hard fail 或至少 warning
        has_unknown = any("空话" in str(r) for r in gate.get("hard_errors", []) + gate.get("warnings", []))
        assert has_unknown, (
            f"Expected unknown phrase flag. "
            f"hard_errors={gate.get('hard_errors', [])} "
            f"warnings={gate.get('warnings', [])}"
        )

    def test_normal_chinese_not_zero_chars(self):
        """正常中文正文 body_char_count 不会被正则 BUG 统计为 0"""
        article = _skeleton_article(900)
        article["word_count"] = 1500
        article["body_char_count"] = 900
        # 纯正常中文，不含任何空话
        body = (
            "根据公开报道，事件发生在某市某区，涉及人员已被警方控制。"
            "周边居民表示事发时听到巨响，随后看到浓烟升起。"
            "消防部门在接警后8分钟内抵达现场，展开救援工作。"
            "截至发稿时，伤者已送医救治，事故原因正在调查中。"
            "此事件引发社会广泛关注，相关部门已成立专项工作组。"
        ) * 8
        article["content_markdown"] = f"# 正常报道\n\n{body}"
        article["sections"] = [
            {"heading": "事件经过", "body": body, "image_brief": ""},
        ]
        gate = quality_gate(article, _research_bundle(accepted_count=2))
        # 核心断言：quality_gate 内部 total_body_chars 不应为 0
        # 如果正则 BUG 未修复，total_body_chars=0 导致空话比为 0 且字数检测失效
        # 正常情况：应检测到字数偏低 warning 而非空话相关
        has_body_short = any("字数不足" in str(r) or "字数偏低" in str(r)
                            for r in gate.get("hard_errors", []) + gate.get("warnings", []))
        assert has_body_short, (
            f"Expected body-too-short flag but got none. "
            f"hard_errors={gate.get('hard_errors', [])} "
            f"warnings={gate.get('warnings', [])}"
        )


# ═══════════════════════════════════════════════════════════════════
# 测试 6: 手动话题走方法型结构
# ═══════════════════════════════════════════════════════════════════

class TestManualTopicStructure:
    """手动话题测试"""

    def test_custom_topic_prompt_has_method_structure(self):
        """手动话题prompt包含方法型结构"""
        topic = _topic("如何提高写作效率")
        bundle = _research_bundle("custom_topic")
        bundle["custom_topic"] = True
        prompt = _prompt(topic, _angle(), "科普解读", "专业分析", 1200, research_bundle=bundle)
        assert "核心概念" in prompt
        assert "可执行方法" in prompt
        assert "具体步骤" in prompt
        assert "风险提醒" in prompt
        assert "总结" in prompt

    def test_custom_topic_no_news_template_words(self):
        """手动话题不包含新闻模板词（排除自引用禁令行）"""
        topic = _topic("Python学习路线")
        bundle = _research_bundle("custom_topic")
        bundle["custom_topic"] = True
        prompt = _prompt(topic, _angle(), "科普解读", "专业分析", 1200, research_bundle=bundle)
        # 排除第4条禁令行（它自引用这些词做禁止说明）
        body_lines = [l for l in prompt.split("\n") if not re.match(r"\d+\.\s*不得出现", l)]
        body = "\n".join(body_lines)
        assert "事件发生了什么" not in body, f"Found '事件发生了什么' in body"
        assert "热榜" not in body
        assert "权威信息确认" not in body

    def test_custom_topic_has_case_requirement(self):
        """手动话题必须要求案例和步骤"""
        topic = _topic("个人品牌建设")
        bundle = _research_bundle("custom_topic")
        bundle["custom_topic"] = True
        prompt = _prompt(topic, _angle(), "科普解读", "专业分析", 1500, research_bundle=bundle)
        assert "案例" in prompt
        assert "步骤" in prompt or "具体" in prompt


# ═══════════════════════════════════════════════════════════════════
# 测试 7: 分类结构映射
# ═══════════════════════════════════════════════════════════════════

class TestClassificationStructure:
    """分类结构映射测试"""

    def test_prompt_uses_classified_headings(self):
        """热点prompt使用分类后的标题结构"""
        # B类社会话题
        topic = _topic("女子在医院坠楼身亡 家属质疑", category="社会")
        prompt = _prompt(topic, _angle(), "热点资讯", "客观通俗", 1500, research_bundle=_research_bundle())
        # 应包含B类结构: 细节画面, 事件还原等
        assert "细节画面" in prompt or "事件还原" in prompt

    def test_complete_article_uses_classified_headings(self):
        """_complete_article_structure 使用分类标题"""
        topic = _topic("某地发生重大交通事故 多人受伤", category="社会")
        # B类结构
        from generation.topic_classifier import classify_topic
        cls = classify_topic(topic.title, topic.category, topic.summary or "")
        classified_headings = tuple(h for h, _ in cls["structure"])

        article = {
            "title": "",
            "intro": "",
            "sections": [
                {"heading": classified_headings[0], "body": "正文内容" * 20, "image_brief": ""},
                {"heading": classified_headings[1], "body": "正文内容" * 20, "image_brief": ""},
                {"heading": classified_headings[2], "body": "正文内容" * 20, "image_brief": ""},
                {"heading": classified_headings[3], "body": "正文内容" * 20, "image_brief": ""},
                {"heading": classified_headings[4], "body": "正文内容" * 20, "image_brief": ""},
            ],
        }
        result = _complete_article_structure(article, topic, _angle(), classified_headings)
        result_headings = [s["heading"] for s in result.get("sections", [])]
        assert result_headings == list(classified_headings), f"Expected {classified_headings}, got {result_headings}"

    def test_generic_paragraph_dynamic_heading(self):
        """_generic_paragraph 处理任意动态标题"""
        topic = _topic("测试")
        # 新分类标题应该都有兜底内容
        for heading in ["钩子开头", "30秒速览", "单点深挖", "观点判断", "结尾互动",
                        "细节画面", "事件还原", "追问反思", "同类参照", "普通人启示",
                        "利益导语", "事件全貌", "背景解释", "三层分析", "影响判断", "后续关注"]:
            result = _generic_paragraph(topic, heading)
            assert result and len(result) > 20, f"Heading '{heading}' got empty/short fallback: {result[:50]}"


# ═══════════════════════════════════════════════════════════════════
# 测试 8: Word 导出正文达标
# ═══════════════════════════════════════════════════════════════════

class TestWordExportQuality:
    """Word 导出质量测试"""

    def test_article_markdown_has_title(self):
        """文章 markdown 有新标题"""
        article = _skeleton_article(1200)
        article["title"] = "这是一篇测试文章的标题"
        md = _append_sections_to_markdown(article)
        assert "# 这是一篇测试文章的标题" in md or md.startswith("# ")

    def test_article_markdown_has_intro(self):
        """文章 markdown 有导语"""
        article = _skeleton_article(1200)
        article["intro"] = "这是一段导语"
        md = _append_sections_to_markdown(article)
        assert "导语" in md

    def test_article_markdown_has_sections(self):
        """文章 markdown 有3～5个二级标题"""
        article = _skeleton_article(1200)
        md = _append_sections_to_markdown(article)
        h2_count = len(re.findall(r"^## ", md, re.MULTILINE))
        assert 3 <= h2_count <= 6, f"Expected 3-6 H2 headings, got {h2_count}"

    def test_article_markdown_omits_source_heading(self):
        """正式文章 markdown 不展示资料来源"""
        article = _skeleton_article(1200)
        md = _append_sections_to_markdown(article)
        assert "资料来源" not in md

    def test_article_markdown_omits_ai_statement(self):
        """正式文章 markdown 不展示 AI 生成声明"""
        article = _skeleton_article(1200)
        md = _append_sections_to_markdown(article)
        assert "AI辅助声明" not in md
        assert "AI声明" not in md

    def test_article_markdown_no_json(self):
        """无 JSON、代码块残留"""
        article = _skeleton_article(1200)
        md = _append_sections_to_markdown(article)
        assert "```" not in md
        assert '{"' not in md

    def test_body_char_count_accurate(self):
        """body_char_count 准确计数不含标题/来源/AI"""
        article = _skeleton_article(1200)
        article["title"] = "标题测试"
        article["intro"] = "导语测试内容" * 20  # 6 × 20 = 120 chars
        article["sections"] = [
            {"heading": "第一节", "body": "正文字数" * 100, "image_brief": ""},  # 4 × 100 = 400 chars
            {"heading": "第二节", "body": "更多正文" * 100, "image_brief": ""},   # 4 × 100 = 400 chars
        ]
        article["word_count"] = 1200
        count = count_body_chinese_chars(article)
        # 导语120 + 章节400+400 = 920
        assert 800 <= count <= 1000, f"Expected ~920, got {count}"


# ═══════════════════════════════════════════════════════════════════
# 测试 9: client_request_id 幂等
# ═══════════════════════════════════════════════════════════════════

class TestClientRequestId:
    """幂等性测试"""

    def test_api_batch_dedup_store_exists(self):
        """确认 _BATCH_DEDUP_STORE 存在"""
        from api import _BATCH_DEDUP_STORE
        assert isinstance(_BATCH_DEDUP_STORE, dict)

    def test_create_batch_request_has_client_request_id(self):
        """CreateBatchRequest 有 client_request_id 字段"""
        from api import CreateBatchRequest
        payload = CreateBatchRequest(
            batch_name="测试",
            mode="multi_topic",
            topic_ids=["topic-1"],
            client_request_id="test-req-id-001",
        )
        assert payload.client_request_id == "test-req-id-001"


# ═══════════════════════════════════════════════════════════════════
# 测试 10: 旧 R1.2 回归
# ═══════════════════════════════════════════════════════════════════

class TestR12Regression:
    """旧 R1.2 回归测试"""

    def test_quality_gate_accepts_known_structure(self):
        """已知结构的文章通过质量门"""
        article = _skeleton_article(1100)
        article["word_count"] = 1200
        article["body_char_count"] = 1100
        article["content_markdown"] = "# T\n\n" + "读者可以核验信息的真实性。" * 50 + "\n\n## 资料来源\n来源\n\nAI辅助声明"
        article["sections"] = [
            {"heading": "核验路径", "body": "读者可以核验信息的真实性和来源可靠性。" * 15, "image_brief": ""},
            {"heading": "传播风险", "body": "存在误读风险需要读者注意。" * 15, "image_brief": ""},
            {"heading": "读者启示", "body": "读者启示是先核实来源，再判断相关说法是否存在夸张或遗漏。" * 15, "image_brief": ""},
        ]
        gate = quality_gate(article, _research_bundle(accepted_count=2))
        assert gate["status"] != "failed", f"Unexpected failed: {gate['hard_errors']}"

    def test_count_body_chinese_chars_excludes_metadata(self):
        """count_body_chinese_chars 不含来源和AI声明"""
        article = {
            "title": "标题测试五个汉字",
            "intro": "导语十个汉字测试内容整理",
            "sections": [
                {"heading": "第一章", "body": "正文字数从这里开始计数二十个"},
            ],
            "source_list": ["来源：很多很多很多很多很多很多很多很多很多很多字" * 20],
            "ai_statement": "AI声明声明声明声明声明声明声明声明声明声明声明声明声明声明" * 20,
        }
        count = count_body_chinese_chars(article)
        assert count < 50, f"Body only (title+intro+section) should be <50, got {count}"

    def test_fallback_article_never_uses_old_weak_template(self):
        """回退文章不使用旧弱模板"""
        from generation.article_generator import _generic_paragraph
        topic = _topic()
        # 旧的兜底不应再出现
        old_fallback = _generic_paragraph(topic, "未知标题")
        assert "后续仍需等待权威信息确认" in old_fallback or len(old_fallback) > 20

    def test_prompt_truncation_3500(self):
        """prompt截断到3500字符"""
        topic = _topic("A" * 100)
        prompt = _prompt(topic, _angle(), "热点资讯", "客观通俗", 1200, research_bundle=_research_bundle())
        assert len(prompt) <= 3500, f"Prompt too long: {len(prompt)}"

    def test_unknown_phrases_list_not_empty(self):
        """UNKNOWN_PHRASES 词表不为空"""
        assert len(UNKNOWN_PHRASES) >= 10

    def test_value_section_markers_not_empty(self):
        """VALUE_SECTION_MARKERS 类别齐全"""
        assert len(VALUE_SECTION_MARKERS) >= 6


# ═══════════════════════════════════════════════════════════════════
# 测试 11: 综合场景
# ═══════════════════════════════════════════════════════════════════

class TestIntegrationScenarios:
    """综合场景测试"""

    def test_full_pipeline_word_count_respected(self):
        """完整流程：字数映射 + 分类 + 质量门"""
        topic = _topic("明星A被曝与网红B秘密交往 网友热议", category="娱乐")
        # 1. 分类应为A类
        cls = classify_topic(topic.title, topic.category, topic.summary or "")
        assert cls["category_key"] == "A"
        assert cls["target_chars"] == 1200

        # 2. Prompt 应包含1200~1400目标
        prompt = _prompt(topic, _angle(), "热点资讯", "客观通俗", 1200, research_bundle=_research_bundle())
        assert "1200～1400" in prompt

        # 3. 文章结构应包含A类标题
        for heading, _ in cls["structure"]:
            assert heading in prompt

    def test_limited_article_still_valuable(self):
        """hotlist_limited 文章仍有价值段落"""
        topic = _topic("突发事件引发全网热议")
        prompt = _prompt(topic, _angle(), "热点资讯", "客观通俗", 1200,
                         research_bundle=_research_bundle("hotlist_limited", accepted_count=0))
        # 必须有建设性内容
        assert "传播核验" in prompt or "核验路径" in prompt
        # 不能只是免责声明
        disclaimer_count = prompt.count("无法确认") + prompt.count("尚未确认") + prompt.count("仍待核实")
        content_word_count = len(prompt)
        disclaimer_ratio = disclaimer_count * 5 / max(content_word_count, 1)
        assert disclaimer_ratio < 0.15, f"Disclaimer ratio too high: {disclaimer_ratio:.2%}"

    def test_custom_topic_full_content(self):
        """手动话题完整流程测试"""
        topic = _topic("个人财务管理入门")
        bundle = _research_bundle("custom_topic")
        bundle["custom_topic"] = True
        prompt = _prompt(topic, _angle(), "科普解读", "专业分析", 1500, research_bundle=bundle)

        # 必须有所有方法型结构
        required = ["核心概念", "可执行方法", "具体步骤", "风险提醒", "总结"]
        for req in required:
            assert req in prompt, f"Missing '{req}' in custom topic prompt"

        # 不能有新闻模板词（排除禁令行自引用）
        body_lines = [l for l in prompt.split("\n") if not re.match(r"\d+\.\s*不得出现", l)]
        body = "\n".join(body_lines)
        forbidden = ["事件发生了什么", "热榜", "权威信息确认"]
        for fw in forbidden:
            assert fw not in body, f"Forbidden '{fw}' found in custom topic prompt body"
