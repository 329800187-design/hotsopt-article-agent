from pathlib import Path

import pytest

from hot_sources.platforms import normalize_topic_platform
from modules.database import SQLiteStore
from modules.models import HotTopic


def test_navigation_uses_sidebar_value_and_focuses_created_batch():
    source = Path("ui/rc1_app.py").read_text(encoding="utf-8")
    assert '"我的内容": "▣ 我的内容"' in source
    assert 'focus_batch_id=str(batch.get("batch_id") or "")' in source
    assert 'st.session_state["rc1_navigation_target"] = _NAVIGATION_OPTIONS.get(page' in source


def test_platform_metadata_keeps_source_platform_separate_from_channel():
    topic = HotTopic(
        id="platform-topic",
        title="真实平台字段",
        source_name="今日热榜 多平台聚合",
        source_url="https://s.weibo.com/weibo?q=test",
        raw_data={"platform": "微博", "board": "热搜"},
    )
    provider = type("Provider", (), {"provider_name": "tophub_overview", "display_name": "今日热榜 多平台聚合"})()
    normalized = normalize_topic_platform(topic, provider)
    assert normalized.source_name == "微博"
    assert normalized.raw_data["source_platform"] == "微博"
    assert normalized.raw_data["acquisition_channel"] == "今日热榜"
    assert normalized.raw_data["aggregated_platforms"] == ["微博"]
    assert normalized.to_dict()["source_platform"] == "微博"


def _topics(count: int) -> list[dict]:
    return [HotTopic(id=f"url-{index}", title=f"链接文章 {index}", source_url=f"https://example.com/{index}").to_dict() for index in range(count)]


def test_url_batch_accepts_twenty_children_without_widening_normal_batch(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite")
    batch = store.create_batch("20链接", "multi_topic", _topics(20), {"url_batch": True, "image_plan_mode": "none"}, 3)
    assert batch["total_count"] == 20
    assert len(batch["items"]) == 20
    with pytest.raises(ValueError, match="1 到 5"):
        store.create_batch("普通批次", "multi_topic", _topics(6), {"image_plan_mode": "none"}, 2)


def test_url_batch_ui_and_api_have_twenty_limit():
    ui = Path("ui/rc1_app.py").read_text(encoding="utf-8")
    api = Path("api.py").read_text(encoding="utf-8")
    assert "unique_lines[:20]" in ui
    assert 'MAX_BATCH_URLS = 20' in api
    assert "max_length=MAX_BATCH_URLS" in api
    assert 'options["url_batch"] = True' in api
