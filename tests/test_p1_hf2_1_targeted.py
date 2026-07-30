from __future__ import annotations

import json
from pathlib import Path

import pytest

from generation.angle_planner import plan_angles
from generation.article_generator import generate_article
from modules.models import HotTopic
from modules.source_formatter import normalize_source_list
from providers.text_provider import ProviderError


ROOT = Path(__file__).resolve().parents[1]


def _topic() -> HotTopic:
    return HotTopic(
        id="hf2-1-topic",
        title="嫦娥六号月背样品研究进展",
        summary="围绕月背样品研究和后续价值展开。",
        source_url="https://example.com/topic",
    )


def _profile() -> dict[str, str]:
    return {
        "api_key": "test-key",
        "base_url": "https://example.com/v1",
        "endpoint": "/chat/completions",
        "model": "test-model",
    }


def _short_article(paragraph: str) -> str:
    payload = {
        "title": "测试文章",
        "intro": "这是一段简短导语。",
        "sections": [
            {"heading": "已知事实", "body": paragraph, "image_brief": "新闻现场"},
            {"heading": "技术说明", "body": paragraph, "image_brief": "技术画面"},
            {"heading": "后续观察", "body": paragraph, "image_brief": "研究画面"},
        ],
        "content_markdown": f"# 测试文章\n\n这是一段简短导语。\n\n## 已知事实\n{paragraph}\n\n## 技术说明\n{paragraph}\n\n## 后续观察\n{paragraph}",
        "summary": "简短摘要",
        "fact_basis": [{"fact_id": "f1", "fact": "公开资料事实", "source_ids": ["S1"], "confidence": "confirmed"}],
        "keywords": ["测试"],
        "source_statement": "来源说明",
        "ai_statement": "AI辅助声明：测试。",
    }
    return json.dumps(payload, ensure_ascii=False)



def test_duplicate_supplement_paragraph_returns_article_too_short(monkeypatch):
    paragraph = "???????????????????????????" * 12
    responses = iter([_short_article(paragraph), _short_article(paragraph + "?????")])

    def fake_generate(self, prompt: str, temperature: float = 0.8, max_tokens: int = 3000) -> str:
        return next(responses)

    monkeypatch.setattr("generation.article_generator.OpenAITextProvider.generate", fake_generate)
    stats: dict[str, object] = {}
    article = generate_article(
        _topic(),
        plan_angles(1)[0],
        "????",
        "????",
        800,
        _profile(),
        research_bundle={"sources": []},
        generation_stats=stats,
    )
    assert article["recommended_status"] in {"review_required", "warning"}
    assert article.get("content_warning_code") == "CONTENT_TOO_SHORT"
    assert stats["text_generation_calls"] in {1, 2}
    if stats["text_generation_calls"] == 2:
        assert stats["text_generation_second_call_reason"] == "CONTENT_TOO_SHORT_REWRITE"


def test_json_repair_uses_invalid_output_recovery_call(monkeypatch):
    def fake_generate(self, prompt: str, temperature: float = 0.8, max_tokens: int = 3000) -> str:
        return json.dumps({"title": "???", "intro": "??", "sections": [{"heading": "???", "body": "??", "image_brief": "?"}]}, ensure_ascii=False)

    monkeypatch.setattr("generation.article_generator.OpenAITextProvider.generate", fake_generate)
    stats: dict[str, object] = {}
    with pytest.raises(ProviderError) as exc:
        generate_article(
            _topic(),
            plan_angles(1)[0],
            "????",
            "????",
            800,
            _profile(),
            research_bundle={"sources": []},
            generation_stats=stats,
        )
    assert exc.value.code == "MODEL_OUTPUT_INVALID"
    assert stats["text_generation_calls"] == 2
    assert stats["text_generation_call_reasons"] == ["INITIAL_GENERATION", "INVALID_OUTPUT_RECOVERY"]


def test_source_list_normalizes_dict_and_serialized_dict():
    value = [
        {
            "source_id": "S1",
            "publisher": "国家航天局",
            "title": "嫦娥六号任务进展",
            "date": "2024-06-25",
            "url": "https://example.com/1",
        },
        "{'source_id': 'S2', 'title': '月背样品研究', 'publisher': '新华社', 'date': '2024-06-28', 'url': 'https://example.com/2'}",
    ]
    result = normalize_source_list(value)
    assert result == [
        "[1] 国家航天局：《嫦娥六号任务进展》，2024-06-25\n原文链接：https://example.com/1",
        "[2] 新华社：《月背样品研究》，2024-06-28\n原文链接：https://example.com/2",
    ]


def test_hf2_1_release_copy_and_streamlit_bind_are_fixed():
    build_source = (ROOT / "scripts" / "build_rc1_3_3_lite_r2_2_7.py").read_text(encoding="utf-8")
    ui_source = (ROOT / "ui" / "rc1_app.py").read_text(encoding="utf-8")
    desktop_source = (ROOT / "desktop_host.py").read_text(encoding="utf-8")
    bootstrapper_source = (ROOT / "packaging" / "setup_bootstrapper.cs").read_text(encoding="utf-8")
    assert "RELEASE =" in build_source
    assert "zip" in build_source
    assert 'uninstaller = install_dir / "unins000.exe"' in build_source
    assert "UsePreviousAppDir=no" in build_source
    assert "文本和图片使用同一个API Key" in ui_source
    assert "接口地址和模型仍分别设置" in ui_source
    assert '"--server.address", "127.0.0.1"' in desktop_source
    assert 'string uninstaller = Path.Combine(installRoot, "unins000.exe");' in bootstrapper_source
    assert "热点图文工作台卸载.exe" not in bootstrapper_source
