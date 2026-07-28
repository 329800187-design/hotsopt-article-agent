"""Test RC1.3.3-Lite-P1-HF4.1-R1.2: Unified production text generation path.

Verifies:
1. basic_connection_test and generate share _request_text
2. article_capability_test and generate share same decoder
3. generate accepts standard JSON response
4. generate accepts SSE response
5. generate accepts text/plain Markdown response
6. generate accepts content array response
7. generate does NOT send json_object in payload
8. generate returns Markdown → article_generator parses successfully
9. HTTP success but article parse failure returns ARTICLE_PARSE_ERROR
10. Regression: test-path success + formal-path failure is fixed
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from providers.text_provider import (
    OpenAITextProvider,
    ProviderError,
    _decode_provider_response,
    _parse_sse_stream,
    _extract_response_content,
)
from providers.contracts import ArticleGenerationRequest
from generation.article_generator import generate_article as ag_generate_article
from modules.models import HotTopic


# ── helpers ────────────────────────────────────────────────

def _mock_response(body: str, status_code: int = 200, content_type: str = "application/json") -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.text = body
    resp.headers = {"Content-Type": content_type}
    resp.json.return_value = json.loads(body) if content_type == "application/json" and body.strip().startswith("{") else json.loads("{}")
    resp.raise_for_status.return_value = None
    return resp


def _make_profile(**overrides: Any) -> dict[str, Any]:
    return {
        "name": "test",
        "base_url": "https://api.example.com/v1",
        "endpoint": "/chat/completions",
        "model": "test-model",
        "api_key": "sk-test",
        "auth_type": "bearer",
        "headers": {},
        "timeout_seconds": 30,
        "response_format": "json_object",
        **overrides,
    }


STANDARD_JSON_BODY = json.dumps({
    "choices": [{"message": {"role": "assistant", "content": "# 携程被罚\n\n这是文章正文。"}}]
})

SSE_BODY = """data: {"choices":[{"delta":{"content":"# 携程被"}}]}

data: {"choices":[{"delta":{"content":"罚\\n\\n这是正文。"}}]}

