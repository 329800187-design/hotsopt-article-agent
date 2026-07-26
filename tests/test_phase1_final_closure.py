from __future__ import annotations

import json
import re
from pathlib import Path

import httpx
import pytest

from generation.article_generator import generate_article
from generation.angle_planner import plan_angles
from generation.orchestrator import run_batch
from export.zip_exporter import export_zip
from hot_sources.dailyhot import DailyHotSource
from modules.database import SQLiteStore
from modules.hot_score import normalize_hot_score
from modules.models import HotTopic
from modules.network import classify_network_error, redact_sensitive_text
from modules.security import sanitize_sensitive_data
from modules.topic_cache import TopicCacheStore
from providers.text_provider import ProviderError
from hot_sources.service import HotTrendService


ROOT = Path(__file__).resolve().parents[1]


def make_topic(title: str = "最终收口测试") -> HotTopic:
    return HotTopic(id="final-topic", title=title, source="test", source_name="测试源", category="综合热点")


def profiles() -> tuple[dict[str, str], dict[str, str]]:
    return ({"api_key": "text-key", "base_url": "https://example.invalid/v1", "model": "text"}, {"api_key": "image-key", "base_url": "https://example.invalid/v1", "model": "image"})


def test_production_empty_sections_never_falls_back_to_demo(monkeypatch):
    monkeypatch.setattr("generation.article_generator.OpenAITextProvider.generate", lambda *args, **kwargs: json.dumps({"title": "模型标题", "sections": []}))
    text_profile, _ = profiles()
    with pytest.raises(ProviderError, match="MODEL_OUTPUT_INVALID"):
        generate_article(make_topic(), plan_angles(1)[0], "热点资讯", "客观通俗", 800, text_profile, app_mode="production")


def test_production_task_rejects_demo_content(monkeypatch, tmp_path):
    import generation.orchestrator as orchestrator
    import modules.task_store as task_store

    monkeypatch.setattr(orchestrator, "ROOT", tmp_path)
    monkeypatch.setattr(task_store, "TASK_DIR", tmp_path / "tasks")
    monkeypatch.setattr(orchestrator, "generate_article", lambda *args, **kwargs: {"title": "【演示模式】不应完成", "intro": "demo", "sections": [{"heading": "a", "body": "b", "image_brief": "c"}] * 3, "content_markdown": "# demo", "demo_mode": True})

    def fake_image(self, prompt, output_path):
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"fake-image")
        return output_path

    monkeypatch.setattr(orchestrator.OpenAIImageProvider, "generate", fake_image)
    text_profile, image_profile = profiles()
    task = run_batch([make_topic()], text_profile, image_profile, 1, "热点资讯", "客观通俗", "动漫", 800, app_mode="production", demo_mode=False)
    assert task["status"] == "failed"
    assert any(error["code"] == "DEMO_CONTENT_IN_PRODUCTION" for error in task["errors"])
    assert not any(item.get("status") == "completed" and item.get("demo_mode") for item in task["articles"])


def test_end_to_end_empty_sections_fails_even_when_image_provider_succeeds(monkeypatch, tmp_path):
    import generation.orchestrator as orchestrator
    import modules.task_store as task_store

    monkeypatch.setattr(orchestrator, "ROOT", tmp_path)
    monkeypatch.setattr(task_store, "TASK_DIR", tmp_path / "tasks")
    monkeypatch.setattr("generation.article_generator.OpenAITextProvider.generate", lambda *args, **kwargs: json.dumps({"title": "空 sections", "sections": []}))

    image_calls = {"count": 0}

    def fake_image(self, prompt, output_path):
        image_calls["count"] += 1
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"fake-image")
        return output_path

    monkeypatch.setattr(orchestrator.OpenAIImageProvider, "generate", fake_image)
    text_profile, image_profile = profiles()
    task = run_batch([make_topic()], text_profile, image_profile, 1, "热点资讯", "客观通俗", "动漫", 800, app_mode="production", demo_mode=False)
    assert task["status"] == "failed"
    assert any(error["code"] == "MODEL_OUTPUT_INVALID" for error in task["errors"])
    assert image_calls["count"] == 0
    assert not any("演示模式" in str(item.get("title", "")) for item in task["articles"])


