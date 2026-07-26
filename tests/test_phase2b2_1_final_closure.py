from pathlib import Path

from generation.angle_planner import plan_angles
from generation.article_generator import _prompt
from generation.similarity import compare_batch_report
from modules.models import HotTopic


ROOT = Path(__file__).resolve().parents[1]
APP_SOURCE = (ROOT / "ui" / "rc1_app.py").read_text(encoding="utf-8")


def sample_topic() -> HotTopic:
    return HotTopic(id="closure-topic", title="城市生活热点", summary="公开摘要", source_url="https://example.com/news")


def test_rewrite_context_enters_prompt():
    angle = plan_angles(1)[0]
    prompt = _prompt(sample_topic(), angle, "热点资讯", "客观通俗", 800, {
        "rewrite_count": 1,
        "conflict_article": {"title": "旧标题结构", "opening": "旧开头内容", "headings": ["旧一", "旧二"]},
        "violations": ["title_similarity", "opening_similarity"],
        "angle_name": "观点评论",
        "opening_strategy": "从普通人影响切入",
        "avoid_expressions": ["值得关注的是"],
    })
    assert "旧标题结构" in prompt
    assert "旧开头内容" in prompt
    assert "旧一、旧二" in prompt
    assert "title_similarity" in prompt
    assert "不得复用旧标题结构" in prompt


def test_first_and_rewrite_prompts_are_different():
    angle = plan_angles(1)[0]
    first = _prompt(sample_topic(), angle, "热点资讯", "客观通俗", 800)
    rewrite = _prompt(sample_topic(), angle, "热点资讯", "客观通俗", 800, {"rewrite_count": 1, "conflict_article": {"title": "旧", "opening": "旧开头"}, "violations": ["body_similarity"]})
    assert first != rewrite
    assert "这不是首次生成" not in first
    assert "不是首次生成" in rewrite


def test_conflict_title_and_opening_are_explicitly_avoided():
    angle = plan_angles(1)[0]
    prompt = _prompt(sample_topic(), angle, "热点资讯", "客观通俗", 800, {"conflict_article": {"title": "冲突标题", "opening": "冲突开头"}, "rewrite_count": 2})
    assert "冲突标题" in prompt and "冲突开头" in prompt
    assert "调整段落顺序" in prompt
    assert "更换核心论述" in prompt


def test_three_articles_produce_three_pair_results():
    articles = [
        {"title": "新闻事实", "content_markdown": "事实开头一", "sections": [{"heading": "时间线", "body": "事实内容"}]},
        {"title": "社会影响", "content_markdown": "社会开头二", "sections": [{"heading": "影响面", "body": "社会内容"}]},
        {"title": "观点分析", "content_markdown": "观点开头三", "sections": [{"heading": "讨论点", "body": "观点内容"}]},
    ]
    report = compare_batch_report(articles)
    assert report["total_pairs_checked"] == 3
    assert len(report["pairs"]) == 3
    assert all("title_similarity" in pair and "overall_similarity" in pair and "status" in pair for pair in report["pairs"])
    assert "violating_pairs" in report


def test_quality_evidence_fields_are_persisted_by_runtime():
    text = (ROOT / "generation" / "single_task.py").read_text(encoding="utf-8")
    for field in ["article_sha_before", "article_sha_after", "prompt_sha_before", "prompt_sha_after", "cover_prompt_sha"]:
        assert field in text
    assert "rewrite_count" in text


def test_model_page_has_direct_configuration_and_all_presets():
    for provider in ["阿里云百炼", "火山引擎", "智谱 GLM", "DeepSeek", "OpenAI 兼容", "自定义"]:
        assert provider in APP_SOURCE
    assert 'st.text_input("API Key"' in APP_SOURCE
    assert "保存并检测" in APP_SOURCE


def test_model_page_does_not_render_raw_json():
    start = APP_SOURCE.index("def _settings_page")
    end = APP_SOURCE.index("def render_rc1_app", start)
    section = APP_SOURCE[start:end]
    assert "st.json" not in section
    assert "HTTP 状态" not in section
    assert "st.write(\"Provider" not in section


def test_normal_model_errors_are_chinese():
    for message in ["文本模型连接成功", "图片模型连接成功", "API Key 无效", "当前接口不能生成图片", "网络连接异常"]:
        assert message in APP_SOURCE


def test_single_item_cancel_and_retry_controls_exist():
    start = APP_SOURCE.index("def _content")
    end = APP_SOURCE.index("def _settings_page", start)
    section = APP_SOURCE[start:end]
    assert "取消这篇" in section
    assert "重新搜索资料并生成" in section
    assert "重新写文章" in section
    assert "/items/{task_id}/cancel" in section
    assert "/items/{task_id}/retry" in section


def test_review_required_has_regeneration_action():
    assert "这篇内容与其他文章较接近，建议重新生成。" in APP_SOURCE
    assert 'state.get("similarity_status") == "review_required"' in APP_SOURCE


def test_normal_user_terms_do_not_use_batch_word():
    start = APP_SOURCE.index("def render_rc1_app")
    end = len(APP_SOURCE)
    section = APP_SOURCE[start:end]
    assert "我的批次" not in section
    assert "批次名称" not in section
    assert "批次已创建" not in section
    assert "停止这一批" not in section


def test_rewrite_uses_temporary_attempt_directory():
    source = (ROOT / "generation" / "single_task.py").read_text(encoding="utf-8")
    assert ".attempts" in source
    assert "source.replace(target)" in source
    assert "新版本生成失败，当前展示上一版本" in source


def test_old_cover_is_only_shown_when_current_cover_completed():
    start = APP_SOURCE.index("def _content")
    end = APP_SOURCE.index("def _settings_page", start)
    section = APP_SOURCE[start:end]
    assert 'state.get("cover") or {}).get("status") == "completed"' in section


def test_rewrite_context_contains_angle_strategy_fields():
    source = (ROOT / "generation" / "batch_executor.py").read_text(encoding="utf-8")
    assert "opening_strategy" in source
    assert "avoid_expressions" in source
    assert "conflict_article" in source


def test_completed_task_can_be_regenerated_without_new_task_id():
    source = (ROOT / "generation" / "batch_executor.py").read_text(encoding="utf-8")
    assert 'task.get("status") == "completed"' in source
    assert '"rewrite_requested": True' in source
    assert "self._submit_item(batch_id, task_id, step)" in source