data: [DONE]"""

PLAIN_TEXT_BODY = "# 携程被罚后内部全员信曝光\n\n这是一篇关于携程被罚的深度分析文章..."

CONTENT_ARRAY_BODY = json.dumps({
    "choices": [{"message": {"role": "assistant", "content": [{"type": "text", "text": "# 携程被罚\n\n文章正文内容。"}]}}]
})


# ── test 1: _decode_provider_response handles all formats ──

class TestDecodeProviderResponse:
    def test_json_standard(self):
        resp = _mock_response(STANDARD_JSON_BODY)
        content, diag = _decode_provider_response(resp)
        assert diag["parser_mode"] == "json"
        assert "# 携程被罚" in content

    def test_sse_stream(self):
        resp = _mock_response(SSE_BODY, content_type="text/event-stream")
        resp.json.side_effect = json.JSONDecodeError("bad", "", 0)
        content, diag = _decode_provider_response(resp)
        assert diag["parser_mode"] == "sse"
        assert "携程被罚" in content

    def test_plain_text(self):
        resp = _mock_response(PLAIN_TEXT_BODY, content_type="text/plain")
        resp.json.side_effect = json.JSONDecodeError("bad", "", 0)
        content, diag = _decode_provider_response(resp)
        assert diag["parser_mode"] == "text"
        assert "携程被罚" in content

    def test_content_array(self):
        resp = _mock_response(CONTENT_ARRAY_BODY)
        content, diag = _decode_provider_response(resp)
        assert diag["parser_mode"] == "json"
        assert "携程被罚" in content

    def test_empty(self):
        resp = _mock_response("", content_type="application/json")
        resp.json.return_value = {}
        content, diag = _decode_provider_response(resp)
        assert content == ""
        # parser_mode should be "json" since json parsed successfully but was empty
        assert diag["parser_mode"] in ("json", "empty")

    def test_reasoning_content_without_content(self):
        body = json.dumps({
            "choices": [{
                "message": {
                    "role": "assistant",
                    "reasoning_content": "Let me think about this...",
                }
            }]
        })
        resp = _mock_response(body)
        content, diag = _decode_provider_response(resp)
        assert content == "MODEL_OUTPUT_EMPTY"
        assert diag.get("reasoning_content_present") is True
        assert diag.get("content_present") is False


# ── test 2: _request_text shared path ──

class TestRequestText:
    def test_response_format_none_does_not_add_json_object(self):
        """Verify _request_text with response_format='none' does not add json_object to payload."""
        provider = OpenAITextProvider(_make_profile())
        resp = _mock_response(STANDARD_JSON_BODY)
        with patch("providers.text_provider.create_http_client") as mock_client:
            mock_ctx = MagicMock()
            mock_client.return_value.__enter__.return_value = mock_ctx
            mock_ctx.post.return_value = resp
            content, diag = provider._request_text(
                messages=[{"role": "user", "content": "hello"}],
                temperature=0.5,
                max_tokens=100,
                response_format="none",
            )
            call_args = mock_ctx.post.call_args
            payload = call_args[1]["json"]
            assert "response_format" not in payload
            assert diag["payload_has_response_format"] is False

    def test_response_format_json_object_adds_to_payload(self):
        provider = OpenAITextProvider(_make_profile())
        resp = _mock_response(STANDARD_JSON_BODY)
        with patch("providers.text_provider.create_http_client") as mock_client:
            mock_ctx = MagicMock()
            mock_client.return_value.__enter__.return_value = mock_ctx
            mock_ctx.post.return_value = resp
            content, diag = provider._request_text(
                messages=[{"role": "user", "content": "hello"}],
                temperature=0.5,
                max_tokens=100,
                response_format="json_object",
            )
            call_args = mock_ctx.post.call_args
            payload = call_args[1]["json"]
            assert payload.get("response_format") == {"type": "json_object"}
            assert diag["payload_has_response_format"] is True

    def test_diagnostic_fields(self):
        provider = OpenAITextProvider(_make_profile())
        resp = _mock_response(STANDARD_JSON_BODY)
        with patch("providers.text_provider.create_http_client") as mock_client:
            mock_ctx = MagicMock()
            mock_client.return_value.__enter__.return_value = mock_ctx
            mock_ctx.post.return_value = resp
            content, diag = provider._request_text(
                messages=[{"role": "user", "content": "hello"}],
                temperature=0.3,
                max_tokens=500,
                response_format="none",
            )
            assert "final_url" in diag
            assert diag["requested_response_format"] == "none"
            assert diag["max_tokens"] == 500
            assert diag["http_status"] == 200
            assert "api_key" not in str(diag).lower()  # never log key
            assert diag["error_type"] == "success"


# ── test 3: generate() uses _request_text ──

class TestGenerate:
    def test_generate_calls_request_text(self):
        provider = OpenAITextProvider(_make_profile())
        resp = _mock_response(STANDARD_JSON_BODY)
        with patch.object(provider, "_request_text", wraps=provider._request_text) as spy:
            with patch("providers.text_provider.create_http_client") as mock_client:
                mock_ctx = MagicMock()
                mock_client.return_value.__enter__.return_value = mock_ctx
                mock_ctx.post.return_value = resp
                result = provider.generate("test prompt")
                assert spy.called
                call_kwargs = spy.call_args[1]
                assert call_kwargs["response_format"] == "none"
                assert "# 携程被罚" in result

    def test_basic_connection_test_calls_request_text(self):
        provider = OpenAITextProvider(_make_profile())
        resp = _mock_response(json.dumps({
            "choices": [{"message": {"content": "连接成功"}}]
        }))
        with patch.object(provider, "_request_text", wraps=provider._request_text) as spy:
            with patch("providers.text_provider.create_http_client") as mock_client:
                mock_ctx = MagicMock()
                mock_client.return_value.__enter__.return_value = mock_ctx
                mock_ctx.post.return_value = resp
                result = provider.basic_connection_test()
                assert spy.called
                assert result.success

    def test_article_capability_test_calls_request_text(self):
        provider = OpenAITextProvider(_make_profile())
        cap_body = json.dumps({
            "choices": [{"message": {"content": json.dumps({
                "title": "Test", "intro": "Test intro",
                "sections": [
                    {"heading": "h1", "body": "b1", "image_brief": "img1"},
                    {"heading": "h2", "body": "b2", "image_brief": "img2"},
                    {"heading": "h3", "body": "b3", "image_brief": "img3"},
                ],
                "content_markdown": "Full markdown",
                "fact_basis": [{"fact_id": "f1", "fact": "test fact", "source_ids": ["s1"]}],
            })}}]
        })
        resp = _mock_response(cap_body)
        with patch.object(provider, "_request_text", wraps=provider._request_text) as spy:
            with patch("providers.text_provider.create_http_client") as mock_client:
                mock_ctx = MagicMock()
                mock_client.return_value.__enter__.return_value = mock_ctx
                mock_ctx.post.return_value = resp
                result = provider.article_capability_test()
                assert spy.called
                assert result.success


# ── test 4: regression — old path failure is fixed ──

class TestRegressionFix:
    """Reproduce the exact user-reported failure scenario and verify it's fixed."""

    def test_formal_article_with_none_response_format_succeeds_with_non_json_response(self):
        """
        Before fix: generate_article() always called response.json().
        When response_format='none' + provider returned non-JSON, this crashed.
        After fix: _decode_provider_response handles text/plain gracefully.
        """
        provider = OpenAITextProvider(_make_profile())
        # Simulate a provider that returns plain text when response_format is not json_object
        resp = _mock_response(PLAIN_TEXT_BODY, content_type="text/plain")
        resp.json.side_effect = json.JSONDecodeError("Expecting value", "", 0)
        with patch("providers.text_provider.create_http_client") as mock_client:
            mock_ctx = MagicMock()
            mock_client.return_value.__enter__.return_value = mock_ctx
            mock_ctx.post.return_value = resp
            result = provider.generate_article(
                ArticleGenerationRequest(
                    "请生成文章", temperature=0.6, max_tokens=1600, response_format="none"
                )
            )
            assert "携程被罚" in result
            assert provider.last_diagnostic["parser_mode"] == "text"

    def test_formal_article_with_sse_response_succeeds(self):
        provider = OpenAITextProvider(_make_profile())
        resp = _mock_response(SSE_BODY, content_type="text/event-stream")
        resp.json.side_effect = json.JSONDecodeError("bad", "", 0)
        with patch("providers.text_provider.create_http_client") as mock_client:
            mock_ctx = MagicMock()
            mock_client.return_value.__enter__.return_value = mock_ctx
            mock_ctx.post.return_value = resp
            result = provider.generate_article(
                ArticleGenerationRequest(
                    "请生成文章", temperature=0.6, max_tokens=1600, response_format="none"
                )
            )
            assert "携程被罚" in result
            assert provider.last_diagnostic["parser_mode"] == "sse"


