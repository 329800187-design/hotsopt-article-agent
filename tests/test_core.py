from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hot_sources.dedupe import deduplicate_topics
from hot_sources.manual import ManualHotSource
from hot_sources.service import HotTrendService
from hot_sources.toutiao_official import ToutiaoOfficialSource
from modules.database import SQLiteStore
from modules.models import HotTopic
from modules.topic_cache import CACHE_PATH, save_topics


def topic(title: str, rank: int = 1, hot_value: str = "100") -> HotTopic:
    return HotTopic(id=f"id-{title}", title=title, rank=rank, hot_value=hot_value, category="综合热点", source="test", source_name="测试源", source_url=f"https://example.com/{title}", summary="测试摘要")


class FakeProvider:
    provider_name = "fake"
    display_name = "测试在线源"

    def __init__(self, topics=None, error: str | None = None):
        self.topics = topics or [topic("中文热点")]
        self.error = error
        self.last_success_at = None
        self.last_error = None

    def fetch_trends(self):
        if self.error:
            self.last_error = self.error
            raise RuntimeError(self.error)
        self.last_success_at = datetime.now(timezone.utc).isoformat()
        return self.topics

    def supports_category(self, category):
        return category in {"社会民生", "财经商业", "科技数码", "体育赛事", "娱乐影视", "教育职场", "健康科普", "国际时事", "综合热点"}


class FakeCache(FakeProvider):
    provider_name = "local_cache"
    display_name = "本地缓存"

    def fetch_trends(self):
        values = [item for item in self.topics]
        for item in values:
            item.is_cached = True
            item.provider_status = "cached"
        return values


def test_database_initialization_is_idempotent(tmp_path):
    store = SQLiteStore(tmp_path / "中文目录" / "data.db")
    store.init_schema()
    store.init_schema()
    assert store.db_path.exists()


def test_toutiao_official_normalization():
    page = '<script>{"QueryWord":"中文官方热点","HotValue":"12345","ClusterIdStr":"987"}</script>'
    items = ToutiaoOfficialSource._items(page)
    assert items[0]["QueryWord"] == "中文官方热点"
    topic_value = ToutiaoOfficialSource().normalize_item(items[0], 1, datetime.now(timezone.utc).isoformat())
    assert topic_value is not None
    assert topic_value.source_url.endswith("/987/")


def test_primary_failure_falls_back_to_backup(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite")
    result = HotTrendService(store=store, providers=[FakeProvider(error="主源失败"), FakeProvider([topic("备用热点")])], cache_provider=FakeCache()).refresh()
    assert result["status"] == "online"
    assert result["display_name"] == "测试在线源"
    assert result["topics"][0].title == "备用热点"
    assert "主源失败" in result["errors"][0]


def test_all_online_failures_use_cache(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite")
    cache_topic = topic("缓存热点")
    result = HotTrendService(store=store, providers=[FakeProvider(error="主源失败"), FakeProvider(error="备用失败")], cache_provider=FakeCache([cache_topic])).refresh()
    assert result["status"] == "cached"
    assert result["is_cached"] is True
    assert result["topics"][0].title == "缓存热点"


def test_cache_expired_warning(monkeypatch, tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite")
    cache_topic = topic("过期缓存")
    old_time = (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat()
    cache_topic.captured_at = old_time
    fake_cache = FakeCache([cache_topic])
    monkeypatch.setattr(HotTrendService, "cache_age_seconds", lambda self: 12 * 60 * 60)
    result = HotTrendService(store=store, providers=[FakeProvider(error="主源失败")], cache_provider=fake_cache).refresh()
    assert result["stale"] is True
    assert "过期" in result["last_error"]


def test_dedupe_and_classification():
    first = topic("人工智能改变就业", 1)
    first.category = "科技数码"
    second = topic("人工智能改变就业！", 2)
    assert len(deduplicate_topics([first, second])) == 1
    manual = ManualHotSource("农村随礼问题", "家庭讨论")
    assert manual.fetch()[0].category == "社会民生"


def test_filters_sort_and_time_range(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite")
    old = topic("旧热点", 2, "10")
    old.captured_at = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
    recent = topic("科技新热点", 1, "999")
    recent.category = "科技数码"
    store.save_topics([old, recent])
    service = HotTrendService(store=store, providers=[], cache_provider=FakeCache())
    assert service.list_topics("科技", "科技数码")[0].title == "科技新热点"
    assert service.list_topics(sort="hot_desc")[0].title == "科技新热点"
    assert all(item.title != "旧热点" for item in service.list_topics(time_range="最近24小时"))


def test_manual_topic_and_maximum_five_selection(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite")
    values = [topic(f"话题{i}", i) for i in range(1, 7)]
    store.save_topics(values)
    service = HotTrendService(store=store, providers=[], cache_provider=FakeCache())
    assert len(service.select_topics([item.id for item in values[:5]])) == 5
    with pytest.raises(ValueError):
        service.select_topics([item.id for item in values])


def test_both_task_modes_and_persistence(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite")
    values = [topic("任务话题1"), topic("任务话题2")]
    store.save_topics(values)
    service = HotTrendService(store=store, providers=[], cache_provider=FakeCache())
    multi = service.create_task("多热点任务", "multi_topic", [item.id for item in values], 2)
    single = service.create_task("单热点任务", "single_topic_multi_angle", [values[0].id], 5)
    restarted = SQLiteStore(tmp_path / "db.sqlite")
    tasks = restarted.list_tasks()
    assert {item["task_id"] for item in tasks} == {multi["task_id"], single["task_id"]}
    with pytest.raises(ValueError):
        service.create_task("非法任务", "multi_topic", [values[0].id], 2)


def test_sensitive_raw_data_is_redacted(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite")
    item = topic("脱敏热点")
    item.raw_data = {"title": "脱敏热点", "cookie": "secret", "nested": {"token": "secret", "ok": 1}}
    store.save_topics([item])
    saved = store.list_topics()[0]
    assert "cookie" not in json.dumps(saved.raw_data)
    assert "token" not in json.dumps(saved.raw_data)


def test_topic_observations_keep_history(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite")
    first = topic("历史热点")
    second = topic("历史热点")
    second.captured_at = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
    store.save_topics([first])
    store.save_topics([second])
    with store.connect() as connection:
        count = connection.execute("SELECT COUNT(*) FROM hot_topic_observations WHERE topic_id=?", (first.id,)).fetchone()[0]
    assert count == 2
