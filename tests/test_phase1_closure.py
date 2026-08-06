from __future__ import annotations

import hashlib
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from generation.angle_planner import plan_angles
from generation.article_generator import generate_article
from hot_sources.classifier import CATEGORIES, classify_topic
from hot_sources.service import HotTrendService
from modules.database import SQLiteStore
from modules.models import HotTopic
from modules.network import classify_network_error, create_http_client, sanitize_proxy_url
from modules.topic_cache import TopicCacheStore
from providers.text_provider import ProviderError


def make_topic(index: int) -> HotTopic:
    return HotTopic(id=f"closure-{index}", title=f"收口话题{index}", source="test", source_name="测试源")


class OnlineProvider:
    provider_name = "closure"
    display_name = "收口在线源"
    last_success_at = None
    last_error = None

    def fetch_trends(self):
        return [make_topic(1)]

    def supports_category(self, category):
        return category in CATEGORIES


class FailingProvider(OnlineProvider):
    def fetch_trends(self):
        raise RuntimeError("测试失败")


def test_test_cache_never_changes_production_cache(tmp_path):
    production_path = ROOT / "data" / "cache" / "latest_topics.json"
    before = hashlib.sha256(production_path.read_bytes()).hexdigest() if production_path.exists() else None
    store = SQLiteStore(tmp_path / "db.sqlite")
    service = HotTrendService(store=store, providers=[OnlineProvider()])
    service.refresh()
    test_cache = store.db_path.parent / "cache" / "latest_topics.json"
    assert test_cache.exists()
    assert TopicCacheStore(test_cache, "test").get_info()["environment"] == "test"
    after = hashlib.sha256(production_path.read_bytes()).hexdigest() if production_path.exists() else None
    assert before == after


def test_cache_environment_isolation(tmp_path):
    path = tmp_path / "cache" / "latest_topics.json"
    TopicCacheStore(path, "test").save([make_topic(1)], "测试源")
    assert TopicCacheStore(path, "production").load() == []
    assert len(TopicCacheStore(path, "test").load()) == 1


def test_source_categories_are_normalized_before_storage(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite")
    item = make_topic(1)
    item.category = "热榜"
    item.title = "iPhone 发布引发关注"
    store.save_topics([item])
    assert store.list_topics()[0].category == "科技数码"
    assert classify_topic("财经市场最新消息", source_category="财经") == "财经商业"
    assert classify_topic("人工智能新品", source_category="科技") == "科技数码"


def test_network_client_modes_and_redaction():
    system = create_http_client({"mode": "system"})
    direct = create_http_client({"mode": "direct"})
    custom = create_http_client({"mode": "custom", "https_proxy": "http://user:password@example.com:8080"})
    try:
        assert system._trust_env is True
        assert direct._trust_env is False
        assert custom._trust_env is False
        assert "user:password" not in sanitize_proxy_url("http://user:password@example.com:8080")
        assert classify_network_error(httpx.ConnectError("[Errno 11001] getaddrinfo failed"))["category"] == "dns"
    finally:
        system.close()
        direct.close()
        custom.close()


def test_production_without_model_key_fails():
    with pytest.raises(ProviderError, match="MODEL_NOT_CONFIGURED"):
        generate_article(make_topic(1), plan_angles(1)[0], "热点资讯", "客观通俗", 800, {}, demo_mode=False)


def test_demo_requires_explicit_mode():
    demo = generate_article(make_topic(1), plan_angles(1)[0], "热点资讯", "客观通俗", 800, {}, demo_mode=True, app_mode="demo")
    assert demo["demo_mode"] is True


def test_mixed_basket_persists_and_limits(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite")
    store.save_topics([make_topic(i) for i in range(1, 7)])
    service = HotTrendService(store=store, providers=[], cache_store=TopicCacheStore(tmp_path / "cache.json", "test"))
    items = service.add_to_basket([f"closure-{i}" for i in range(1, 4)])
    assert len(items) == 3
    manual = HotTopic(id="manual", title="手动话题", source="manual", source_name="手动话题")
    store.save_topics([manual])
    assert len(service.add_to_basket(["manual"])) == 4
    with pytest.raises(ValueError):
        service.add_to_basket([f"closure-{i}" for i in range(4, 7)])
    assert len(SQLiteStore(tmp_path / "db.sqlite").get_basket()) == 4


def test_concurrent_task_writes(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite")
    topics = [make_topic(1)]
    store.save_topics(topics)
    service = HotTrendService(store=store, providers=[], cache_store=TopicCacheStore(tmp_path / "cache.json", "test"))

    def create(index):
        return service.create_task(f"并发任务{index}", "single_topic_multi_angle", [topics[0].id], 5)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(create, range(8)))
    assert len(store.list_tasks()) == 8