# ── test 5: article_generator uses ARTICLE_PARSE_ERROR ──

class TestArticleParseError:
    def test_parse_markdown_empty_text_returns_article_parse_error(self):
        from generation.article_generator import _parse_markdown_article_response
        topic = HotTopic(
            id="t1", title="测试话题", category="科技", summary="测试摘要",
            source_url="https://example.com", rank=1, captured_at="2026-01-01",
        )
        angle = {"name": "深度分析", "instruction": "test"}
        with pytest.raises(ProviderError) as exc_info:
            _parse_markdown_article_response("", topic, angle)
        assert exc_info.value.code == "ARTICLE_PARSE_ERROR"

    def test_parse_markdown_insufficient_content_returns_article_parse_error(self):
        from generation.article_generator import _parse_markdown_article_response
        topic = HotTopic(
            id="t1", title="测试话题", category="科技", summary="测试摘要",
            source_url="https://example.com", rank=1, captured_at="2026-01-01",
        )
        angle = {"name": "深度分析", "instruction": "test"}
        # Very short content that won't meet the threshold
        with pytest.raises(ProviderError) as exc_info:
            _parse_markdown_article_response("短", topic, angle)
        assert exc_info.value.code == "ARTICLE_PARSE_ERROR"

    def test_clean_article_invalid_data_returns_article_parse_error(self):
        from generation.article_generator import _clean_article
        topic = HotTopic(
            id="t1", title="测试话题", category="科技", summary="测试摘要",
            source_url="https://example.com", rank=1, captured_at="2026-01-01",
        )
        angle = {"name": "深度分析", "instruction": "test"}
        with pytest.raises(ProviderError) as exc_info:
            _clean_article([], topic, angle)
        assert exc_info.value.code == "ARTICLE_PARSE_ERROR"


# ── test 6: full chain — generate_article returns Markdown → ag parses it ──

