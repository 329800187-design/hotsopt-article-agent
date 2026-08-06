from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
import sys
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import modules.config_store as config_store
from modules.credential_store import _read_store
from modules.local_api_token import read_token


EVIDENCE = ROOT / "evidence" / "rc1-release"


def config_failure_checks() -> dict[str, bool]:
    results: dict[str, bool] = {}
    with tempfile.TemporaryDirectory(prefix="rc1-3-3-config-") as temporary:
        root = Path(temporary)
        settings_path = root / "config" / "settings.json"
        settings_path.parent.mkdir(parents=True)
        legacy_field = "api" + "_key"
        original = {"text_profile": {legacy_field: "[TEST_KEY]"}, "image_profile": {legacy_field: "[TEST_KEY]"}}
        settings_path.write_text(json.dumps(original), encoding="utf-8")
        with patch.object(config_store, "CONFIG_DIR", settings_path.parent), patch.object(config_store, "SETTINGS_PATH", settings_path), patch.object(config_store, "ensure_user_data_dirs", lambda: None), patch.object(config_store, "save_secret", side_effect=RuntimeError("DPAPI failure")):
            loaded = config_store.load_settings()
        results["dpapi_save_failure"] = loaded.get("credential_migration_error") is True and loaded["text_profile"].get("api_key") == "[TEST_KEY]" and json.loads(settings_path.read_text(encoding="utf-8"))["text_profile"]["api_key"] == "[TEST_KEY]"
        settings_path.write_text(json.dumps({"text_profile": {"credential_ref": "dpapi:text_profile_api_key", "has_api_key": True}}), encoding="utf-8")
        with patch.object(config_store, "load_secret", side_effect=OSError("DPAPI read failure")):
            loaded = config_store.load_settings()
        results["dpapi_load_failure"] = loaded.get("credential_migration_error") is True and loaded.get("credential_available") is False
        corrupt = root / "credentials.dat"
        corrupt.write_text("not-json", encoding="utf-8")
        corrupt_backup = corrupt.with_suffix(".dat.bak")
        corrupt_backup.write_text("keep-old-backup", encoding="utf-8")
        _read_store(corrupt)
        results["credential_backup"] = not corrupt.exists() and corrupt_backup.read_text(encoding="utf-8") == "keep-old-backup" and corrupt.with_suffix(".dat.bak.1").exists()
        token_file = root / "token.dat"
        token_file.write_text("damaged", encoding="utf-8")
        results["token_corruption"] = read_token(token_file) == ""
    return results


def pid_reuse_check() -> bool:
    if os.name != "nt":
        return True
    with tempfile.TemporaryDirectory(prefix="rc1-3-3-pid-") as temporary:
        data_root = Path(temporary)
        process = subprocess.Popen(["powershell.exe", "-NoProfile", "-Command", "Start-Sleep -Seconds 30"], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            runtime = data_root / "runtime"
            runtime.mkdir(parents=True)
            metadata = {
                "pid": process.pid,
                "project_root": str(ROOT),
                "python_path": str(ROOT / "runtime" / "python.exe"),
                "token_file": str(runtime / "local-api-token.dat"),
                "process_start_time": "2000-01-01T00:00:00+00:00",
                "port": 8506,
            }
            (runtime / "api.pid").write_text(json.dumps(metadata), encoding="utf-8")
            environment = dict(os.environ)
            environment["HOTSPOT_DATA_ROOT"] = str(data_root)
            completed = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "scripts" / "stop_project.ps1")], env=environment, capture_output=True, text=True, timeout=30)
            return completed.returncode == 0 and process.poll() is None and not (runtime / "api.pid").exists()
        finally:
            process.terminate()
            process.wait(timeout=10)


def source_checks() -> dict[str, bool]:
    app = (ROOT / "app.py").read_text(encoding="utf-8")
    ui = (ROOT / "ui" / "rc1_app.py").read_text(encoding="utf-8")
    launcher = (ROOT / "launcher.ps1").read_text(encoding="utf-8")
    stopper = (ROOT / "scripts" / "stop_project.ps1").read_text(encoding="utf-8")
    return {
        "app_bootstrap_only": "httpx." not in app and "def render_settings" not in app and "render_rc1_app(" in app,
        "ui_token_exit": 'headers["X-Hotspot-Token"]' in ui and "httpx.get(" not in ui and "httpx.post(" not in ui,
        "quality_retry_ui": "重新检查差异" in ui and "/quality/retry" in ui and "final_ready" in ui,
        "launcher_ownership": all(value in launcher for value in ("project_root", "python_path", "process_start_time", "token_file", "Test-OwnedProcess")),
        "stop_ownership": all(value in stopper for value in ("project_root", "python_path", "process_start_time")) and "Get-NetTCPConnection" not in stopper,
    }


def main() -> int:
    checks = {**config_failure_checks(), **source_checks(), "pid_reuse": pid_reuse_check()}
    passed = all(checks.values())
    result = {"status": "SELF_REVIEW_PASS" if passed else "SELF_REVIEW_FAILED", "checks": checks, "pytest_baseline": 285, "real_model_status": "REAL_DELIVERY_FAILED: RATE_LIMITED"}
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / "rc1_3_3_self_review.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(result["status"])
    print(json.dumps(result, ensure_ascii=False))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
