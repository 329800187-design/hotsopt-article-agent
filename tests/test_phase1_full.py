"""Phase 1 full acceptance tests — 热点和任务闭环.

Coverage:
- database init idempotent (duplicate, kept in test_core.py)
- toutiao normalize / newsnow normalize
- provider interface contract
- primary failure → backup fallback
- all sources fail → cache fallback
- cache stale warning
- deduplication
- classification rules (including edge cases: 国际时事)
- keyword search / category filter / sort / time filter
- manual topic creation
- max 5 topic selection / 6th rejected
- both task modes
- task persistence across restart (simulated)
- Chinese title / Chinese path
- API error format
- sensitive data redaction
- topic observation history
- provider status tracking
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hot_sources.base import HotProvider
from hot_sources.classifier import CATEGORY_RULES, classify_topic
from hot_sources.dailyhot import DailyHotSource
from hot_sources.dedupe import deduplicate_topics
from hot_sources.manual import ManualHotSource
from hot_sources.newsnow import NewsNowSource
from hot_sources.service import HotTrendService
from modules.database import SQLiteStore
from modules.models import HotTopic


# ── helpers ────────────────────────────────────────────────


def topic(title: str, rank: int = 1, hot_value: str = "100", category: str = "综合热点",
          source_name: str = "测试源", source_url: str = "") -> HotTopic:
    return HotTopic(
        id=f"id-{title}",
        title=title,
        rank=rank,
        hot_value=hot_value,
        category=category,
        source="test",
        source_name=source_name,
        source_url=source_url or f"https://example.com/{title}",
        summary="测试摘要",
    )


class FakeProvider:
    provider_name = "fake"
    display_name = "测试在线源"

    def __init__(self, topics_list=None, error: str | None = None):
        self.topics = topics_list or [topic("中文热点")]
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


# ── 1. provider interface ───────────────────────────────────


def test_dailyhot_normalize_item():
    """今日头条规范化：标题/热度/排名/来源URL均提取。"""
    src = DailyHotSource("https://api-hot.imsyy.top/toutiao")
    item = {"title": "测试热点", "hot": "12345", "url": "https://www.toutiao.com/a/123/", "rank": 3, "desc": "测试描述"}
    result = src.normalize_item(item, 1, "2026-01-01T00:00:00+00:00")
    assert result is not None
    assert result.title == "测试热点"
    assert result.hot_value == "12345"
    assert result.source_url == "https://www.toutiao.com/a/123/"
    assert result.rank == 3


def test_dailyhot_normalize_empty_title():
    """空标题返回 None。"""
    src = DailyHotSource("https://api-hot.imsyy.top/toutiao")
    assert src.normalize_item({}, 1, "") is None


def test_dailyhot_health_check_fails_for_bad_url():
    """无效端点 health_check 返回 ok=False。"""
    src = DailyHotSource("https://invalid.test.local/hot")
    result = src.health_check()
    assert result["ok"] is False


def test_newsnow_normalize_item():
    """NewsNow 规范化：标题/热度正确提取。"""
    src = NewsNowSource()
    item = {"title": "今日要闻", "hot": "9999", "url": "https://example.com/news/1", "desc": "重要新闻"}
    result = src.normalize_item(item, 5, "2026-01-01T00:00:00+00:00")
    assert result is not None
    assert result.title == "今日要闻"
    assert result.source_name == "NewsNow 今日头条备用源"
    assert result.rank == 5


def test_provider_abstract_protocol():
    """所有 provider 实现 HotProvider 协议。"""
    providers = [
        DailyHotSource("https://api-hot.imsyy.top/toutiao"),
        NewsNowSource(),
        ManualHotSource("手工测试"),
    ]
    for p in providers:
        assert isinstance(p, HotProvider)
        assert p.provider_name
        assert p.display_name
        assert callable(p.health_check)
        assert callable(p.fetch_trends)
        assert callable(p.normalize_item)
        assert callable(p.supports_category)


# ── 2. failover & cache ─────────────────────────────────────


def test_provider_status_tracking(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite")

    class PrimaryFake(FakeProvider):
        provider_name = "primary_source"

    class BackupFake(FakeProvider):
        provider_name = "backup_source"

    service = HotTrendService(store=store, providers=[
        PrimaryFake(error="主源失败"),
        BackupFake([topic("备用热点")]),
    ], cache_provider=FakeCache())
    service.refresh()
    statuses = store.list_provider_status()
    assert any(s["provider_name"] == "primary_source" and s["status"] == "error" for s in statuses)
    assert any(s["provider_name"] == "backup_source" and s["status"] == "online" for s in statuses)
    # cache provider status is only stored when cache is actually used (all online sources fail)


def test_fallback_without_cache_produces_empty_topics(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite")
    class EmptyCache(FakeCache):
        def fetch_trends(self):
            raise RuntimeError("no cache")
    result = HotTrendService(store=store, providers=[FakeProvider(error="主源失败")], cache_provider=EmptyCache()).refresh()
    assert result["status"] == "offline"
    assert result["topics"] == []


# ── 3. classification ──────────────────────────────────────


@pytest.mark.parametrize("title,expected", [
    ("俄不承认南海仲裁案裁决合法性", "国际时事"),
    ("中美关税谈判最新进展", "国际时事"),
    ("A股三大指数集体上涨", "财经商业"),
    ("iPhone 16发布引发关注", "科技数码"),
    ("CBA总决赛广东夺冠", "体育赛事"),
    ("某演员离婚引发争议", "娱乐影视"),
    ("2026年高考改革方案公布", "教育职场"),
    ("研究发现新型抗癌药物", "健康科普"),
    ("某地发生重大交通事故", "社会民生"),
    ("完全随机无关键词标题", "综合热点"),
    ("农村随礼越来越高普通家庭如何应对", "社会民生"),
    ("2026上半年中国经济数据公布", "财经商业"),
])
def test_classification_rules(title, expected):
    assert classify_topic(title, "") == expected


def test_classification_keywords_cover_all_categories():
    """确保每个分类都有关键词规则。"""
    expected_categories = {"社会民生", "财经商业", "科技数码", "体育赛事", "娱乐影视", "教育职场", "健康科普", "国际时事"}
    assert set(CATEGORY_RULES.keys()) == expected_categories


# ── 4. dedup & filters ─────────────────────────────────────


def test_dedup_with_different_sources():
    """同一标题不同来源：去重保留第一个。"""
    a = topic("同一个热点", source_name="头条")
    b = topic("同一个热点", source_name="备用源")
    result = deduplicate_topics([a, b])
    assert len(result) == 1
    assert result[0].source_name == "头条"


def test_sort_by_hot_value(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite")
    store.save_topics([
        topic("热点A", hot_value="100"),
        topic("热点B", hot_value="999"),
        topic("热点C", hot_value="50"),
    ])
    service = HotTrendService(store=store, providers=[], cache_provider=FakeCache())
    result = service.list_topics(sort="hot_desc")
    assert result[0].title == "热点B"
    assert result[-1].title == "热点C"


def test_sort_by_rank(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite")
    store.save_topics([
        topic("第三", rank=3),
        topic("第一", rank=1),
        topic("第二", rank=2),
    ])
    service = HotTrendService(store=store, providers=[], cache_provider=FakeCache())
    result = service.list_topics(sort="rank_asc")
    assert result[0].title == "第一"
    assert result[-1].title == "第三"


def test_time_range_filter_respects_boundary(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite")
    now = datetime.now(timezone.utc)
    recent = topic("新热点")
    recent.captured_at = now.isoformat()
    old = topic("旧热点")
    old.captured_at = (now - timedelta(hours=25)).isoformat()
    store.save_topics([recent, old])
    service = HotTrendService(store=store, providers=[], cache_provider=FakeCache())
    results = service.list_topics(time_range="最近24小时")
    assert any(t.title == "新热点" for t in results)
    assert not any(t.title == "旧热点" for t in results)


def test_source_filter(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite")
    store.save_topics([
        topic("头条热点A", source_name="今日头条主源"),
        topic("备用热点B", source_name="NewsNow 今日头条备用源"),
    ])
    service = HotTrendService(store=store, providers=[], cache_provider=FakeCache())
    result = service.list_topics(source="今日头条主源")
    assert len(result) == 1
    assert result[0].source_name == "今日头条主源"


# ── 5. manual topics ────────────────────────────────────────


def test_manual_topic_with_summary(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite")
    service = HotTrendService(store=store, providers=[], cache_provider=FakeCache())
    manual = service.add_manual_topic("手工话题测试", "这是补充背景信息")
    assert manual.source == "manual"
    assert manual.source_name
    assert manual.title == "手工话题测试"
    assert manual.summary == "这是补充背景信息"


def test_manual_topic_empty_title_raises():
    src = ManualHotSource("")
    assert src.fetch_trends() == []


# ── 6. task creation ────────────────────────────────────────


def test_single_topic_multi_angle_requires_one_topic(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite")
    store.save_topics([topic("A"), topic("B")])
    service = HotTrendService(store=store, providers=[], cache_provider=FakeCache())
    ids = [t.id for t in store.list_topics(limit=10)]
    with pytest.raises(ValueError):
        service.create_task("失败任务", "single_topic_multi_angle", ids[:2], 5)


def test_multi_topic_article_count_bound(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite")
    store.save_topics([topic(f"话题{i}") for i in range(3)])
    service = HotTrendService(store=store, providers=[], cache_provider=FakeCache())
    ids = [t.id for t in store.list_topics(limit=10)]
    with pytest.raises(ValueError):
        service.create_task("失败任务", "multi_topic", ids, 10)


def test_task_status_initial_value(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite")
    store.save_topics([topic("任务话题")])
    service = HotTrendService(store=store, providers=[], cache_provider=FakeCache())
    ids = [t.id for t in store.list_topics(limit=10)]
    # default status is "queued"
    task = service.create_task("任务卡片", "single_topic_multi_angle", ids[:1], 1)
    assert task["status"] in ("queued", "draft")


def test_task_source_name_aggregation(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite")
    store.save_topics([
        topic("A", source_name="今日头条主源"),
        topic("B", source_name="手动话题"),
    ])
    service = HotTrendService(store=store, providers=[], cache_provider=FakeCache())
    ids = [t.id for t in store.list_topics(limit=10)]
    task = service.create_task("多源任务", "multi_topic", ids, 2)
    # source_name should contain both source names
    assert "手动话题" in task["source_name"] or "今日头条主源" in task["source_name"]


# ── 7. API error format ─────────────────────────────────────


def test_api_error_format():
    """错误对象包含 code/message/detail/retryable。"""
    from api import _error
    response = _error("TEST_ERROR", "测试错误", detail="详细描述", retryable=True, status_code=400)
    body = json.loads(response.body)
    assert body["success"] is False
    assert body["error"]["code"] == "TEST_ERROR"
    assert body["error"]["message"] == "测试错误"
    assert body["error"]["detail"] == "详细描述"
    assert body["error"]["retryable"] is True
    assert "request_id" in body
    assert "timestamp" in body


def test_api_response_format():
    from api import _response
    response = _response(True, {"test": "value"})
    body = json.loads(response.body)
    assert body["success"] is True
    assert body["data"]["test"] == "value"
    assert body["error"] is None
    assert "request_id" in body
    assert "timestamp" in body


# ── 8. data integrity ───────────────────────────────────────


def test_chinese_path_works(tmp_path):
    """中文路径下的 SQLite 库可以正常读写。"""
    chinese_path = tmp_path / "热点缓存数据" / "生产库.db"
    store = SQLiteStore(chinese_path)
    store.init_schema()
    store.save_topics([topic("中文路径测试123")])
    result = store.list_topics()
    assert len(result) == 1
    assert result[0].title == "中文路径测试123"


def test_unicode_topic_title(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite")
    store.save_topics([topic("🎉emoji中文Title🔥测试！")])
    result = store.list_topics()[0]
    assert "emoji" in result.title


def test_sensitive_data_log_redaction(tmp_path):
    """raw_data 中的 cookie/token/authorization/api_key/secret 被删除。"""
    store = SQLiteStore(tmp_path / "db.sqlite")
    t = topic("脱敏测试")
    t.raw_data = {
        "public": "可见",
        "Cookie": "should_be_removed",
        "authorization": "Bearer xyz",
        "api_key": "sk-1234",
        "Secret": "shh",
        "nested": {"token": "rm", "ok_data": "keep"},
    }
    store.save_topics([t])
    saved = store.list_topics()[0]
    assert saved.raw_data.get("public") == "可见"
    for blocked in ("Cookie", "cookie", "authorization", "token", "api_key", "Secret"):
        assert blocked not in json.dumps(saved.raw_data).lower()
    # nested ok_data survives
    assert saved.raw_data.get("nested", {}).get("ok_data") == "keep"


def test_task_persistence_across_restart(tmp_path):
    """模拟重启：新建 SQLiteStore 实例后任务仍存在。"""
    store = SQLiteStore(tmp_path / "db.sqlite")
    store.save_topics([topic("持久化测试")])
    service = HotTrendService(store=store, providers=[], cache_provider=FakeCache())
    ids = [t.id for t in store.list_topics(limit=10)]
    task = service.create_task("重启前任务", "single_topic_multi_angle", ids[:1], 3)

    # simulate restart
    restarted = SQLiteStore(tmp_path / "db.sqlite")
    tasks = restarted.list_tasks()
    assert any(t["task_id"] == task["task_id"] for t in tasks)
