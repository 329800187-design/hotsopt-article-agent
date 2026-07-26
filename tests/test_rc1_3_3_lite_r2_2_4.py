from __future__ import annotations

from pathlib import Path

import pytest

from hot_sources.service import HotTrendService
from modules.models import HotTopic


ROOT = Path(__file__).resolve().parents[1]


def read_text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


class _FakeStore:
    def __init__(self) -> None:
        self.topics = [HotTopic(id=f"topic-{index}", title=f"热点 {index}", source_name="今日头条") for index in range(1, 8)]
        self.basket: list[dict] = []

    def list_topics(self, *args, **kwargs):
        return self.topics

    def get_basket(self):
        return list(self.basket)

    def set_basket(self, topics):
        if len(topics) > 5:
            raise ValueError("TOPIC-SELECT-LIMIT")
        self.basket = list(topics)
        return list(self.basket)


def _service_with_store(store: _FakeStore) -> HotTrendService:
    service = object.__new__(HotTrendService)
    service.store = store
    return service


def test_topic_duplicate_and_limit_errors_pass():
    store = _FakeStore()
    service = _service_with_store(store)
    service.add_to_basket(["topic-1"])
    with pytest.raises(ValueError, match="TOPIC-SELECT-DUPLICATE"):
        service.add_to_basket(["topic-1"])
    service.add_to_basket(["topic-2", "topic-3", "topic-4", "topic-5"])
    with pytest.raises(ValueError, match="TOPIC-SELECT-LIMIT"):
        service.add_to_basket(["topic-6"])


def test_topic_selection_ui_and_error_codes_pass():
    ui = read_text("ui/rc1_app.py")
    api = read_text("api.py")
    assert "选择此热点" in ui
    assert "已加入选题篮" in ui
    assert "清空选题篮" in ui
    assert "TOPIC-SELECT-DUPLICATE" in api
    assert "TOPIC-SELECT-LIMIT" in api
    assert "TOPIC-SELECT-STATE" in api
    assert "TOPIC-REMOVE-FAILED" in api


def test_image_real_test_one_call_preview_timestamp_pass():
    ui = read_text("ui/rc1_app.py")
    api = read_text("api.py")
    provider = read_text("providers/image_provider.py")
    assert "测试图片模型" in ui
    assert "真实测试图片模型" in ui
    assert "开始测试" in ui
    assert "本次将调用图片模型1次" in ui
    assert "自动重试0次" in ui
    assert "/models/image/test-artifact" in ui
    assert "last_image_test_at" in api
    assert "一只白色咖啡杯放在木桌上，纯净背景，不含文字。" in provider
    assert "IMAGE-TEST-401" in ui
    assert "IMAGE-TEST-BALANCE" in ui


def test_auto_research_before_generation_and_zero_image_on_failure_pass():
    source = read_text("generation/single_task.py")
    assert "ResearchService().collect(topic, references=reference_urls, supplemental_text=supplemental_text)" in source
    assert "for round_index in range(1, 3)" in source
    assert '"stage": "collecting_research"' in source
    assert "RESEARCH_NOT_COLLECTED" in source
    assert "INSUFFICIENT_INFORMATION" in source
    assert '"generation_calls": 0' in source
    assert "bundle = _auto_collect_research(state, store, topic)" in source


def test_failed_task_research_regenerate_and_rewrite_guard_pass():
    api = read_text("api.py")
    ui = read_text("ui/rc1_app.py")
    assert "/api/tasks/{task_id}/research-regenerate" in api
    assert "/api/batches/{batch_id}/items/{task_id}/research-regenerate" in api
    assert "_clear_research_bundle_for_task" in api
    assert "重新搜索资料并生成" in ui
    assert "重新写文章" in ui
    assert "当前资料不足，请先重新搜索资料。" in ui
    assert "_rewrite_only_ready" in ui


def test_delete_task_batch_selected_failed_pass():
    api = read_text("api.py")
    db = read_text("modules/database.py")
    ui = read_text("ui/rc1_app.py")
    assert "def delete_task_api" in api
    assert "def delete_batch_api" in api
    assert "def clear_failed_tasks" in api
    assert "def delete_task" in db
    assert "def delete_batch" in db
    assert "def delete_failed_tasks" in db
    assert "删除本次创作" in ui
    assert "删除选中" in ui
    assert "清空全部失败任务" in ui
    assert "delete_exports\": False" in ui


def test_failed_progress_stops_and_reason_visible_pass():
    components = read_text("ui/components.py")
    ui = read_text("ui/rc1_app.py")
    assert 'state.get("status") or "") == "failed"' in components
    assert "生成失败" in components
    assert "失败原因：" in ui
    assert "没有找到足够的相关公开资料" in ui
    assert "文本模型失败" in ui
    assert "文章质量未通过" in ui
    assert "图片模型失败" in ui
    assert "用户取消" in ui


def test_r225_release_identity_and_status_pass():
    assert 'APP_VERSION = "RC1.3.3-Lite-R2.2.8-P1"' in read_text("modules/app_version.py")
    assert 'Version = "RC1.3.3-Lite-R2.2.8-P1"' in read_text("packaging/setup_bootstrapper.cs")
    build = read_text("scripts/build_rc1_3_3_lite_r2_2_7.py")
    assert 'RELEASE = "RC1.3.3-Lite-R2.2.8-P1"' in build
    assert "output_setup" in build
    assert "hotspot-article-agent-rc1-3-3-lite-r2-2-8-p1-source.zip" in build
    assert "RC1.3.3-Lite-R2.2.8-P1 Hermes修复与自检完成，等待用户复测" in build
