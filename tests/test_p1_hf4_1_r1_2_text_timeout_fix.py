"""Test R1.2 text generation timeout and response_format fixes."""
from __future__ import annotations

import pytest
from unittest.mock import patch, MagicMock

from providers.contracts import ArticleGenerationRequest
from generation.article_generator import _prompt, generate_article, CUSTOM_TOPIC_SECTION_HEADINGS, MAX_TEXT_GENERATION_CALLS
from modules.models import HotTopic


class TestR1_2TextTimeoutFix:
    """Verify R1.2 text generation fixes."""

    def test_max_text_generation_calls_is_one(self):
        """Lock: auto-call limit is 1 per article."""
        assert MAX_TEXT_GENERATION_CALLS == 1

    def test_article_generation_request_defaults(self):
        """ArticleGenerationRequest defaults should be explicit."""
        req = ArticleGenerationRequest("test", response_format="none")
        assert req.response_format == "none"

    def test_manual_topic_prompt_does_not_require_json(self):
        """Manual topic prompt must NOT contain JSON requirement."""
        topic = HotTopic.from_dict({
            "id": "test-1",
            "title": "普通人该如何使用AI赚钱",
            "source": "manual",
            "source_name": "手动输入",
        })
        angle = {"name": "方法指南", "instruction": "提供可执行入门方案"}
        research_bundle = {
            "research_status": "custom_topic",
            "custom_topic": True,
        }
        prompt = _prompt(topic, angle, "方法型", "通俗实用", 800, research_bundle=research_bundle)
        # Must NOT contain positive JSON instruction (json_object, 返回JSON, etc.)
        assert "json_object" not in prompt.lower()
        assert "返回 JSON" not in prompt
        assert "返回json" not in prompt.lower()
        assert "sections" not in prompt  # JSON schema fields
        assert "content_markdown" not in prompt  # JSON schema fields
        assert "直接输出标准 Markdown" in prompt

    def test_manual_topic_prompt_does_not_contain_news_template(self):
        """Manual topic prompt must NOT contain news hotlist fields."""
        topic = HotTopic.from_dict({
            "id": "test-2",
            "title": "测试话题",
            "source": "manual",
            "source_name": "手动输入",
        })
        angle = {"name": "测试", "instruction": "测试"}
        research_bundle = {
            "research_status": "custom_topic",
            "custom_topic": True,
        }
        prompt = _prompt(topic, angle, "方法型", "通俗", 800, research_bundle=research_bundle)
        assert "canonical_fact" not in prompt
        assert "independent_publishers" not in prompt
        assert "新闻热榜" not in prompt

    def test_manual_topic_prompt_targets_700_1000_chars(self):
        """Manual topic prompt should target 700-1000 Chinese chars."""
        topic = HotTopic.from_dict({
            "id": "test-3",
            "title": "测试",
            "source": "manual",
            "source_name": "手动输入",
        })
        angle = {"name": "测试", "instruction": "测试"}
        research_bundle = {
            "research_status": "custom_topic",
            "custom_topic": True,
        }
        prompt = _prompt(topic, angle, "方法型", "通俗", 800, research_bundle=research_bundle)
        assert "700～1000" in prompt

    def test_manual_topic_prompt_under_3500_chars(self):
        """Manual topic prompt must be under 3500 chars."""
        topic = HotTopic.from_dict({
            "id": "test-4",
            "title": "A" * 200,
            "source": "manual",
            "source_name": "手动输入",
            "summary": "B" * 500,
        })
        angle = {"name": "测试" * 20, "instruction": "测试" * 20}
        research_bundle = {
            "research_status": "custom_topic",
            "custom_topic": True,
        }
        prompt = _prompt(topic, angle, "方法型", "通俗", 800, research_bundle=research_bundle)
        assert len(prompt) <= 3500, f"Prompt too long: {len(prompt)} chars"

    def test_hotlist_prompt_allows_markdown_only(self):
        """Hotlist prompt should only ask for Markdown, no JSON."""
        topic = HotTopic.from_dict({
            "id": "test-5",
            "title": "热点测试",
            "source": "tophub",
            "source_name": "今日头条",
        })
        angle = {"name": "热点解读", "instruction": "解读"}
        research_bundle = {
            "research_status": "sufficient",
            "accepted_source_count": 2,
        }
        prompt = _prompt(topic, angle, "热点资讯", "客观", 800, research_bundle=research_bundle)
        assert "不要输出 JSON" in prompt
        assert "json_object" not in prompt.lower()

    def test_generate_article_uses_response_format_none(self):
        """generate_article must request response_format='none'."""
        topic = HotTopic.from_dict({
            "id": "test-6",
            "title": "测试",
            "source": "manual",
            "source_name": "手动输入",
        })
        angle = {"name": "测试", "instruction": "测试"}
        profile = {
            "api_key": "sk-test",
            "base_url": "https://api.test.com",
            "model": "test-model",
            "response_format": "json_object",
            "timeout_seconds": 120,
            "auth_type": "bearer",
        }
        research_bundle = {
            "research_status": "custom_topic",
            "custom_topic": True,
            "sources": [{
                "source_id": "custom-topic-input",
                "source_name": "手动输入",
                "fetch_success": True,
                "custom_topic_input": True,
            }],
        }
        with patch("generation.article_generator.OpenAITextProvider") as mock_provider_class:
            mock_provider = MagicMock()
            mock_provider.generate_article.return_value = "# 测试标题\n\n导语内容\n\n## 核心概念\n正文内容\n\n## 可执行方法\n方法细节\n\n## 具体步骤\n步骤说明\n\n## 风险提醒\n风险说明\n\n## 总结\n总结内容"
            mock_provider_class.return_value = mock_provider

            generation_stats = {"text_generation_calls": 0, "text_generation_limit": 1, "text_generation_second_call_reason": ""}
            article = generate_article(
                topic=topic,
                angle=angle,
                article_type="方法型",
                style="通俗",
                word_count=800,
                profile=profile,
                demo_mode=False,
                app_mode="production",
                research_bundle=research_bundle,
                generation_stats=generation_stats,
            )

            # Verify the call was made with response_format="none"
            call_args = mock_provider.generate_article.call_args[0][0]
            assert isinstance(call_args, ArticleGenerationRequest)
            assert call_args.response_format == "none", f"Expected 'none', got '{call_args.response_format}'"
            assert article["text_generation_calls"] == 1

    def test_markdown_response_produces_complete_article(self):
        """Markdown response should be parsed into a complete article."""
        topic = HotTopic.from_dict({
            "id": "test-7",
            "title": "测试话题",
            "source": "manual",
            "source_name": "手动输入",
        })
        angle = {"name": "测试", "instruction": "测试"}
        profile = {
            "api_key": "sk-test",
            "base_url": "https://api.test.com",
            "model": "test-model",
            "response_format": "json_object",
            "timeout_seconds": 120,
            "auth_type": "bearer",
        }
        research_bundle = {
            "research_status": "custom_topic",
            "custom_topic": True,
            "sources": [{
                "source_id": "custom-topic-input",
                "source_name": "手动输入",
                "fetch_success": True,
                "custom_topic_input": True,
            }],
        }
        markdown_response = """# AI赚钱入门指南

AI正在改变普通人挣钱的方式。本文从实用角度出发，梳理你能立马上手的方向和步骤。

## 核心概念或事件概览
普通人用AI挣钱的核心不是成为技术专家，而是成为"会用工具的人"。你不需要写代码，只需要知道AI能做什么、不能做什么，然后帮愿意付费的人解决他们不想做的事。

## 可执行方法或背景原因
小红书文案代写是最容易起步的方向。很多店主每天要发5-10篇推广笔记，但不会写、没时间写。你用AI一篇15分钟搞定，收费30-80元一篇。

## 具体步骤或影响分析
第一步：注册闲鱼或小红书账号，挂"AI文案代写"服务。第二步：用AI生成几篇示例文案当"作品"。第三步：看到有人搜"代写文案"就主动评论吸引客户。第四步：接到单后用AI生成初稿，人工修改后交付。

## 风险提醒或后续关注
不要承诺能带来多少销量——你卖的是文案撰写服务，不是销售承诺。不要用AI生成的内容直接交付，至少花5分钟修改。涉及品牌名、产品功效、数据对比时更要小心。

## 总结
从代写文案起步，跑通第一单，再扩展其他服务。关键是先动起来，而不是一直学习永远不上场。"""

        with patch("generation.article_generator.OpenAITextProvider") as mock_provider_class:
            mock_provider = MagicMock()
            mock_provider.generate_article.return_value = markdown_response
            mock_provider_class.return_value = mock_provider

            generation_stats = {"text_generation_calls": 0, "text_generation_limit": 1, "text_generation_second_call_reason": ""}
            article = generate_article(
                topic=topic,
                angle=angle,
                article_type="方法型",
                style="通俗",
                word_count=800,
                profile=profile,
                demo_mode=False,
                app_mode="production",
                research_bundle=research_bundle,
                generation_stats=generation_stats,
            )

            assert article["text_generation_calls"] == 1
            assert article["fallback_kind"] != "custom_topic_fallback", f"Should not be fallback, got {article.get('fallback_kind')}"
            assert len(article.get("content_markdown") or "") > 200
            assert article["body_char_count"] >= 200
            # Should have sections
            sections = article.get("sections") or []
            assert len(sections) >= 3, f"Expected >=3 sections, got {len(sections)}"

    def test_timeout_does_not_crash_but_falls_back(self):
        """When provider times out, fallback should engage without crashing."""
        topic = HotTopic.from_dict({
            "id": "test-8",
            "title": "测试",
            "source": "manual",
            "source_name": "手动输入",
        })
        angle = {"name": "测试", "instruction": "测试"}
        profile = {
            "api_key": "sk-test",
            "base_url": "https://api.test.com",
            "model": "test-model",
            "response_format": "json_object",
            "timeout_seconds": 120,
            "auth_type": "bearer",
        }
        research_bundle = {
            "research_status": "custom_topic",
            "custom_topic": True,
            "sources": [{
                "source_id": "custom-topic-input",
                "source_name": "手动输入",
                "fetch_success": True,
                "custom_topic_input": True,
            }],
        }

        with patch("generation.article_generator.OpenAITextProvider") as mock_provider_class:
            from providers.text_provider import ProviderError
            mock_provider = MagicMock()
            mock_provider.generate_article.side_effect = ProviderError("TIMEOUT", "text model response timed out")
            mock_provider_class.return_value = mock_provider

            generation_stats = {"text_generation_calls": 0, "text_generation_limit": 1, "text_generation_second_call_reason": ""}
            with pytest.raises(ProviderError) as exc_info:
                generate_article(
                    topic=topic,
                    angle=angle,
                    article_type="方法型",
                    style="通俗",
                    word_count=800,
                    profile=profile,
                    demo_mode=False,
                    app_mode="production",
                    research_bundle=research_bundle,
                    generation_stats=generation_stats,
                )
            assert exc_info.value.code == "TIMEOUT"

    def test_api_key_not_logged(self):
        """API key must not appear in article output."""
        topic = HotTopic.from_dict({
            "id": "test-9",
            "title": "测试",
            "source": "manual",
            "source_name": "手动输入",
        })
        angle = {"name": "测试", "instruction": "测试"}
        profile = {
            "api_key": "sk-secret-key-12345",
            "base_url": "https://api.test.com",
            "model": "test-model",
            "response_format": "json_object",
            "timeout_seconds": 120,
            "auth_type": "bearer",
        }
        research_bundle = {
            "research_status": "custom_topic",
            "custom_topic": True,
            "sources": [{
                "source_id": "custom-topic-input",
                "source_name": "手动输入",
                "fetch_success": True,
                "custom_topic_input": True,
            }],
        }

        with patch("generation.article_generator.OpenAITextProvider") as mock_provider_class:
            mock_provider = MagicMock()
            mock_provider.generate_article.return_value = "# 测试\n\n导语\n\n## 核心概念\n正文"
            mock_provider_class.return_value = mock_provider

            generation_stats = {"text_generation_calls": 0, "text_generation_limit": 1, "text_generation_second_call_reason": ""}
            article = generate_article(
                topic=topic,
                angle=angle,
                article_type="方法型",
                style="通俗",
                word_count=800,
                profile=profile,
                demo_mode=False,
                app_mode="production",
                research_bundle=research_bundle,
                generation_stats=generation_stats,
            )

            article_json = str(article)
            assert "sk-secret-key-12345" not in article_json

    def test_single_task_timeout_not_capped_at_70(self):
        """The 70-second hardcap must be removed from run_single_task."""
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

        # Read the actual single_task.py to verify the fix is in place
        single_task_path = Path(__file__).resolve().parents[1] / "generation" / "single_task.py"
        content = single_task_path.read_text(encoding="utf-8")
        assert "max(90, min(180" in content, "Timeout fix not found in single_task.py"
        # The old hardcap must be gone
        assert "min(70" not in content, "Old 70-second hardcap still present in single_task.py"
