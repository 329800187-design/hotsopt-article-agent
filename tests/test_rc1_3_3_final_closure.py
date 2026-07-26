from __future__ import annotations

import json
from pathlib import Path

import modules.config_store as config_store
from modules.credential_store import _read_store


ROOT = Path(__file__).resolve().parents[1]


def _isolate_config(tmp_path, monkeypatch) -> Path:
    settings_path = tmp_path / "config" / "settings.json"
    monkeypatch.setattr(config_store, "CONFIG_DIR", settings_path.parent)
    monkeypatch.setattr(config_store, "SETTINGS_PATH", settings_path)
    monkeypatch.setattr(config_store, "ensure_user_data_dirs", lambda: settings_path.parent.mkdir(parents=True, exist_ok=True))
    return settings_path


def test_dpapi_save_failure_keeps_plaintext_settings_and_allows_startup(tmp_path, monkeypatch):
    settings_path = _isolate_config(tmp_path, monkeypatch)
    original = {
        "text_profile": {"api_key": "sk-legacy-test-value"},
        "image_profile": {"api_key": "sk-legacy-test-value"},
    }
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps(original), encoding="utf-8")
    monkeypatch.setattr(config_store, "save_secret", lambda *_args: (_ for _ in ()).throw(RuntimeError("DPAPI unavailable")))

    loaded = config_store.load_settings()

    assert loaded["credential_migration_error"] is True
    assert loaded["credential_available"] is True
    assert loaded["text_profile"]["api_key"] == "sk-legacy-test-value"
    assert json.loads(settings_path.read_text(encoding="utf-8"))["text_profile"]["api_key"] == "sk-legacy-test-value"


def test_dpapi_load_failure_does_not_block_startup(tmp_path, monkeypatch):
    settings_path = _isolate_config(tmp_path, monkeypatch)
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({"text_profile": {"credential_ref": "dpapi:text_profile_api_key", "has_api_key": True}}), encoding="utf-8")
    monkeypatch.setattr(config_store, "load_secret", lambda *_args: (_ for _ in ()).throw(OSError("credential read failed")))

    loaded = config_store.load_settings()

    assert loaded["credential_migration_error"] is True
    assert loaded["credential_available"] is False
    assert loaded["text_profile"]["api_key"] == ""


def test_corrupt_credential_store_is_backed_up_without_overwriting_backup(tmp_path):
    path = tmp_path / "credentials.dat"
    path.write_text("not-json", encoding="utf-8")
    first_backup = path.with_suffix(".dat.bak")
    first_backup.write_text("existing-backup", encoding="utf-8")

    result = _read_store(path)

    assert result == {"version": 1, "secrets": {}}
    assert not path.exists()
    assert first_backup.read_text(encoding="utf-8") == "existing-backup"
    assert path.with_suffix(".dat.bak.1").exists()


def test_quality_failure_ui_exposes_retry_and_final_gate():
    source = (ROOT / "ui" / "rc1_app.py").read_text(encoding="utf-8")
    assert "quality_status" in source
    assert "quality_error" in source
    assert "差异检查暂未完成" in source
    assert "重新检查差异" in source
    assert "/quality/retry" in source
    assert "final_ready" in source


def test_quality_retry_route_exists():
    import api

    assert "/api/batches/{batch_id}/quality/retry" in {route.path for route in api.app.routes}


def test_app_contains_only_bootstrap_and_render_entrypoint():
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    assert "render_rc1_app(settings, save_settings, hot_service, ROOT, CATEGORIES)" in source
    assert "httpx." not in source
    assert "def render_settings" not in source
    assert "def render_batches" not in source


def test_formal_ui_uses_one_tokenized_request_exit():
    source = (ROOT / "ui" / "rc1_app.py").read_text(encoding="utf-8")
    assert "def _request(" in source
    assert 'headers["X-Hotspot-Token"]' in source
    assert "httpx.get(" not in source
    assert "httpx.post(" not in source


def test_launcher_requires_project_runtime_and_process_start_metadata():
    source = (ROOT / "desktop_host.py").read_text(encoding="utf-8")
    for required in ["runtime_root", "_python_executable", "_write_json", "api.json", "web.json", "_stop_process", "_api_healthy"]:
        assert required in source


def test_stop_script_refuses_pid_reuse_and_other_project_processes():
    source = (ROOT / "scripts" / "stop_project.ps1").read_text(encoding="utf-8")
    assert "process_start_time" in source
    assert "project_root" in source
    assert "python_path" in source
    assert "refused to stop" in source.lower()
    assert "Get-NetTCPConnection" not in source
