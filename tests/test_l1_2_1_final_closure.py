from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from modules import device_identity, license_service


ROOT = Path(__file__).resolve().parents[1]


def _fresh_paths(monkeypatch, tmp_path):
    root = tmp_path / "fresh-data"
    monkeypatch.setattr(device_identity, "license_root", lambda: root / "license")
    monkeypatch.setattr(license_service, "license_root", lambda: root / "license")
    monkeypatch.setattr(license_service, "ACTIVE_LICENSE_PATH", root / "license" / "active.license")
    monkeypatch.setattr(license_service, "STATE_PATH", root / "license" / "license_state.json")
    return root


def test_fresh_check_license_creates_identity_before_clock_state(monkeypatch, tmp_path):
    root = _fresh_paths(monkeypatch, tmp_path)
    status = license_service.check_license()
    assert status["code"] == "LICENSE_REQUIRED"
    assert device_identity.installation_path().exists()
    assert device_identity.installation_backup_path().exists()
    assert status["code"] != "INSTALLATION_ID_MISSING"
    assert (root / "license" / "license_state.json").exists()


def test_fresh_api_license_status_has_device_code(monkeypatch, tmp_path):
    _fresh_paths(monkeypatch, tmp_path)
    import api

    response = api.license_status()
    body = json.loads(response.body.decode("utf-8"))
    assert body["success"] is True
    assert body["data"]["code"] == "LICENSE_REQUIRED"
    assert body["data"]["device_code"] and body["data"]["device_code"].count("-") == 4


def test_first_launch_ui_uses_public_device_status_only():
    source = (ROOT / "ui/rc1_app.py").read_text(encoding="utf-8")
    block = source.split("def _render_license_activation", 1)[1].split("def render_rc1_app", 1)[0]
    assert "device_status()" in block
    assert "st.code(device_value, language=None)" in block
    assert "INSTALLATION_ID_MISSING" not in block


def test_first_launch_smoke_uses_rc12_1_windows_package():
    source = (ROOT / "scripts/l1_first_launch_smoke.ps1").read_text(encoding="utf-8")
    assert "hotspot-article-agent-l1-rc1-2-3-windows.zip" in source
    assert "FIRST_LAUNCH_DEVICE_CODE_PASS" in source


def test_corrected_time_smoke_does_not_jump_past_future_reference():
    source = (ROOT / "scripts/l1_license_recovery_smoke.ps1").read_text(encoding="utf-8")
    assert "previous = now + timedelta(days=2)" in source
    assert "now + timedelta(seconds=10)" in source
    assert "now + timedelta(seconds=20)" in source
    assert "future + timedelta(minutes" not in source
    assert "CLOCK_CORRECTED_TIME_RECOVERY_PASS" in source


def test_recovery_code_allows_corrected_time_checks():
    source = (ROOT / "modules/license_service.py").read_text(encoding="utf-8")
    assert "allow_recovery: bool = False" in source
    assert "_save_clock_state(now or datetime.now(timezone.utc), allow_recovery=True)" in source
    assert "validate_license(_load_json(ACTIVE_LICENSE_PATH), now=current_time)" in source


def test_restricted_settings_accepts_restricted_flag():
    source = (ROOT / "ui/rc1_app.py").read_text(encoding="utf-8")
    assert "def _settings_page(settings: dict[str, Any], save_settings: Any, root: Path, restricted: bool = False)" in source
    assert "_settings_page(settings, save_settings, root, restricted=True)" in source


def test_restricted_settings_does_not_import_provider_clients():
    source = (ROOT / "ui/rc1_app.py").read_text(encoding="utf-8")
    assert "OpenAITextProvider" not in source
    assert "OpenAIImageProvider" not in source
    assert '"保存设置" if restricted else "保存并检测"' in source


def test_authorized_settings_uses_backend_model_tests():
    source = (ROOT / "ui/rc1_app.py").read_text(encoding="utf-8")
    assert '_api("POST", "/models/text/test")' in source
    assert '_api("POST", "/models/image/test")' in source
    assert "test_connection()" not in source


def test_model_test_routes_keep_backend_license_gate():
    source = (ROOT / "api.py").read_text(encoding="utf-8")
    text_start = source.index("def test_text_model")
    image_start = source.index("def test_image_model")
    assert "blocked = _license_gate()" in source[text_start:image_start]
    assert "blocked = _license_gate(\"image_generation\")" in source[image_start:]


def test_admin_readme_has_only_valid_launch_commands():
    text = (ROOT / "license_admin/README.md").read_text(encoding="utf-8")
    assert "python license_admin/license_generator_gui.py" not in text
    assert "双击根目录的 `start-license-generator.bat`" in text
    assert "python -m license_admin.license_generator_gui" in text


def test_admin_independent_import_command_is_documented():
    source = (ROOT / "license_admin/README.md").read_text(encoding="utf-8")
    bat = (ROOT / "start-license-generator.bat").read_text(encoding="utf-8")
    assert "不包含初始化命令" in source
    assert "Hotspot License Admin.exe" in bat
    assert "license_generator_gui" in bat


def test_conftest_has_no_import_time_license_creation():
    source = (ROOT / "tests/conftest.py").read_text(encoding="utf-8")
    assert "_install_signed_test_license()" not in source
    assert "@pytest.fixture(autouse=True)" in source


def test_non_windows_collection_has_explicit_protector():
    source = (ROOT / "tests/conftest.py").read_text(encoding="utf-8")
    assert 'if os.name != "nt":' in source
    assert "fake_save" in source
    assert "fake_load" in source


def test_first_launch_status_does_not_treat_clock_files_as_identity_evidence():
    source = (ROOT / "modules/device_identity.py").read_text(encoding="utf-8")
    assert "identity_evidence =" in source
    assert "active.license" in source
    assert "installation.initialized" in source
    assert "license_state.json" not in source.split("identity_evidence =", 1)[1].split("if not root.exists", 1)[0]


def test_final_self_review_marker_is_configured():
    source = (ROOT / "scripts/package_l1.py").read_text(encoding="utf-8")
    assert "L1-RC1.2.3" in source
    assert "OFFLINE_LICENSE_RC1_2_3_SELF_REVIEW_PASS" in source


@pytest.mark.skipif(os.name != "nt", reason="final Windows first-launch process smoke runs on Windows")
def test_first_launch_smoke_is_windows_only():
    assert os.name == "nt"
