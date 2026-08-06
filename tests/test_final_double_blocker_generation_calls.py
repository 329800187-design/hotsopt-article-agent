from __future__ import annotations

import pytest

from generation.article_generator import generate_article
from modules.models import HotTopic
from providers.text_provider import OpenAITextProvider, ProviderError


def _topic() -> HotTopic:
    return HotTopic(
        id="final-double-blocker-topic",
        title="智能驾驶提示灯会成为行业趋势吗",
        category="汽车",
        summary="围绕智能驾驶提示灯的讨论正在升温。",
        source_url="https://example.com/topic",
        captured_at="2026-07-30",
    )


def _angle() -> dict[str, object]:
    return {"name": "趋势分析", "instruction": "分析趋势和边界", "structure": [], "must_avoid": []}


def _profile() -> dict[str, object]:
    return {"api_key": "test-key", "model": "test-model", "base_url": "https://example.test/v1", "endpoint": "/chat/completions"}


def _paragraph(seed: str, repeat: int) -> str:
    return (seed + "这一段补充事件背景、现实影响和读者判断路径，避免简单复述标题。") * repeat


def _markdown(body_repeat: int) -> str:
    return (
        "# 车外提示灯会成为智能驾驶的新标配吗\n\n"
        "导语：围绕智能驾驶提示灯的讨论正在升温，公众关注它是否会成为车辆与外界沟通的新方式。\n\n"
        "## 事件发生了什么\n\n"
        f"{_paragraph('事件层面，', body_repeat)}\n\n"
        f"{_paragraph('公开讨论中，', body_repeat)}\n\n"
        "## 为什么受到关注\n\n"
        f"{_paragraph('关注原因在于，', body_repeat)}\n\n"
        f"{_paragraph('消费者视角看，', body_repeat)}\n\n"
        "## 可能带来哪些影响\n\n"
        f"{_paragraph('可能影响包括，', body_repeat)}\n\n"
        f"{_paragraph('行业实践中，', body_repeat)}\n\n"
        "## 后续值得关注什么\n\n"
        f"{_paragraph('后续观察点是，', body_repeat)}\n\n"
        f"{_paragraph('判断趋势时，', body_repeat)}\n"
    )


def _run_with_responses(monkeypatch: pytest.MonkeyPatch, responses: list[str], stats: dict | None = None) -> dict:
    calls: list[dict[str, object]] = []

    def fake_generate(self, prompt: str, temperature: float = 0.8, max_tokens: int = 3000) -> str:
        calls.append({"prompt": prompt, "max_tokens": max_tokens})
        self.last_diagnostic = {
            "http_status": 200,
            "content_type": "application/json; charset=utf-8",
            "parser_mode": "json",
            "timeout_seconds": 180,
        }
        index = len(calls) - 1
        if index >= len(responses):
            raise AssertionError("unexpected extra model call")
        return responses[index]

    monkeypatch.setattr(OpenAITextProvider, "generate", fake_generate)
    result = generate_article(
        _topic(),
        _angle(),
        "热点资讯",
        "客观通俗",
        1200,
        _profile(),
        research_bundle=None,
        generation_stats=stats if stats is not None else {},
    )
    result["_test_calls"] = calls
    return result


def test_initial_generation_long_enough_uses_one_call(monkeypatch: pytest.MonkeyPatch) -> None:
    article = _run_with_responses(monkeypatch, [_markdown(5)])
    assert article["text_generation_calls"] == 1
    assert article["text_generation_call_1_reason"] == "INITIAL_GENERATION"
    assert article["body_char_count"] >= 1000


def test_short_valid_initial_rewrites_once_and_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    article = _run_with_responses(monkeypatch, [_markdown(3), _markdown(5)])
    assert article["text_generation_calls"] == 2
    assert article["text_generation_call_1_reason"] == "INITIAL_GENERATION"
    assert article["text_generation_call_2_reason"] == "CONTENT_TOO_SHORT_REWRITE"
    assert article["body_char_count"] >= 1000


def test_invalid_initial_recovers_once_when_recovery_is_long_enough(monkeypatch: pytest.MonkeyPatch) -> None:
    article = _run_with_responses(monkeypatch, ["", _markdown(5)])
    assert article["text_generation_calls"] == 2
    assert article["text_generation_call_2_reason"] == "INVALID_OUTPUT_RECOVERY"
    assert article["body_char_count"] >= 1000


def test_invalid_initial_short_recovery_gets_third_length_rewrite(monkeypatch: pytest.MonkeyPatch) -> None:
    article = _run_with_responses(monkeypatch, ["", _markdown(3), _markdown(5)])
    assert article["text_generation_calls"] == 3
    assert article["text_generation_call_1_reason"] == "INITIAL_GENERATION"
    assert article["text_generation_call_2_reason"] == "INVALID_OUTPUT_RECOVERY"
    assert article["text_generation_call_3_reason"] == "CONTENT_TOO_SHORT_REWRITE"
    assert article["body_char_count"] >= 1000


def test_invalid_initial_invalid_recovery_does_not_call_third(monkeypatch: pytest.MonkeyPatch) -> None:
    stats: dict = {}
    with pytest.raises(ProviderError):
        _run_with_responses(monkeypatch, ["", ""], stats)
    assert stats["text_generation_calls"] == 2
    assert stats["text_generation_call_reasons"] == ["INITIAL_GENERATION", "INVALID_OUTPUT_RECOVERY"]


def test_authentication_failure_does_not_trigger_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def fake_generate(self, prompt: str, temperature: float = 0.8, max_tokens: int = 3000) -> str:
        calls.append(prompt)
        raise ProviderError("AUTHENTICATION_FAILED", "bad key")

    stats: dict = {}
    monkeypatch.setattr(OpenAITextProvider, "generate", fake_generate)
    with pytest.raises(ProviderError):
        generate_article(_topic(), _angle(), "热点资讯", "客观通俗", 1200, _profile(), generation_stats=stats)
    assert len(calls) == 1
    assert stats["text_generation_calls"] == 1


def test_no_hardcoded_padding_is_injected(monkeypatch: pytest.MonkeyPatch) -> None:
    article = _run_with_responses(monkeypatch, [_markdown(3), _markdown(5)])
    content = article["content_markdown"]
    assert "公共交通" not in content
    assert "身份标签" not in content
    assert "公共空间冲突" not in content
    assert len(article["text_generation_call_reasons"]) <= 3
