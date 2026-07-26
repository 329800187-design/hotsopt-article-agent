from __future__ import annotations

from pathlib import Path

from generation.article_generator import _source_block
from generation.content_quality import quality_gate


ROOT = Path(__file__).resolve().parents[1]


def read_text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _section(source: str, start: str, end: str) -> str:
    left = source.index(start)
    right = source.index(end, left)
    return source[left:right]


def _normal_bundle() -> dict:
    return {
        "research_status": "sufficient",
        "accepted_source_count": 2,
        "usable_fact_count": 3,
        "official_or_reliable_source_count": 0,
        "key_organizations": ["某公司"],
        "sources": [
            {
                "source_id": "s1",
                "source_name": "甲媒体",
                "publisher_id": "a.example",
                "domain": "a.example",
                "fetch_success": True,
                "accepted_for_research": True,
                "content": "某公司发布新款手机。新手机支持折叠屏。",
                "summary": "某公司发布新款手机。新手机支持折叠屏。",
            },
            {
                "source_id": "s2",
                "source_name": "乙媒体",
                "publisher_id": "b.example",
                "domain": "b.example",
                "fetch_success": True,
                "accepted_for_research": True,
                "content": "发布会今天举行。",
                "summary": "发布会今天举行。",
            },
        ],
        "usable_facts": [
            {"fact_id": "f1", "canonical_fact": "某公司发布新款手机。", "supporting_source_ids": ["s1"], "verification_type": "single_source"},
            {"fact_id": "f2", "canonical_fact": "发布会今天举行。", "supporting_source_ids": ["s2"], "verification_type": "single_source"},
            {"fact_id": "f3", "canonical_fact": "新手机支持折叠屏。", "supporting_source_ids": ["s1"], "verification_type": "single_source"},
        ],
        "single_source_facts": [],
        "verified_facts": [],
    }


def _article(extra: str = "") -> dict:
    content = "据甲媒体报道，某公司发布新款手机。根据乙媒体报道，发布会今天举行。据甲媒体报道，新手机支持折叠屏。" + extra
    return {
        "content_markdown": content,
        "word_count": 0,
        "fact_basis": [
            {"fact_id": "f1", "fact": "某公司发布新款手机。", "source_ids": ["s1"]},
            {"fact_id": "f2", "fact": "发布会今天举行。", "source_ids": ["s2"]},
            {"fact_id": "f3", "fact": "新手机支持折叠屏。", "source_ids": ["s1"]},
        ],
    }


def test_topic_page_simplified_and_research_internals_removed_pass():
    ui = read_text("ui/rc1_app.py")
    topics_section = _section(ui, "def render_topics", "def render_start")
    assert "刷新今日热点" in topics_section
    assert "分类" in topics_section
    assert "选择此热点" in topics_section
    assert "移除" in topics_section
    assert "清空选题篮" in topics_section
    assert "下一步" in topics_section
    forbidden = ["补充参考链接", "补充资料", "重新采集所选话题资料", "查看资料卡", "多来源一致信息", "官方信息", "单一来源信息", "存在争议信息"]
    assert not any(item in topics_section for item in forbidden)


def test_research_runs_inside_generation_and_progress_is_user_friendly_pass():
    single_task = read_text("generation/single_task.py")
    components = read_text("ui/components.py")
    assert "bundle = _auto_collect_research(state, store, topic)" in single_task
    assert "for round_index in range(1, 3)" in single_task
    assert "正在查找资料，已用时" in components
    assert "已找到 {accepted} 个可用来源" in components
    assert "正在整理事件信息……" in components
    assert "正在生成正文……" in components
    assert "正在检查内容……" in components


def test_normal_article_lower_gate_allows_two_sources_three_usable_facts_pass():
    result = quality_gate(_article(), _normal_bundle())
    assert result["status"] == "passed", result["reasons"]


def test_unsupported_concrete_claim_still_blocks_pass():
    result = quality_gate(_article("另有消息称投入300亿元。"), _normal_bundle())
    assert result["status"] == "failed"
    assert any(str(reason).startswith("正文具体陈述缺少来源资料支持") for reason in result["reasons"])
    assert any("投入300亿元" in item for item in result["metrics"]["fact_trace"]["unsupported_concrete_claims"])


def test_single_source_prompt_requires_attribution_and_hides_research_jargon_pass():
    block = _source_block(_normal_bundle())
    assert "必须写明来源归属" in block
    assert "verification_type" not in block
    assert "independent_publishers" not in block
    prompt = read_text("generation/article_generator.py")
    assert "单一来源信息可以写入正文" in prompt
    assert "据XX媒体报道" in prompt
    assert "不得创造资料中不存在的 fact_id" in prompt


def test_supplement_entry_only_after_insufficient_and_brief_removed_pass():
    ui = read_text("ui/rc1_app.py")
    topics_section = _section(ui, "def render_topics", "def render_start")
    assert "补充参考资料" not in topics_section
    assert "补充参考资料" in ui
    assert "当前公开资料较少" in ui
    assert "使用补充资料重新生成" in ui
    assert "生成300字简讯" not in ui


def test_failed_actions_simplified_and_default_zero_retry_pass():
    ui = read_text("ui/rc1_app.py")
    executor = read_text("generation/executor.py")
    assert "重新搜索资料并生成" in ui
    assert "重新写文章" in ui
    assert "删除" in ui
    assert "当前资料不足，请先重新搜索资料。" in ui
    assert 'settings.get("max_auto_retries", 0)' in executor


def test_r225_identity_and_final_status_pass():
    assert 'APP_VERSION = "RC1.3.3-Lite-R2.2.8-P1"' in read_text("modules/app_version.py")
    assert 'Version = "RC1.3.3-Lite-R2.2.8-P1"' in read_text("packaging/setup_bootstrapper.cs")
    build = read_text("scripts/build_rc1_3_3_lite_r2_2_7.py")
    assert 'RELEASE = "RC1.3.3-Lite-R2.2.8-P1"' in build
    assert "RC1.3.3-Lite-R2.2.8-P1 Hermes修复与自检完成，等待用户复测" in build
