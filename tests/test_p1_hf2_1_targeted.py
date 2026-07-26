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
    paragraph = "嫦娥六号完成月背采样并返回，相关任务过程已经在正文中说明。" * 12
    responses = iter(
        [
            _short_article(paragraph),
            json.dumps(
                {
                    "sections": [
                        {"heading": "补充分析", "body": paragraph, "image_brief": "重复内容"}
                    ]
                },
                ensure_ascii=False,
            ),
        ]
    )

    def fake_generate(self, prompt: str, temperature: float = 0.8, max_tokens: int = 3000) -> str:
        return next(responses)

    monkeypatch.setattr("generation.article_generator.OpenAITextProvider.generate", fake_generate)
    stats: dict[str, object] = {}
    with pytest.raises(ProviderError) as exc:
        generate_article(
            _topic(),
            plan_angles(1)[0],
            "热点资讯",
            "客观通俗",
            800,
            _profile(),
            research_bundle={"sources": []},
            generation_stats=stats,
        )
    assert exc.value.code == "ARTICLE_TOO_SHORT"
    assert stats["text_generation_calls"] == 2
    assert stats["text_generation_second_call_reason"] == "length_extension"


def test_json_repair_consumes_second_call_and_blocks_length_extension(monkeypatch):
    short_paragraph = "正文较短，需要后续补写，但第二次调用已经用于结构修复。" * 8
    responses = iter(
        [
            json.dumps({"title": "坏结构", "intro": "导语", "sections": [{"heading": "唯一段", "body": "太短", "image_brief": "图"}]} , ensure_ascii=False),
            _short_article(short_paragraph),
        ]
    )

    def fake_generate(self, prompt: str, temperature: float = 0.8, max_tokens: int = 3000) -> str:
        return next(responses)

    monkeypatch.setattr("generation.article_generator.OpenAITextProvider.generate", fake_generate)
    stats: dict[str, object] = {}
    with pytest.raises(ProviderError) as exc:
        generate_article(
            _topic(),
            plan_angles(1)[0],
            "热点资讯",
            "客观通俗",
            800,
            _profile(),
            research_bundle={"sources": []},
            generation_stats=stats,
        )
    assert exc.value.code == "ARTICLE_TOO_SHORT"
    assert stats["text_generation_calls"] == 2
    assert stats["text_generation_second_call_reason"] == "json_repair"


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
    assert 'RELEASE = "RC1.3.3-Lite-P1-HF2.1"' in build_source
    assert 'hotspot-article-agent-rc1-3-3-lite-p1-hf2-1-source.zip' in build_source
    assert 'uninstaller = install_dir / "unins000.exe"' in build_source
    assert "UsePreviousAppDir=no" in build_source
    assert 'INSTALL_DIR_NAME = "热点图文批量生产工作台"' in build_source
    assert "文本和图片使用同一个API Key" in ui_source
    assert "接口地址和模型仍分别设置" in ui_source
    assert '"--server.address", "127.0.0.1"' in desktop_source
    assert 'private const string ProductFolderName = "热点图文批量生产工作台";' in bootstrapper_source
    assert 'string uninstaller = Path.Combine(installRoot, "unins000.exe");' in bootstrapper_source
    assert "热点图文工作台卸载.exe" not in bootstrapper_source
