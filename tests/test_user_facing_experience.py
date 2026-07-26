from pathlib import Path

from ui.rc1_app import NORMAL_PAGES, USER_STATUS_LABELS, user_status
from generation.angle_planner import available_angles, plan_angles


ROOT = Path(__file__).resolve().parents[1]
APP_SOURCE = (ROOT / "ui" / "rc1_app.py").read_text(encoding="utf-8")


def test_normal_navigation_uses_simple_chinese_labels():
    for label in NORMAL_PAGES:
        assert label in APP_SOURCE
    assert 'NORMAL_PAGES = ["首页", "选择话题", "开始生成", "我的内容", "模型设置"]' in APP_SOURCE


def test_technical_pages_are_under_advanced_settings():
    assert 'with st.expander("高级设置")' in APP_SOURCE
    assert "quality/retry" in APP_SOURCE


def test_normal_ui_has_no_task_identifiers_in_copy():
    assert "任务 ID" not in APP_SOURCE
    assert "Provider:" not in APP_SOURCE
    assert "traceback" not in APP_SOURCE.lower()


def test_status_labels_are_user_friendly():
    assert user_status("completed") == "已完成"
    assert user_status("partial_success") == "部分完成"
    assert user_status("review_required") == "需要检查"
    assert user_status("unknown") == "处理中"


def test_all_five_angles_have_chinese_names():
    angles = available_angles()
    assert len(angles) == 5
    assert all(item["angle_name"] for item in angles)


def test_angle_defaults_are_different_for_each_count():
    assert len(plan_angles(1)) == 1
    assert len(plan_angles(5)) == 5
    assert len({item["angle_id"] for item in plan_angles(5)}) == 5


def test_user_flow_exposes_r227_generation_modes():
    assert "单热点生成多篇" in APP_SOURCE
    assert "多热点各生成1篇" in APP_SOURCE
    assert "最多 5 篇文章" in APP_SOURCE
    assert "开始生成" in APP_SOURCE


def test_user_flow_exposes_content_and_cover_results():
    assert "我的内容" in APP_SOURCE
    assert "查看全文" in APP_SOURCE
    assert 'caption="封面"' in APP_SOURCE


def test_user_flow_has_friendly_similarity_guidance():
    assert "建议重新生成" in APP_SOURCE


def test_user_flow_hides_backend_error_details():
    assert "差异检查暂未完成" in APP_SOURCE
    assert "当前展示上一版本" not in APP_SOURCE


def test_batch_creation_limits_user_selection_to_five():
    assert "{len(basket)}/5" in APP_SOURCE
    assert "len(basket) >= 5" in APP_SOURCE
    assert "当前版本每次只能创作1个热点" not in APP_SOURCE


def test_batch_creation_sends_r227_modes():
    assert '"multi_topic"' in APP_SOURCE
    assert 'mode = "single_topic_multi_angle"' in APP_SOURCE
    assert 'concurrency = 2' in APP_SOURCE
    assert '"angles": angles' in APP_SOURCE


def test_advanced_settings_explains_local_key_storage():
    assert "密钥不会进入文章、任务或导出文件" in APP_SOURCE
    assert "密钥安全迁移失败，原配置尚未修改，请重新保存模型设置。" in APP_SOURCE


def test_no_word_or_publish_controls_in_user_page():
    assert "Word 精排" not in APP_SOURCE
    assert "自动发布" not in APP_SOURCE


def test_all_backend_statuses_have_a_safe_label():
    expected = {"queued", "running", "completed", "failed", "partial_success", "cancelled"}
    assert expected.issubset(USER_STATUS_LABELS)