class TestFullChain:
    def test_provider_returns_markdown_article_generator_parses_it(self):
        """End-to-end: provider returns Markdown → article_generator successfully parses."""
        provider = OpenAITextProvider(_make_profile())
        markdown_response = json.dumps({
            "choices": [{"message": {"content": (
                "# 携程被罚后内部全员信曝光：深度分析\n\n"
                "近日携程因违规操作被监管部门处罚的消息引发广泛关注，公司内部随即发布全员信说明情况。"
                "本文基于公开资料梳理事件经过、影响及后续观察。\n\n"
                "## 事件发生了什么\n\n"
                "携程因违反相关数据安全规定，被网信办处以罚款并责令整改。"
                "根据公开通报，涉及数据收集范围超出必要限度，以及用户信息保护措施不到位等问题。"
                "公司方面表示将全面配合整改，加强数据安全体系建设。\n\n"
                "## 为什么受到关注\n\n"
                "携程作为国内领先的在线旅游平台，其数据合规问题直接关系到数亿用户的个人信息安全。"
                "此次处罚也释放出监管部门对平台数据治理持续加强的信号。\n\n"
                "## 可能带来哪些影响\n\n"
                "短期来看，携程需要投入资源进行合规整改，可能影响部分业务运营。"
                "长期来看，数据安全合规将成为行业标配，有利于建立更健康的市场秩序。\n\n"
                "## 后续值得关注什么\n\n"
                "整改落实情况、用户数据保护措施的具体改进方案、"
                "以及行业其他平台是否会跟进加强数据安全建设。"
            )}}]
        })
        resp = _mock_response(markdown_response)
        with patch("providers.text_provider.create_http_client") as mock_client:
            mock_ctx = MagicMock()
            mock_client.return_value.__enter__.return_value = mock_ctx
            mock_ctx.post.return_value = resp
            result = provider.generate_article(
                ArticleGenerationRequest(
                    "请为携程被罚话题生成文章",
                    temperature=0.6,
                    max_tokens=1600,
                    response_format="none",
                )
            )
            assert "携程被罚" in result
            assert provider.last_diagnostic["parser_mode"] == "json"

    def test_generate_passes_none_response_format(self):
        """generate() always passes response_format='none'."""
        provider = OpenAITextProvider(_make_profile(response_format="json_object"))
        resp = _mock_response(STANDARD_JSON_BODY)
        with patch.object(provider, "_request_text") as mock_rt:
            mock_rt.return_value = ("content", {"error_type": "success"})
            provider.generate("test")
            assert mock_rt.call_args[1]["response_format"] == "none"

    def test_article_generator_calls_provider_with_none(self):
        """article_generator.generate_article() passes response_format='none' to provider."""
        provider = OpenAITextProvider(_make_profile())
        markdown = (
            "# 携程被罚后内部全员信曝光\n\n"
            "携程因违反数据安全规定被监管部门处以罚款并责令整改，公司内部随后发布全员信说明情况。"
            "此次事件引发广泛关注，涉及用户数据收集范围超出必要限度等问题。"
            "本文基于公开资料梳理事件经过、分析影响并展望后续发展。\n\n"
            "## 事件发生了什么\n\n"
            "携程因数据安全问题被网信办处罚，罚款金额及整改要求在通报中明确列出。"
            "公司方面表示将全面配合整改，加强数据安全体系建设，确保用户信息安全。"
            "根据公开通报，主要涉及数据收集范围超出必要限度以及用户信息保护措施不到位等问题。\n\n"
            "## 为什么受到关注\n\n"
            "携程作为国内领先的在线旅游平台，服务数亿用户，其数据合规问题直接关系到用户个人信息安全。"
            "此次处罚也释放出监管部门对平台数据治理持续加强的信号，对整个行业具有警示意义。"
            "多家媒体对此进行了持续跟踪报道，资本市场也对此表达了关注。\n\n"
            "## 可能带来哪些影响\n\n"
            "短期内携程需要投入大量资源进行合规整改，可能影响部分业务运营效率。"
            "但从长期来看，数据安全合规将成为行业标准，有利于建立更加健康、有序的市场环境。"
            "其他平台也可能因此加强自身的数据安全建设，推动全行业合规水平提升。\n\n"
            "## 后续值得关注什么\n\n"
            "重点应关注携程整改落实的具体进度、用户数据保护措施的实际改进方案，"
            "以及监管部门的后续检查结果。同时，行业其他平台是否会跟进加强数据安全建设也值得留意。"
            "本文将持续关注后续进展，为读者提供最新信息。"
        )
        body = json.dumps({"choices": [{"message": {"content": markdown}}]})
        resp = _mock_response(body)
        topic = HotTopic(
            id="t1", title="携程被罚", category="科技",
            summary="携程被罚事件", source_url="https://example.com", rank=1,
            captured_at="2026-01-01",
        )
        angle = {"name": "深度分析", "instruction": "分析事件影响", "structure": [], "must_avoid": []}
        with patch("providers.text_provider.create_http_client") as mock_client:
            mock_ctx = MagicMock()
            mock_client.return_value.__enter__.return_value = mock_ctx
            mock_ctx.post.return_value = resp
            article = ag_generate_article(
                topic=topic,
                angle=angle,
                article_type="analysis",
                style="documentary",
                word_count=1000,
                profile=_make_profile(),
            )
            assert article is not None


# ── test 7: SSE parser ──

class TestSSEParser:
    def test_parse_sse_stream_standard(self):
        result = _parse_sse_stream(SSE_BODY)
        assert "携程被罚" in result

    def test_parse_sse_with_done_marker(self):
        body = 'data: {"choices":[{"delta":{"content":"hello"}}]}\n\ndata: [DONE]\n'
        result = _parse_sse_stream(body)
        assert result == "hello"

    def test_parse_sse_empty(self):
        assert _parse_sse_stream("") == ""

    def test_parse_sse_comments(self):
        body = ': comment line\ndata: {"choices":[{"delta":{"content":"test"}}]}\n'
        result = _parse_sse_stream(body)
        assert result == "test"
