from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

import modules.config_store as config_store
import modules.generation_store as generation_store
from generation.batch_executor import BatchExecutor
from modules.database import SQLiteStore
from modules.generation_store import save_generation_task
from scripts.package_rc1 import _include_customer_source_file, _requirements_for_runtime


def test_api_fail_closed_without_token(monkeypatch):
    import api

    monkeypatch.delenv("HOTSPOT_ALLOW_UNAUTHENTICATED_TEST_API", raising=False)
    monkeypatch.delenv("HOTSPOT_LOCAL_API_TOKEN", raising=False)
    response = TestClient(api.app).get("/api/health")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "LOCAL_API_AUTH_REQUIRED"


def test_api_rejects_empty_and_wrong_token(monkeypatch):
    import api

    monkeypatch.delenv("HOTSPOT_ALLOW_UNAUTHENTICATED_TEST_API", raising=False)
    monkeypatch.setenv("HOTSPOT_LOCAL_API_TOKEN", "x" * 43)
    client = TestClient(api.app)
    assert client.get("/api/health", headers={"X-Hotspot-Token": ""}).status_code == 401
    assert client.get("/api/health", headers={"X-Hotspot-Token": "bad"}).status_code == 401
    assert client.get("/api/health", headers={"X-Hotspot-Token": "x" * 43}).status_code == 200


def test_openapi_docs_are_disabled(monkeypatch):
    import api

    monkeypatch.setenv("HOTSPOT_ALLOW_UNAUTHENTICATED_TEST_API", "1")
    client = TestClient(api.app)
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def _topic() -> dict:
    return {"id": "q-topic", "title": "质量恢复热点", "summary": "摘要", "source": "test", "source_name": "测试源", "source_url": "https://example.com", "captured_at": "2026-07-20T00:00:00+08:00"}


def _completed_quality_batch(tmp_path: Path, monkeypatch, quality_status: str) -> tuple[SQLiteStore, str]:
    monkeypatch.setattr(generation_store, "TASKS_ROOT", tmp_path / "tasks")
    store = SQLiteStore(tmp_path / "db.sqlite")
    batch = store.create_batch("质量恢复", "single_topic_multi_angle", [_topic()], {"article_count": 2}, angles=[{"angle_id": "a"}, {"angle_id": "b"}])
    for index, item in enumerate(batch["items"], start=1):
        task_id = item["task"]["task_id"]
        save_generation_task({"task_id": task_id, "status": "completed", "stage": "completed", "state_version": 0, "article": {"title": f"文章 {index}", "summary": "摘要", "sections": [{"heading": f"小标题 {index}", "body": f"正文 {index}"}], "content_markdown": f"# 文章 {index}\n\n正文 {index}"}})
        store.update_task_status(task_id, "completed")
    store.update_batch_quality(batch["batch_id"], quality_status)
    return store, batch["batch_id"]


def _no_similarity_violations(monkeypatch) -> None:
    monkeypatch.setattr(
        "generation.batch_executor.compare_batch_report",
        lambda articles: {"total_pairs_checked": 1, "pairs": [], "violating_pairs": []},
    )


def test_quality_pending_recovery_rechecks_and_becomes_final(tmp_path, monkeypatch):
    _no_similarity_violations(monkeypatch)
    store, batch_id = _completed_quality_batch(tmp_path, monkeypatch, "pending")
    report = BatchExecutor(store).recover_batches()
    batch = store.refresh_batch(batch_id)
    assert report["recovery_failed"] == []
    assert batch["quality_status"] == "passed"
    assert batch["final_ready"] == 1


def test_quality_checking_recovery_is_idempotent(tmp_path, monkeypatch):
    _no_similarity_violations(monkeypatch)
    store, batch_id = _completed_quality_batch(tmp_path, monkeypatch, "checking")
    executor = BatchExecutor(store)
    executor.recover_batches()
    first = store.get_batch(batch_id)
    executor.recover_batches()
    second = store.get_batch(batch_id)
    assert first["final_ready"] == 1
    assert second["final_ready"] == 1
    assert second["quality_status"] == "passed"


def test_failed_quality_can_be_retried(tmp_path, monkeypatch):
    _no_similarity_violations(monkeypatch)
    store, batch_id = _completed_quality_batch(tmp_path, monkeypatch, "failed")
    batch = BatchExecutor(store).retry_quality_check(batch_id)
    assert batch["quality_status"] == "passed"
    assert batch["final_ready"] == 1


def test_settings_blank_preserves_existing_key(tmp_path, monkeypatch):
    settings_path = tmp_path / "config" / "settings.json"
    saved: dict[str, str] = {}

    monkeypatch.setattr(config_store, "CONFIG_DIR", settings_path.parent)
    monkeypatch.setattr(config_store, "SETTINGS_PATH", settings_path)
    monkeypatch.setattr(config_store, "save_secret", lambda name, secret: saved.setdefault(name, secret) and f"dpapi:{name}")
    monkeypatch.setattr(config_store, "load_secret", lambda ref: saved.get(ref.replace("dpapi:", ""), ""))
    monkeypatch.setattr(config_store, "delete_secret", lambda ref: saved.pop(ref.replace("dpapi:", ""), None))

    config_store.save_settings({"text_profile": {"api_key": "old-key-value-1234567890"}, "image_profile": {"api_key": "old-image-key-1234567890"}})
    config_store.save_settings({"text_profile": {"model": "new-model", "api_key": "***"}, "image_profile": {"model": "new-image", "api_key": "***"}})
    loaded = config_store.load_settings()
    assert loaded["text_profile"]["api_key"] == "old-key-value-1234567890"
    assert loaded["text_profile"]["model"] == "new-model"
    config_store.save_settings({"text_profile": {"clear_api_key": True, "api_key": ""}, "image_profile": {"clear_api_key": True, "api_key": ""}})
    persisted = json.loads(settings_path.read_text(encoding="utf-8"))
    assert persisted["text_profile"]["has_api_key"] is False


def test_runtime_requirements_include_socksio_and_pillow_lt_12():
    requirements = {requirement.name.lower(): str(requirement.specifier) for requirement in _requirements_for_runtime()}
    assert "socksio" in requirements
    assert "pillow" in requirements
    assert "<12" in requirements["pillow"] or "==11.3.0" in requirements["pillow"]


def test_customer_package_excludes_test_bypass_switch():
    assert _include_customer_source_file("tests/conftest.py") is False
    assert _include_customer_source_file("requirements-runtime.txt") is True
