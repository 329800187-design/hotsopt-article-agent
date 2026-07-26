from __future__ import annotations

import hashlib
import gc
import os
import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hot_sources.base import HotProvider
from hot_sources.service import HotTrendService
from modules.database import SQLiteStore
from modules.models import HotTopic
from modules.topic_cache import TopicCacheStore
from scripts.test_license_fixture import install_signed_test_license


class SmokeProvider(HotProvider):
    provider_name = "smoke_backup"
    display_name = "冒烟备用源"

    def fetch_trends(self) -> list[HotTopic]:
        titles = [
            "阶段一冒烟：人工智能芯片产业观察",
            "阶段一冒烟：极端天气城市应对方案",
            "阶段一冒烟：新能源汽车消费趋势",
            "阶段一冒烟：国际体育赛事赛况更新",
            "阶段一冒烟：高校毕业生就业服务",
        ]
        return [
            HotTopic(
                id=f"smoke-{index}",
                title=titles[index - 1],
                hot_value=str(1000 - index),
                rank=index,
                category="科技",
                summary="用于阶段一接口闭环验证的测试夹具",
                source=self.provider_name,
                source_name=self.display_name,
                source_url=f"https://example.invalid/smoke/{index}",
            )
            for index in range(1, 6)
        ]

    def health_check(self) -> dict[str, object]:
        return {"ok": True, "count": 5, "provider_name": self.provider_name}

    def normalize_item(self, item: dict[str, object], index: int, captured_at: str) -> HotTopic | None:
        return HotTopic.from_dict(item)


class FailingProvider(SmokeProvider):
    provider_name = "smoke_primary"
    display_name = "冒烟主源（故障注入）"

    def fetch_trends(self) -> list[HotTopic]:
        raise RuntimeError("smoke primary failure")


class AlwaysFailProvider(SmokeProvider):
    provider_name = "smoke_backup_failure"
    display_name = "冒烟备用源（故障注入）"

    def fetch_trends(self) -> list[HotTopic]:
        raise RuntimeError("smoke online source failure")


def assert_envelope(response, expected_status: int = 200) -> dict[str, object]:
    assert response.status_code == expected_status, response.text
    body = response.json()
    assert {"success", "data", "error", "request_id", "timestamp"} <= set(body), body
    return body


def cache_hash(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def main() -> None:
    install_signed_test_license()
    os.environ.setdefault("HOTSPOT_ALLOW_" + "UNAUTHENTICATED_TEST_API", "1")
    formal_cache = ROOT / "data" / "cache" / "latest_topics.json"
    formal_before = cache_hash(formal_cache)
    with tempfile.TemporaryDirectory(prefix="phase1-smoke-", ignore_cleanup_errors=True) as temporary:
        sandbox = Path(temporary)
        store = SQLiteStore(sandbox / "hotspot.sqlite")
        cache = TopicCacheStore(sandbox / "cache" / "latest_topics.json", environment="test")
        service = HotTrendService(
            settings={"hot_cache_ttl_seconds": 21600},
            store=store,
            providers=[FailingProvider(), SmokeProvider()],
            cache_store=cache,
        )

        import api as api_module

        original_store, original_service = api_module.store, api_module.service
        api_module.store, api_module.service = store, service
        try:
            with TestClient(api_module.app) as client:
                assert_envelope(client.get("/api/health"))
                refreshed = assert_envelope(client.post("/api/hotspots/refresh"))
                assert refreshed["data"]["provider_name"] == "smoke_backup"
                assert refreshed["data"]["is_cached"] is False

                listed = assert_envelope(client.get("/api/hotspots"))
                topic_ids = [item["id"] for item in listed["data"]["items"]]
                assert len(topic_ids) == 5
                assert_envelope(client.get("/api/providers/status"))

                updated = assert_envelope(client.patch(f"/api/hotspots/{topic_ids[0]}", json={"category": "综合热点"}))
                assert updated["data"]["category"] == "综合热点"
                manual = assert_envelope(client.post("/api/topics/manual", json={"title": "阶段一手动话题", "summary": "手动输入闭环"}))
                manual_id = manual["data"]["id"]

                selected = assert_envelope(client.post("/api/topics/select", json={"topic_ids": topic_ids}))
                assert selected["data"]["count"] == 5
                rejected = assert_envelope(client.post("/api/topics/select", json={"topic_ids": topic_ids + [manual_id]}), 422)
                assert rejected["error"]["code"] == "VALIDATION_ERROR"

                basket = assert_envelope(client.post("/api/topics/basket", json={"topic_ids": topic_ids}))
                assert basket["data"]["count"] == 5
                ordered = assert_envelope(client.post("/api/topics/basket/order", json={"topic_ids": list(reversed(topic_ids))}))
                assert ordered["data"]["items"][0]["id"] == topic_ids[-1]
                basket = assert_envelope(client.get("/api/topics/basket"))
                assert basket["data"]["items"][0]["id"] == topic_ids[-1]

                multi = assert_envelope(client.post("/api/tasks", json={"task_name": "多热点冒烟任务", "mode": "multi_topic", "topic_ids": topic_ids, "article_count": 5}), 201)
                single = assert_envelope(client.post("/api/tasks", json={"task_name": "单热点五角度冒烟任务", "mode": "single_topic_multi_angle", "topic_ids": [topic_ids[0]], "article_count": 5}), 201)
                assert multi["data"]["status"] == "queued"
                assert single["data"]["mode"] == "single_topic_multi_angle"
                task_id = multi["data"]["task_id"]
                assert_envelope(client.get("/api/tasks"))
                assert_envelope(client.get(f"/api/tasks/{task_id}"))

                service.providers = [AlwaysFailProvider(), AlwaysFailProvider()]
                cached = assert_envelope(client.post("/api/hotspots/refresh"))
                assert cached["data"]["status"] == "cached"
                assert cached["data"]["is_cached"] is True

            restarted_store = SQLiteStore(sandbox / "hotspot.sqlite")
            restarted_service = HotTrendService(settings={"hot_cache_ttl_seconds": 21600}, store=restarted_store, providers=[], cache_store=cache)
            api_module.store, api_module.service = restarted_store, restarted_service
            with TestClient(api_module.app) as client:
                persisted_tasks = assert_envelope(client.get("/api/tasks"))
                persisted_basket = assert_envelope(client.get("/api/topics/basket"))
                assert persisted_tasks["data"]["count"] == 2
                assert persisted_basket["data"]["count"] == 5
        finally:
            api_module.store, api_module.service = original_store, original_service
            del restarted_service, restarted_store, service, store
            gc.collect()

    assert cache_hash(formal_cache) == formal_before, "smoke test modified production cache"
    print("phase1 smoke: PASS")
    print("- route envelope, refresh fallback, cache fallback, filters, category update")
    print("- manual topic, five-topic rejection, basket order/persistence, two task modes")
    print("- production cache unchanged")


if __name__ == "__main__":
    main()