def test_sanitizer_handles_nested_structures_consistently(tmp_path):
    raw = {"public": 1, "api_key": "SECRET", "nested": {"access_token": "TOKEN", "ok": 2}, "items": [{"cookie": "COOKIE", "keep": "yes"}], "tuple": ({"password": "PWD", "safe": True},)}
    expected = sanitize_sensitive_data(raw)
    topic = make_topic()
    topic.raw_data = raw
    store = SQLiteStore(tmp_path / "db.sqlite")
    store.save_topics([topic])
    cache = TopicCacheStore(tmp_path / "cache" / "latest.json", environment="test")
    cache.save([topic], "测试源")
    database_value = store.list_topics()[0].raw_data
    cache_value = cache.load()[0].raw_data
    assert database_value == cache_value == expected
    assert "SECRET" not in json.dumps(cache_value)
    assert "TOKEN" not in json.dumps(cache_value)
    store.save_provider_status("test", "测试源", "error", last_error="proxy http://user:password@example.com:8080 token=SECRET")
    assert "password" not in store.list_provider_status()[0]["last_error"]
    assert "SECRET" not in store.list_provider_status()[0]["last_error"]


def test_embedded_credentials_are_removed_from_cache_and_sqlite_observations(tmp_path):
    raw = {"note": "Authorization=Bearer EMBEDDED_SECRET", "proxy": "http://user:password@example.com:8080"}
    topic = make_topic()
    topic.raw_data = raw
    store = SQLiteStore(tmp_path / "db.sqlite")
    store.save_topics([topic])
    cache = TopicCacheStore(tmp_path / "cache" / "latest.json", environment="test")
    cache.save([topic], "测试源")
    cache_text = cache.path.read_text(encoding="utf-8")
    assert "EMBEDDED_SECRET" not in cache_text
    assert "user:password" not in cache_text
    with store.connect() as connection:
        rows = connection.execute("SELECT raw_data FROM hot_topics UNION ALL SELECT raw_data FROM hot_topic_observations").fetchall()
    database_text = " ".join(str(row[0]) for row in rows)
    assert "EMBEDDED_SECRET" not in database_text
    assert "user:password" not in database_text


def sqlite_text_columns(store: SQLiteStore) -> str:
    tables = ["hot_topics", "hot_topic_observations", "provider_status", "generation_tasks", "topic_basket"]
    values: list[str] = []
    with store.connect() as connection:
        for table in tables:
            columns = [row[1] for row in connection.execute(f"PRAGMA table_info({table})") if str(row[2]).upper() == "TEXT"]
            if columns:
                rows = connection.execute(f"SELECT {', '.join(columns)} FROM {table}").fetchall()
                values.extend(str(value) for row in rows for value in row)
    return "\n".join(values)


def test_all_hot_topic_text_columns_are_sanitized(tmp_path):
    topic = make_topic("话题 token=TITLE_SECRET_X9")
    topic.summary = "摘要 token=SUMMARY_SECRET_X9 proxy=http://user:password@proxy.example"
    topic.source = "source token=SOURCE_SECRET_X9"
    topic.source_name = "来源 token=SOURCE_NAME_SECRET_X9"
    topic.source_url = "http://user:password@example.com/path"
    topic.provider_status = "status token=STATUS_SECRET_X9"
    store = SQLiteStore(tmp_path / "db.sqlite")
    store.save_topics([topic])

    with store.connect() as connection:
        rows = connection.execute("SELECT title,summary,source,source_name,source_url,provider_status FROM hot_topics UNION ALL SELECT title,summary,source,source_name,source_url,provider_status FROM hot_topic_observations").fetchall()
    database_text = "\n".join(str(value) for row in rows for value in row)
    returned = store.list_topics()[0]
    assert "TITLE_SECRET_X9" not in database_text
    assert "SUMMARY_SECRET_X9" not in database_text
    assert "user:password" not in database_text
    assert "TITLE_SECRET_X9" not in returned.title
    assert "SUMMARY_SECRET_X9" not in returned.summary
    assert returned.source_url == "http://***:***@example.com/path"


