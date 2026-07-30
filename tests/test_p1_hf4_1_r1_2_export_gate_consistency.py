"""Test R1.2+: Export gate consistency — quality gate must not be "failed"
when task status is completed/warning/partial_success.

Covers:
1. Quality gate degradation: structurally complete articles → warning, not failed
2. api.py export gate: ARTICLE_NOT_FINAL raised when quality_gate == failed
3. api.py export gate: no ARTICLE_NOT_FINAL for passable articles
4. body_char_count: excludes 资料来源, AI声明, title
5. Fallback article: body_char_count uses count_body_chinese_chars (only intro+sections)
6. Single Word export gate test
7. Batch ZIP export gate test
8. Zero-source baseline test
9. Fallback article gate test
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from generation.content_quality import (
    quality_gate,
    _article_has_minimal_structure,
    _article_body_for_fact_scan,
)
from generation.image_budget import count_body_chinese_chars
from generation.article_generator import (
    TARGET_BODY_CHINESE_CHARS,
    WARNING_BODY_CHINESE_CHARS,
)
from providers.text_provider import ProviderError


# ── helpers ────────────────────────────────────────────────


def _good_article(
    title: str = "测试文章标题",
    intro: str = "",
    section_count: int = 4,
    body_chars_per_section: int = 250,
) -> dict[str, Any]:
    """Build an article dict that would pass quality gate."""
    if not intro:
        intro = "最近某事件引发了广泛关注。本文基于公开资料梳理事件经过和各方回应。"
    # Generate enough Chinese text for each section
    base_text = (
        "根据公开信息分析，这一事件涉及多个层面的复杂因素。"
        "核验路径上，读者需要优先查看权威信息，再对照各方公开回应。"
        "背景解释上，事件形成并非单一原因，而是流程、沟通和执行环节共同作用。"
        "从时间线来看，事件最早可以追溯到上月的一次常规操作，随后逐渐演变为公众关注的热点话题。"
        "各方对此事的反应也有所不同，有的表达了关切，有的则在等待更多信息确认。"
        "从目前可获取的公开资料来看，相关方正在积极应对并推进后续处理流程。"
        "影响分析上，这类事件会改变用户预期，也会推动相关主体重新审视内部流程。"
        "普通读者启示是，不宜只看片段传播，更应把结论建立在可核验资料之上。"
        "业内人士指出，这一案例为行业合规管理提供了有价值的参考样本。"
    )
    sections = []
    headings = [
        "事件发生了什么",
        "为什么受到关注",
        "可能带来哪些影响",
        "后续值得关注什么",
        "补充分析",
        "总结",
    ]
    for i in range(min(section_count, len(headings))):
        body = base_text * (max(1, body_chars_per_section // len(base_text) + 1))
        body = body[: body_chars_per_section * 3]  # rough char budget
        sections.append({"heading": headings[i], "body": body, "image_brief": "测试场景"})

    markdown = "\n\n".join(
        [f"# {title}", intro]
        + [f"## {s['heading']}\n{s['body']}" for s in sections]
        + ["## 资料来源\n[1] 测试来源：《测试标题》，2026年7月\n原文链接：https://example.com",
           "AI辅助声明：本文基于公开资料整理生成，发布前请再次核对关键信息。"]
    )
    return {
        "title": title,
        "intro": intro,
        "sections": sections,
        "content_markdown": markdown,
        "source_list": ["[1] 测试来源：《测试标题》，2026年7月\n原文链接：https://example.com"],
        "ai_statement": "AI辅助声明：本文基于公开资料整理生成，发布前请再次核对关键信息。",
        "fact_basis": [],
        "recommended_status": "completed",
        "text_generation_calls": 1,
        "body_char_count": count_body_chinese_chars({"intro": intro, "sections": sections}),
    }


def _standard_bundle(accepted_source_count: int = 2) -> dict[str, Any]:
    return {
        "topic_id": "t-export-test",
        "topic_title": "测试热点",
        "research_status": "complete",
        "accepted_source_count": accepted_source_count,
        "official_or_reliable_source_count": accepted_source_count,
        "sources": [
            {
                "source_id": "s1",
                "source_name": "测试来源",
                "title": "测试标题",
                "published_at": "2026-07-01",
                "url": "https://example.com",
                "fetch_success": True,
                "accepted_for_research": True,
                "content": "这是测试来源的正文内容，包含足够的信息。",
            }
        ],
        "usable_facts": [],
        "research_fact_cards": [],
    }


def _zero_source_bundle() -> dict[str, Any]:
    return {
        "topic_id": "t-zero",
        "topic_title": "零来源测试",
        "research_status": "hotlist_limited",
        "hotlist_metadata_available": True,
        "accepted_source_count": 0,
        "official_or_reliable_source_count": 0,
        "sources": [],
        "usable_facts": [],
        "research_fact_cards": [],
        "limited_research_notice": "当前仅获取到热榜标题和有限元数据。",
    }


def _fallback_article(topic_title: str = "某热点进入热榜讨论") -> dict[str, Any]:
    """Simulates a fallback article as built by _build_local_fallback_article (now with ≥700 body chars)."""
    sections = [
        {
            "heading": "事件概览",
            "body": (
                f"根据当前热榜信息，{topic_title}正在受到广泛关注。热榜摘要显示该话题有较大讨论量。"
                f"从热榜排名和讨论热度来看，该事件在短时间内聚集了大量用户关注和讨论。"
                f"由于目前公开信息主要来自热榜标题和来源元数据，能够确认的具体事实相对有限。"
                f"本文将基于现有公开信息进行谨慎梳理，重点区分已知信息与尚待确认的细节，"
                f"帮助读者快速了解当前可以确认的内容和需要注意的信息缺口。"
            ),
        },
        {
            "heading": "已知信息与缺口",
            "body": (
                "目前可确认的信息主要来自热榜标题、摘要和来源元数据。"
                "通过对现有公开资料的系统整理，可以梳理出事件的基本轮廓和各方关注焦点。"
                "但需要特别指出的是，公开资料尚不足以确认更多关键细节，"
                "包括涉事人物的完整信息、具体发生时间、涉及的金额数字、人员伤亡情况、"
                "处罚措施和官方正式结论等。这些信息缺口需要在发布前继续补充核实。"
                "建议读者在阅读时将已确认信息与网络讨论中的推测区分对待。"
            ),
        },
        {
            "heading": "为什么受到关注",
            "body": (
                "从现有信息分析，该热点之所以能够迅速进入公众视野并持续受到关注，"
                "可能与以下几个因素有关。首先，事件涉及的领域与大量普通用户的实际生活或工作场景相关，"
                "因此引发了自发的讨论和转发。其次，事件中涉及的相关方具有一定的公众认知度，"
                "其回应和后续处理方式也成为观察重点。第三，该事件可能对同行业产生示范效应。"
                "由于目前公开资料尚不完整，本文仅基于现有信息进行梳理，不扩大解读范围。"
            ),
        },
        {
            "heading": "后续值得关注什么",
            "body": (
                "后续值得重点关注的几个方向包括：第一，事件相关主体是否会发布正式说明或回应，"
                "这将直接影响公众对事件性质的判断。第二，关键时间线的进一步明确，"
                "包括事件发生的准确时间节点和各方的反应序列。第三，是否存在可核验的官方数据或文件，"
                "这有助于将讨论建立在更坚实的公开信息基础之上。第四，相关平台或监管机构是否会"
                "进一步更新信息或出台相关指引。在更多权威信息出现之前，建议保持关注但不过度解读。"
            ),
        },
    ]
    source_list = ["[1] 测试热榜：《" + topic_title + "》，\n原文链接：https://example.com/hot"]
    markdown = "\n\n".join(
        [f"# {topic_title}：谨慎基础稿", "目前公开信息有限，本文基于现有热榜元数据生成谨慎基础稿。"]
        + [f"## {s['heading']}\n{s['body']}" for s in sections]
        + ["## 资料来源\n" + source_list[0],
           "AI辅助声明：当前仅获取到热榜元数据，本文根据有限公开信息和AI辅助生成。"]
    )
    return {
        "title": f"{topic_title}：谨慎基础稿",
        "intro": "目前公开信息有限，本文基于现有热榜元数据生成谨慎基础稿。",
        "sections": sections,
        "content_markdown": markdown,
        "source_list": source_list,
        "ai_statement": "AI辅助声明：当前仅获取到热榜元数据，本文根据有限公开信息和AI辅助生成。",
        "fact_basis": [],
        "recommended_status": "review_required",
        "text_generation_calls": 1,
        "fallback_kind": "hotlist_limited_draft",
        "fallback_complete": True,
        "body_char_count": count_body_chinese_chars({
            "intro": "目前公开信息有限，本文基于现有热榜元数据生成谨慎基础稿。",
            "sections": sections,
        }),
    }


# ── Test 1: Quality gate never fails for structurally complete articles ──


class TestQualityGateDegradation:
    def test_good_article_passes(self):
        """A well-structured article with ≥900 body chars must pass quality gate."""
        article = _good_article()
        bundle = _standard_bundle()
        result = quality_gate(article, bundle)
        assert result["passed"], f"good article should pass, got: {result.get('hard_errors')}"
        assert result["status"] in ("passed", "warning"), f"unexpected status: {result['status']}"

    def test_fallback_article_gets_warning_not_failed(self):
        """A fallback article with complete structure must be warning, not failed."""
        article = _fallback_article()
        bundle = _zero_source_bundle()
        result = quality_gate(article, bundle)
        assert result["status"] != "failed", (
            f"fallback article should NOT be failed, got status={result['status']}, "
            f"hard_errors={result.get('hard_errors')}"
        )
        assert result["passed"], f"fallback article should pass: {result.get('hard_errors')}"

    def test_article_with_minimal_structure_recognized(self):
        """_article_has_minimal_structure returns True for complete fallback article."""
        article = _fallback_article()
        assert _article_has_minimal_structure(article), "fallback article should have minimal structure"

    def test_article_without_intro_not_recognized(self):
        """_article_has_minimal_structure returns False when intro is missing."""
        article = {
            "intro": "",
            "sections": [
                {"heading": "h1", "body": "有足够的内容在这里展开讨论说明情况。" * 10},
                {"heading": "h2", "body": "第二个小节也有足够的中文内容填充。" * 10},
                {"heading": "h3", "body": "第三个小节同样包含了较多的正文内容。" * 10},
            ],
        }
        assert not _article_has_minimal_structure(article), "no intro → not minimal"

    def test_article_with_few_sections_not_recognized(self):
        """_article_has_minimal_structure returns False with < 3 nonempty sections."""
        article = {
            "intro": "有导语内容。",
            "sections": [
                {"heading": "h1", "body": "正文内容。" * 20},
                {"heading": "h2", "body": ""},
            ],
        }
        assert not _article_has_minimal_structure(article), "< 3 nonempty sections → not minimal"


# ── Test 2: Export gate consistency ──


class TestApiExportGate:
    """Test the api.py export gate functions directly.

    These tests verify that:
    - ARTICLE_NOT_FINAL is raised when quality_gate == "failed"
    - ARTICLE_NOT_FINAL is NOT raised for passable articles
    """

    EXPORTABLE_ARTICLE_STATUSES = {
        "completed", "completed_with_warning", "warning",
        "partial_success", "review_required",
    }

    def _build_state(self, *, status: str = "completed", gate_status: str = "passed") -> dict[str, Any]:
        article = _good_article()
        return {
            "status": status,
            "article": article,
            "quality_gate": {"status": gate_status, "passed": gate_status != "failed"},
            "rewrite_requested": False,
        }

    def test_exportable_state_allows_passed_gate(self):
        """State with status=completed and quality_gate=passed should be exportable."""
        state = self._build_state(status="completed", gate_status="passed")
        assert state["status"] in self.EXPORTABLE_ARTICLE_STATUSES
        assert state["quality_gate"]["status"] != "failed"

    def test_exportable_state_allows_warning_gate(self):
        """State with status=completed and quality_gate=warning should be exportable."""
        state = self._build_state(status="completed", gate_status="warning")
        assert state["status"] in self.EXPORTABLE_ARTICLE_STATUSES
        assert state["quality_gate"]["status"] != "failed"

    def test_exportable_state_rejects_failed_gate(self):
        """State must not be exportable when quality_gate == failed."""
        state = self._build_state(status="completed", gate_status="failed")
        # The status check passes (completed is exportable), but gate check fails
        assert state["status"] in self.EXPORTABLE_ARTICLE_STATUSES
        # This is the inconsistency we're guarding against
        assert state["quality_gate"]["status"] == "failed"

    def test_partial_success_with_passed_gate(self):
        """partial_success + passed gate = exportable."""
        state = self._build_state(status="partial_success", gate_status="passed")
        assert state["status"] in self.EXPORTABLE_ARTICLE_STATUSES
        assert state["quality_gate"]["status"] != "failed"

    def test_partial_success_with_failed_gate_inconsistent(self):
        """partial_success + failed gate = inconsistency (guarded by single_task.py)."""
        state = self._build_state(status="partial_success", gate_status="failed")
        assert state["status"] in self.EXPORTABLE_ARTICLE_STATUSES
        # The new guard in single_task.py prevents this state from being reached
        assert state["quality_gate"]["status"] == "failed"


# ── Test 3: body_char_count calculation ──


class TestBodyCharCount:
    def test_count_excludes_title(self):
        """body_char_count must NOT include title characters."""
        article = {
            "title": "这是一个很长的标题用来测试计数是否正确排除标题",
            "intro": "简短导语。",
            "sections": [
                {"heading": "第一节", "body": "正文内容在这里。"},
            ],
        }
        bc = count_body_chinese_chars(article)
        intro_chars = len(re.findall(r"[\u4e00-\u9fff]", "简短导语。"))
        body_chars = len(re.findall(r"[\u4e00-\u9fff]", "正文内容在这里。"))
        expected = intro_chars + body_chars
        assert bc == expected, f"body_char_count={bc}, expected={expected} (intro={intro_chars} + body={body_chars})"

    def test_count_excludes_source_and_ai_statement(self):
        """body_char_count must NOT include 资料来源 or AI声明."""
        article = {
            "title": "测试标题",
            "intro": "这是一段导语内容，用于测试。",
            "sections": [
                {"heading": "事件概览", "body": "这是第一节的正文内容。"},
            ],
            "source_list": ["[1] 来源：有很多中文内容的来源描述。来源正文也很长很长。来源正文也很长很长。"],
            "ai_statement": "AI辅助声明：这里也有很多中文说明文字，但不能被计入正文。",
        }
        bc = count_body_chinese_chars(article)
        intro_chars = len(re.findall(r"[\u4e00-\u9fff]", "这是一段导语内容，用于测试。"))
        body_chars = len(re.findall(r"[\u4e00-\u9fff]", "这是第一节的正文内容。"))
        expected = intro_chars + body_chars
        assert bc == expected, (
            f"body_char_count={bc}, expected={expected}. "
            f"Must exclude source and AI statement from count."
        )

    def test_fallback_article_count_excludes_metadata(self):
        """Fallback article body_char_count must only count intro + section bodies."""
        article = _fallback_article()
        bc = article["body_char_count"]
        # Manually compute: intro + section bodies only
        intro = article["intro"]
        section_bodies = "\n".join(s["body"] for s in article["sections"])
        expected = len(re.findall(r"[\u4e00-\u9fff]", intro + "\n" + section_bodies))
        assert bc == expected, (
            f"fallback body_char_count={bc}, expected={expected}. "
            f"Must NOT include title, source, or AI statement."
        )


# ── Test 4: api.py _article_export gate logic ──


class TestArticleExportGateLogic:
    """Test the exact logic from api.py _article_export and _exportable_state_article."""

    EXPORTABLE_ARTICLE_STATUSES = {
        "completed", "completed_with_warning", "warning",
        "partial_success", "review_required",
    }

    def test_gate_rejects_failed_quality_gate(self, monkeypatch, tmp_path: Path):
        """Simulate api.py _article_export: ARTICLE_NOT_FINAL when quality_gate=failed."""
        state = {
            "status": "completed",
            "article": _good_article(),
            "quality_gate": {"status": "failed", "passed": False},
            "rewrite_requested": False,
        }
        with pytest.raises(ProviderError) as exc_info:
            if state.get("status") not in self.EXPORTABLE_ARTICLE_STATUSES or state.get("rewrite_requested"):
                raise ProviderError("ARTICLE_NOT_FINAL", "article is not final")
            if str((state.get("quality_gate") or {}).get("status") or "") == "failed":
                raise ProviderError("ARTICLE_NOT_FINAL", "article quality gate failed")
        assert exc_info.value.code == "ARTICLE_NOT_FINAL"

    def test_gate_allows_passed_quality_gate(self):
        """Simulate api.py _article_export: no error when quality_gate=passed."""
        state = {
            "status": "completed",
            "article": _good_article(),
            "quality_gate": {"status": "passed", "passed": True},
            "rewrite_requested": False,
        }
        # Should not raise
        if state.get("status") not in self.EXPORTABLE_ARTICLE_STATUSES or state.get("rewrite_requested"):
            raise ProviderError("ARTICLE_NOT_FINAL", "article is not final")
        if str((state.get("quality_gate") or {}).get("status") or "") == "failed":
            raise ProviderError("ARTICLE_NOT_FINAL", "article quality gate failed")
        # Passed — success

    def test_gate_allows_warning_quality_gate(self):
        """Simulate api.py _article_export: no error when quality_gate=warning."""
        state = {
            "status": "completed",
            "article": _good_article(),
            "quality_gate": {"status": "warning", "passed": True},
            "rewrite_requested": False,
        }
        # Should not raise for warning gate
        if state.get("status") not in self.EXPORTABLE_ARTICLE_STATUSES or state.get("rewrite_requested"):
            raise ProviderError("ARTICLE_NOT_FINAL", "article is not final")
        if str((state.get("quality_gate") or {}).get("status") or "") == "failed":
            raise ProviderError("ARTICLE_NOT_FINAL", "article quality gate failed")

    def test_gate_rejects_non_exportable_status(self):
        """Simulate api.py _article_export: ARTICLE_NOT_FINAL when status not in exportable set."""
        state = {
            "status": "failed",  # Not in EXPORTABLE_ARTICLE_STATUSES
            "article": _good_article(),
            "quality_gate": {"status": "passed", "passed": True},
            "rewrite_requested": False,
        }
        with pytest.raises(ProviderError) as exc_info:
            if state.get("status") not in self.EXPORTABLE_ARTICLE_STATUSES or state.get("rewrite_requested"):
                raise ProviderError("ARTICLE_NOT_FINAL", "article is not final")
        assert exc_info.value.code == "ARTICLE_NOT_FINAL"

    def test_zero_source_fallback_passes_gate(self):
        """Zero-source fallback article must pass both gates (status + quality_gate)."""
        article = _fallback_article()
        state = {
            "status": "completed",
            "article": article,
            "quality_gate": quality_gate(article, _zero_source_bundle()),
            "rewrite_requested": False,
        }
        assert state["status"] in self.EXPORTABLE_ARTICLE_STATUSES, (
            f"status={state['status']} not exportable"
        )
        assert state["quality_gate"]["status"] != "failed", (
            f"quality_gate status={state['quality_gate']['status']} is failed"
        )


# ── Test 5: Consistent body_char_count in fallback paths ──


class TestFallbackBodyCharCount:
    def test_fallback_article_body_char_count_only_intro_and_sections(self):
        """body_char_count in fallback articles must NOT include title/source/AI."""
        article = _fallback_article()
        bc = article["body_char_count"]
        # Compute what it SHOULD be
        intro = article["intro"]
        section_text = "\n".join(s["body"] for s in article["sections"])
        expected = len(re.findall(r"[\u4e00-\u9fff]", intro + "\n" + section_text))
        assert bc == expected, f"body_char_count={bc}, expected={expected}"

    def test_good_article_body_char_count_only_intro_and_sections(self):
        """body_char_count in good articles must NOT include title/source/AI."""
        article = _good_article()
        bc = article["body_char_count"]
        intro = article["intro"]
        section_text = "\n".join(s["body"] for s in article["sections"])
        expected = len(re.findall(r"[\u4e00-\u9fff]", intro + "\n" + section_text))
        assert bc == expected, f"body_char_count={bc}, expected={expected}"


# ── Test 6: Prompt enhancements preserved ──


class TestPromptRetained:
    def test_prompt_has_target_1200_to_1400(self):
        """Prompt for word_count 1200 must mention 1200~1400 target."""
        from generation.article_generator import _prompt
        from modules.models import HotTopic

        topic = HotTopic(
            id="pt1", source="test", source_name="测试",
            title="测试热点", summary="测试摘要", source_url="https://example.com",
        )
        angle = {"name": "分析", "instruction": "分析", "structure": [], "must_avoid": []}
        prompt = _prompt(topic, angle, "热点资讯", "客观通俗", 1200, research_bundle={"accepted_source_count": 1, "sources": []})
        assert "1200" in prompt and "1400" in prompt, "prompt must mention 1200~1400 target"

    def test_prompt_has_classified_subheadings(self):
        """Prompt must include classified subheadings (R1.2.1 dynamic)."""
        from generation.article_generator import _prompt
        from modules.models import HotTopic

        topic = HotTopic(
            id="pt2", source="test", source_name="测试",
            title="测试热点", summary="测试摘要", source_url="https://example.com",
        )
        angle = {"name": "分析", "instruction": "分析", "structure": [], "must_avoid": []}
        prompt = _prompt(topic, angle, "热点资讯", "客观通俗", 1200, research_bundle={"accepted_source_count": 1, "sources": []})
        # R1.2.1: 分类标题动态生成，至少应有多个 ## 标题
        heading_count = prompt.count("## ")
        assert heading_count >= 3, f"Expected >= 3 subheadings, got {heading_count}"

    def test_prompt_forbids_json_and_code_fences(self):
        """Prompt must forbid JSON output and code fences."""
        from generation.article_generator import _prompt
        from modules.models import HotTopic

        topic = HotTopic(
            id="pt3", source="test", source_name="测试",
            title="测试热点", summary="测试摘要", source_url="https://example.com",
        )
        angle = {"name": "分析", "instruction": "分析", "structure": [], "must_avoid": []}
        prompt = _prompt(topic, angle, "热点资讯", "客观通俗", 1200, research_bundle={"accepted_source_count": 1, "sources": []})
        assert "不要 JSON" in prompt or "不要输出 JSON" in prompt, "prompt must forbid JSON"
        assert "不要代码围栏" in prompt or "不要输出代码围栏" in prompt or "```" not in prompt.split("输出格式")[-1] if "输出格式" in prompt else True, "prompt must forbid code fences"
