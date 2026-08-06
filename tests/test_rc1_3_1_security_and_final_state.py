from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

import modules.config_store as config_store
from modules.database import SQLiteStore
from scripts.package_rc1 import _include_customer_source_file


def _topic(topic_id: str = "topic-1") -> dict:
    return {
        "id": topic_id,
        "title": f"测试热点 {topic_id}",
        "summary": "普通摘要",
        "source": "test",
        "source_name": "测试源",
        "source_url": f"https://example.com/{topic_id}",
        "captured_at": "2026-07-19T00:00:00+08:00",
    }


def test_multi_angle_batch_is_not_final_before_quality_gate(tmp_path: Path):
    store = SQLiteStore(tmp_path / "db.sqlite")
    batch = store.create_batch(
        "三角度创作",
        "single_topic_multi_angle",
        [_topic()],
        {"article_count": 3},
        angles=[
            {"angle_id": "a", "angle_name": "角度 A"},
            {"angle_id": "b", "angle_name": "角度 B"},
            {"angle_id": "c", "angle_name": "角度 C"},
        ],
    )
    for item in batch["items"]:
        store.update_task_status(item["task"]["task_id"], "completed")
    refreshed = store.refresh_batch(batch["batch_id"])
    assert refreshed["status"] == "running"
    assert refreshed["quality_status"] == "pending"
    assert refreshed["final_ready"] == 0
    assert refreshed["completed_at"] is None


def test_multi_angle_batch_final_ready_after_quality_passed(tmp_path: Path):
    store = SQLiteStore(tmp_path / "db.sqlite")
    batch = store.create_batch(
        "三角度创作",
        "single_topic_multi_angle",
        [_topic()],
        {"article_count": 2},
        angles=[{"angle_id": "a", "angle_name": "角度 A"}, {"angle_id": "b", "angle_name": "角度 B"}],
    )
    for item in batch["items"]:
        store.update_task_status(item["task"]["task_id"], "completed")
    store.update_batch_quality(batch["batch_id"], "passed")
    refreshed = store.refresh_batch(batch["batch_id"])
    assert refreshed["status"] == "completed"
    assert refreshed["quality_status"] == "passed"
    assert refreshed["final_ready"] == 1
    assert refreshed["completed_at"]


def test_batch_quality_rewrite_clears_stale_completed_at(tmp_path: Path):
    store = SQLiteStore(tmp_path / "db.sqlite")
    batch = store.create_batch("双角度创作", "single_topic_multi_angle", [_topic()], {"article_count": 2}, angles=[{"angle_id": "a"}, {"angle_id": "b"}])
    for item in batch["items"]:
        store.update_task_status(item["task"]["task_id"], "completed")
    store.update_batch_quality(batch["batch_id"], "passed")
    assert store.refresh_batch(batch["batch_id"])["final_ready"] == 1
    store.update_batch_quality(batch["batch_id"], "rewriting")
    refreshed = store.refresh_batch(batch["batch_id"])
    assert refreshed["status"] == "running"
    assert refreshed["final_ready"] == 0
    assert refreshed["completed_at"] is None


def test_settings_persist_no_plaintext_api_key(tmp_path: Path, monkeypatch):
    settings_path = tmp_path / "config" / "settings.json"
    saved: dict[str, str] = {}

    monkeypatch.setattr(config_store, "CONFIG_DIR", settings_path.parent)
    monkeypatch.setattr(config_store, "SETTINGS_PATH", settings_path)
    monkeypatch.setattr(config_store, "save_secret", lambda name, secret: saved.setdefault(name, secret) and f"dpapi:{name}")
    monkeypatch.setattr(config_store, "load_secret", lambda ref: saved.get(ref.replace("dpapi:", ""), ""))

    config_store.save_settings({"text_profile": {"api_key": "sk-REAL_TEST_SECRET_1234567890"}, "image_profile": {"api_key": "sk-IMAGE_TEST_SECRET_1234567890"}})
    persisted = settings_path.read_text(encoding="utf-8")
    assert "sk-REAL_TEST_SECRET" not in persisted
    assert "sk-IMAGE_TEST_SECRET" not in persisted
    data = json.loads(persisted)
    assert data["text_profile"]["has_api_key"] is True
    assert data["text_profile"]["credential_ref"] == "dpapi:text_profile_api_key"
    loaded = config_store.load_settings()
    assert loaded["text_profile"]["api_key"].startswith("sk-REAL_TEST_SECRET")


def test_local_api_requires_token_when_configured(monkeypatch):
    monkeypatch.setenv("HOTSPOT_LOCAL_API_TOKEN", "u" * 43)
    import api

    client = TestClient(api.app)
    denied = client.get("/api/health")
    assert denied.status_code == 401
    assert denied.json()["error"]["code"] == "LOCAL_API_AUTH_REQUIRED"
    allowed = client.get("/api/health", headers={"X-Hotspot-Token": "u" * 43})
    assert allowed.status_code == 200


def test_customer_windows_package_whitelist_excludes_dev_files():
    blocked = [
        "tests/test_core.py",
        "pytest.ini",
        "requirements-dev.txt",
        "TECH_AUDIT.md",
        "STATUS.md",
        "docs/Windows商业交付候选版_RC1.3_最终验收报告.md",
        "scripts/package_rc1.py",
        "scripts/phase1_smoke_test.py",
        ".gitignore",
        "install.bat",
    ]
    allowed = [
        "api.py",
        "app.py",
        "launcher.ps1",
        "config/settings.example.json",
        "modules/database.py",
        "generation/batch_executor.py",
        "providers/text_provider.py",
        "ui/rc1_app.py",
        "scripts/stop_project.ps1",
    ]
    assert all(not _include_customer_source_file(path) for path in blocked)
    assert all(_include_customer_source_file(path) for path in allowed)