def test_task_name_and_provider_status_text_are_sanitized(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite")
    topic = make_topic("普通话题")
    store.save_topics([topic])
    task = store.create_task("任务 token=TASK_SECRET_X9", "single_topic_multi_angle", [topic.to_dict()], 1)
    store.save_provider_status(
        "provider token=PROVIDER_SECRET_X9",
        "显示 token=DISPLAY_SECRET_X9",
        "status token=STATUS_SECRET_X9",
        "timestamp token=TIME_SECRET_X9",
        "Authorization=Bearer API_ERROR_SECRET proxy=http://user:password@proxy.example",
    )
    assert "TASK_SECRET_X9" not in task["task_name"]
    assert all("TASK_SECRET_X9" not in item["task_name"] for item in store.list_tasks())
    database_text = sqlite_text_columns(store)
    for secret in ("TASK_SECRET_X9", "PROVIDER_SECRET_X9", "DISPLAY_SECRET_X9", "STATUS_SECRET_X9", "TIME_SECRET_X9", "API_ERROR_SECRET", "user:password"):
        assert secret not in database_text


def test_api_error_payload_redacts_message_and_detail():
    from api import _error

    sensitive = "Authorization=Bearer API_ERROR_SECRET proxy=http://user:password@proxy.example"
    codes = ("HEALTH_CHECK_FAILED", "HOTSPOT_REFRESH_FAILED", "HOTSPOT_LIST_FAILED", "MANUAL_TOPIC_FAILED", "TOPIC_SELECTION_FAILED", "BASKET_UPDATE_FAILED", "TASK_CREATE_FAILED")
    for code in codes:
        response = _error(code, sensitive, {"nested": [sensitive]})
        body = json.loads(response.body)
        serialized = json.dumps(body, ensure_ascii=False)
        assert "API_ERROR_SECRET" not in serialized
        assert "user:password" not in serialized


def test_normal_structured_text_is_preserved(tmp_path):
    topic = make_topic("Token 经济")
    topic.summary = "密码学研究与新闻摘要"
    topic.source_url = "https://example.com/public"
    store = SQLiteStore(tmp_path / "db.sqlite")
    store.save_topics([topic])
    returned = store.list_topics()[0]
    assert returned.title == "Token 经济"
    assert returned.summary == "密码学研究与新闻摘要"
    assert returned.source_url == "https://example.com/public"


def test_refresh_success_response_redacts_provider_output(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    import api

    class SensitiveProvider:
        provider_name = "sensitive-provider"
        display_name = "敏感测试源"
        last_success_at = "2026-07-17T00:00:00+00:00"
        last_error = None

        def fetch_trends(self):
            return [HotTopic(id="api-refresh", title="普通热点", summary="摘要 token=SUMMARY_API_SECRET", raw_data={"api_key": "RAW_API_SECRET", "note": "Authorization=Bearer RAW_BEARER_SECRET", "proxy": "http://user:password@proxy.example"})]

    store = SQLiteStore(tmp_path / "db.sqlite")
    service = HotTrendService(store=store, providers=[SensitiveProvider()], cache_store=TopicCacheStore(tmp_path / "cache.json", environment="test"))
    monkeypatch.setattr(api, "store", store)
    monkeypatch.setattr(api, "service", service)
    response = TestClient(api.app).post("/api/hotspots/refresh")
    body = response.json()
    serialized = json.dumps(body, ensure_ascii=False)
    for secret in ("SUMMARY_API_SECRET", "RAW_API_SECRET", "RAW_BEARER_SECRET", "user:password"):
        assert secret not in serialized
    service_result = service.refresh()
    assert "SUMMARY_API_SECRET" not in json.dumps([topic.to_dict() for topic in service_result["topics"]], ensure_ascii=False)


def test_no_source_response_redacts_cache_exception(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    import api

    class FailedProvider:
        provider_name = "failed-provider"
        display_name = "在线源"
        last_success_at = None
        last_error = None

        def fetch_trends(self):
            raise httpx.ConnectError("Authorization=Bearer NO_SOURCE_BEARER proxy=http://user:password@proxy.example")

    class FailedCache:
        provider_name = "failed-cache"
        display_name = "缓存源"
        last_success_at = None
        last_error = None

        def fetch_trends(self):
            raise RuntimeError("Authorization=Bearer CACHE_BEARER proxy=http://user:password@proxy.example")

    store = SQLiteStore(tmp_path / "db.sqlite")
    service = HotTrendService(store=store, providers=[FailedProvider()], cache_provider=FailedCache(), cache_store=TopicCacheStore(tmp_path / "cache.json", environment="test"))
    monkeypatch.setattr(api, "store", store)
    monkeypatch.setattr(api, "service", service)
    response = TestClient(api.app).post("/api/hotspots/refresh")
    body = response.json()
    assert response.status_code == 503
    serialized = json.dumps(body, ensure_ascii=False)
    for secret in ("NO_SOURCE_BEARER", "CACHE_BEARER", "user:password"):
        assert secret not in serialized
    assert not any(secret in json.dumps(body["data"].get(key), ensure_ascii=False) for key in ("errors", "last_error") for secret in ("NO_SOURCE_BEARER", "CACHE_BEARER", "user:password"))
    assert not any(secret in json.dumps(body["error"].get("detail"), ensure_ascii=False) for secret in ("NO_SOURCE_BEARER", "CACHE_BEARER", "user:password"))


def test_manual_topic_success_response_matches_sanitized_database(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    import api

    store = SQLiteStore(tmp_path / "db.sqlite")
    service = HotTrendService(store=store, providers=[], cache_store=TopicCacheStore(tmp_path / "cache.json", environment="test"))
    monkeypatch.setattr(api, "store", store)
    monkeypatch.setattr(api, "service", service)
    response = TestClient(api.app).post("/api/topics/manual", json={"title": "标题 token=MANUAL_TITLE_SECRET", "summary": "摘要 token=MANUAL_SUMMARY_SECRET proxy=http://user:password@proxy.example"})
    body = response.json()
    serialized = json.dumps(body, ensure_ascii=False)
    for secret in ("MANUAL_TITLE_SECRET", "MANUAL_SUMMARY_SECRET", "user:password"):
        assert secret not in serialized
    topic = store.list_topics()[0]
    assert "MANUAL_TITLE_SECRET" not in topic.title
    assert "MANUAL_SUMMARY_SECRET" not in topic.summary
    queried = TestClient(api.app).get("/api/hotspots").json()
    assert all(secret not in json.dumps(queried, ensure_ascii=False) for secret in ("MANUAL_TITLE_SECRET", "MANUAL_SUMMARY_SECRET", "user:password"))


def test_response出口_recursively_redacts_and_preserves_normal_values():
    from api import _response

    response = _response(True, {"title": "Token 经济", "items": ({"summary": "密码学研究", "token": "API_RESPONSE_SECRET"},), "count": 3, "url": "https://example.com/public"}, {"detail": ["Authorization=Bearer API_RESPONSE_BEARER"]})
    body = json.loads(response.body)
    serialized = json.dumps(body, ensure_ascii=False)
    assert "API_RESPONSE_SECRET" not in serialized
    assert "API_RESPONSE_BEARER" not in serialized
    assert body["data"]["title"] == "Token 经济"
    assert body["data"]["items"][0]["summary"] == "密码学研究"
    assert body["data"]["count"] == 3
    assert body["data"]["url"] == "https://example.com/public"
    assert re.fullmatch(r"[0-9a-f]{32}", body["request_id"])
    assert body["timestamp"].endswith("+00:00")


def test_manual_topic_basket_and_task_snapshot_are_sanitized(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite")
    service = HotTrendService(store=store, providers=[], cache_store=TopicCacheStore(tmp_path / "cache.json", environment="test"))
    topic = service.add_manual_topic("手动安全话题", "摘要 token=TOP_SECRET proxy=http://user:password@proxy.example")
    basket = service.add_to_basket([topic.id])
    task = service.create_task("安全快照任务", "single_topic_multi_angle", [topic.id], 5)
    with store.connect() as connection:
        basket_text = connection.execute("SELECT topics FROM topic_basket").fetchone()[0]
        task_text = connection.execute("SELECT selected_topics FROM generation_tasks WHERE task_id=", (task["task_id"],)).fetchone()[0]
    assert basket and "TOP_SECRET" not in basket_text and "user:password" not in basket_text
    assert "TOP_SECRET" not in task_text and "user:password" not in task_text


def test_json_task_storage_is_sanitized(tmp_path, monkeypatch):
    import modules.task_store as task_store

    monkeypatch.setattr(task_store, "TASK_DIR", tmp_path / "tasks")
    task_store.save_task({"id": "safe", "task_name": "任务 token=TASK_SECRET_X9", "selected_topics": [{"summary": "token=TOP_SECRET", "url": "http://user:password@proxy.example"}]})
    text = (tmp_path / "tasks" / "safe.json").read_text(encoding="utf-8")
    assert "TASK_SECRET_X9" not in text
    assert "TOP_SECRET" not in text
    assert "user:password" not in text


def test_package_scanner_detects_embedded_credentials():
    from scripts.package_phase1 import scan_text

    text = "Authorization=Bearer REAL_LOOKING_TEST_VALUE\nhttp://user:password@proxy.example\napi_key=REAL_API_KEY_VALUE"
    categories = scan_text(Path("fixture.txt"), text)
    assert {"bearer_token", "proxy_credentials", "auth_assignment", "key_assignment"} <= set(categories)


def test_proxy_credentials_and_network_errors_are_redacted():
    message = redact_sensitive_text("proxy=http://user:password@example.com:8080 Authorization=Bearer SECRET")
    assert "password" not in message
    assert "SECRET" not in message
    detail = classify_network_error(httpx.ConnectError("proxy http://user:password@example.com:8080"))
    assert detail["category"] == "proxy"
    assert "password" not in detail["message"]


def test_hot_score_units_sort_numerically(tmp_path):
    assert normalize_hot_score("1.2亿") > normalize_hot_score("9500万") > normalize_hot_score("12.5万") > normalize_hot_score("9800")
    store = SQLiteStore(tmp_path / "db.sqlite")
    topics = [make_topic(f"hot-{index}") for index in range(4)]
    for topic, value in zip(topics, ["9800", "12.5万", "9500万", "1.2亿"]):
        topic.id = value
        topic.hot_value = value
    store.save_topics(topics)
    assert [topic.hot_value for topic in store.list_topics(sort="hot_desc")] == ["1.2亿", "9500万", "12.5万", "9800"]


def test_model_provider_uses_profile_network_factory(monkeypatch):
    import providers.text_provider as text_provider

    calls: list[dict] = []

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "ok"}}]}

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def post(self, *args, **kwargs):
            return FakeResponse()

    def factory(settings):
        calls.append(settings)
        return FakeClient()

    monkeypatch.setattr(text_provider, "create_http_client", factory)
    provider = text_provider.OpenAITextProvider({"api_key": "key", "base_url": "https://example.invalid", "network": {"mode": "custom", "https_proxy": "http://u:p@proxy:8080"}}, {"mode": "direct"})
    assert provider.generate("test") == "ok"
    assert calls[0]["mode"] == "custom"
    assert calls[0]["https_proxy"] == "http://u:p@proxy:8080"


def test_failed_health_check_is_explicitly_failed():
    check = DailyHotSource("https://invalid.test.local/hot").health_check()
    assert check["ok"] is False
    assert check["error_type"] in {"dns", "proxy", "tls", "timeout", "http_status", "data_format", "network"}
    assert "retryable" in check


def test_zip_paths_are_posix(tmp_path):
    source = tmp_path / "folder"
    source.mkdir()
    (source / "中文.txt").write_text("ok", encoding="utf-8")
    archive_path = export_zip(source, tmp_path / "out.zip")
    with __import__("zipfile").ZipFile(archive_path) as archive:
        assert all("\\" not in name for name in archive.namelist())
        assert archive.namelist() == ["中文.txt"]


def test_atomic_cache_write_preserves_previous_snapshot(tmp_path, monkeypatch):
    cache = TopicCacheStore(tmp_path / "cache" / "latest.json", environment="test")
    cache.save([make_topic("old")], "源")
    previous = cache.path.read_text(encoding="utf-8")
    real_replace = Path.replace

    def interrupted(self, target):
        if self.name.endswith(".tmp"):
            raise OSError("interrupted")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", interrupted)
    with pytest.raises(OSError):
        cache.save([make_topic("new")], "源")
    assert cache.path.read_text(encoding="utf-8") == previous


def test_python_selector_order_and_safe_stop_contract():
    runtime = (ROOT / "scripts" / "python_runtime.ps1").read_text(encoding="utf-8")
    stopper = (ROOT / "scripts" / "stop_project.ps1").read_text(encoding="utf-8")
    assert re.search(r"foreach \(\$minor in 13, 12, 11\)", runtime)
    assert "Get-NetTCPConnection" not in stopper
    assert "project_root" in stopper
    assert "process_start_time" in stopper
    assert "python_path" in stopper


def test_delivery_docs_have_one_authoritative_status():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    status = (ROOT / "STATUS.md").read_text(encoding="utf-8")
    assert "STATUS.md" in readme
    assert "DEMO 图片完成正式流程" not in readme
    assert "Windows 启动仍待测试" not in readme
    assert "当前测试结果" in status
