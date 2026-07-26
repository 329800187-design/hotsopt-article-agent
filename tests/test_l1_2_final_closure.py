from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from modules import device_identity, license_service
from modules.license_schema import canonical_payload


ROOT = Path(__file__).resolve().parents[1]
FEATURES = ["hot_topics", "custom_topic", "five_articles", "image_generation", "article_editing", "word_export", "zip_export"]


@pytest.fixture
def rc12_license_env(tmp_path, monkeypatch):
    if os.name != "nt":
        pytest.skip("DPAPI recovery is verified on Windows; non-Windows uses explicit skip")
    root = tmp_path / "license"
    private = Ed25519PrivateKey.generate()
    public_path = tmp_path / "license_public_key.pem"
    public_path.write_bytes(private.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    monkeypatch.setattr(device_identity, "license_root", lambda: root)
    monkeypatch.setattr(license_service, "license_root", lambda: root)
    monkeypatch.setattr(license_service, "PUBLIC_KEY_PATH", public_path)
    monkeypatch.setattr(license_service, "ACTIVE_LICENSE_PATH", root / "active.license")
    monkeypatch.setattr(license_service, "STATE_PATH", root / "license_state.json")
    base = datetime.now(timezone.utc).replace(microsecond=0)

    def issue(**changes):
        value = {
            "schema_version": 1,
            "license_id": "LIC-RC12-000001",
            "product": "hotspot-article-agent",
            "edition": "standard",
            "customer_name": "RC1.2 测试客户",
            "device_code": device_identity.device_code(),
            "issued_at": base.isoformat(),
            "not_before": (base - timedelta(minutes=1)).isoformat(),
            "expires_at": (base + timedelta(days=30)).isoformat(),
            "features": FEATURES,
            "signature_algorithm": "Ed25519",
        }
        value.update(changes)
        value.pop("signature", None)
        value["signature"] = base64.urlsafe_b64encode(private.sign(canonical_payload(value))).decode("ascii").rstrip("=")
        return value

    return root, base, issue


def _install_license(root: Path, value: dict) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "active.license").write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def test_markdown_does_not_use_inline_clipboard_script():
    source = (ROOT / "ui/rc1_app.py").read_text(encoding="utf-8")
    assert "navigator.clipboard.writeText" not in source
    assert "javascript:" not in source


def test_device_code_uses_streamlit_copyable_component():
    source = (ROOT / "ui/rc1_app.py").read_text(encoding="utf-8")
    assert "st.code(device_value, language=None)" in source
    assert "点击申请码右上角的复制图标即可复制" in source


def test_empty_device_code_has_no_copy_component():
    source = (ROOT / "ui/rc1_app.py").read_text(encoding="utf-8")
    block = source.split("def _render_license_activation", 1)[1].split("def render_rc1_app", 1)[0]
    assert "if device_value:" in block
    assert block.index("if device_value:") < block.index("st.code(device_value, language=None)")


def test_device_code_view_does_not_expose_installation_identity():
    source = (ROOT / "ui/rc1_app.py").read_text(encoding="utf-8")
    block = source.split("def _render_license_activation", 1)[1].split("def render_rc1_app", 1)[0]
    assert "installation_id" not in block
    assert "_machine_guid" not in block


def test_clock_state_has_recovery_status_machine():
    source = (ROOT / "modules/license_service.py").read_text(encoding="utf-8")
    assert '"normal", "suspected", "recovery_pending"' in source
    assert "def recover_clock_rollback" in source
    assert "def check_system_time" in source


def test_clock_small_change_is_not_false_positive_rc12(rc12_license_env):
    root, base, issue = rc12_license_env
    _install_license(root, issue())
    license_service.save_secret(license_service.STATE_SECRET_NAME, (base + timedelta(minutes=5)).isoformat(), path=license_service._state_secret_path())
    assert license_service.check_license(now=base)["valid"] is True
    assert license_service.clock_status()["clock_status"] == "normal"


def test_clock_rollback_over_24_hours_enters_suspected(rc12_license_env):
    root, base, issue = rc12_license_env
    _install_license(root, issue())
    future = base + timedelta(days=2)
    license_service.save_secret(license_service.STATE_SECRET_NAME, future.isoformat(), path=license_service._state_secret_path())
    result = license_service.check_license(now=base)
    assert result["code"] == "CLOCK_ROLLBACK_SUSPECTED"
    assert license_service.clock_status()["clock_status"] == "suspected"


def test_suspected_clock_blocks_generation_but_keeps_history(rc12_license_env):
    root, base, issue = rc12_license_env
    _install_license(root, issue())
    future = base + timedelta(days=2)
    license_service.save_secret(license_service.STATE_SECRET_NAME, future.isoformat(), path=license_service._state_secret_path())
    allowed, status = license_service.license_allows_generation("five_articles", now=base)
    assert allowed is False
    assert status["code"] == "CLOCK_ROLLBACK_SUSPECTED"


def test_clock_recovery_restores_generation(rc12_license_env):
    root, base, issue = rc12_license_env
    _install_license(root, issue())
    previous = base + timedelta(days=2)
    license_service.save_secret(license_service.STATE_SECRET_NAME, previous.isoformat(), path=license_service._state_secret_path())
    assert license_service.check_license(now=base)["code"] == "CLOCK_ROLLBACK_SUSPECTED"
    assert license_service.check_system_time(now=base + timedelta(seconds=10))["recovery_check_count"] == 0
    assert license_service.check_system_time(now=base + timedelta(seconds=20))["recoverable"] is False
    corrected = previous + timedelta(seconds=10)
    assert license_service.check_system_time(now=corrected)["recovery_ready"] is False
    assert license_service.check_system_time(now=corrected + timedelta(seconds=10))["recovery_ready"] is True
    assert license_service.recover_clock_rollback(now=corrected + timedelta(seconds=10))["recovered"] is True
    assert license_service.check_license(now=corrected + timedelta(seconds=20))["valid"] is True
    assert license_service.license_allows_generation("five_articles", now=corrected + timedelta(seconds=20))[0] is True


def test_legacy_sticky_clock_state_migrates(rc12_license_env):
    root, base, issue = rc12_license_env
    _install_license(root, issue())
    root.mkdir(parents=True, exist_ok=True)
    (root / "license_state.json").write_text(json.dumps({"clock_rollback_suspected": True}), encoding="utf-8")
    license_service.save_secret(license_service.STATE_SECRET_NAME, base.isoformat(), path=license_service._state_secret_path())
    assert license_service.clock_status()["clock_status"] == "suspected"
    assert license_service.check_license(now=base)["code"] in {"CLOCK_ROLLBACK_SUSPECTED", "CLOCK_RECOVERY_PENDING"}


def test_missing_installation_json_recovers_from_dpapi(rc12_license_env):
    _, _, _ = rc12_license_env
    first = device_identity.device_code()
    device_identity.installation_path().unlink()
    assert device_identity.device_code() == first
    assert device_identity.installation_path().exists()
    assert device_identity.installation_backup_path().exists()


def test_corrupt_installation_json_recovers_from_dpapi(rc12_license_env):
    _, _, _ = rc12_license_env
    first = device_identity.device_code()
    device_identity.installation_path().write_text("{broken", encoding="utf-8")
    assert device_identity.device_code() == first


def test_both_installation_files_missing_fail_closed(rc12_license_env):
    _, _, _ = rc12_license_env
    device_identity.device_code()
    device_identity.installation_path().unlink()
    device_identity.installation_backup_path().unlink()
    with pytest.raises(device_identity.InstallationIdentityError) as error:
        device_identity.load_or_create_installation_id()
    assert error.value.code == "INSTALLATION_ID_MISSING"


def test_corrupt_dpapi_backup_has_explicit_error(rc12_license_env):
    _, _, _ = rc12_license_env
    device_identity.device_code()
    device_identity.installation_path().unlink()
    device_identity.installation_backup_path().write_text("not-json", encoding="utf-8")
    with pytest.raises(device_identity.InstallationIdentityError) as error:
        device_identity.load_or_create_installation_id()
    assert error.value.code == "INSTALLATION_BACKUP_CORRUPTED"


def test_admin_readme_does_not_reference_missing_initializer():
    text = (ROOT / "license_admin/README.md").read_text(encoding="utf-8")
    assert "initialize_signing_identity" not in text
    assert "不包含初始化命令" in text


def test_admin_launcher_checks_dependency_and_uses_module_entrypoint():
    text = (ROOT / "start-license-generator.bat").read_text(encoding="utf-8")
    assert "Hotspot License Admin.exe" in text
    assert ".venv\\Scripts\\python.exe" in text
    assert "-m license_admin.license_generator_gui" in text


def test_admin_gui_preflights_signing_identity():
    text = (ROOT / "license_admin/license_generator_gui.py").read_text(encoding="utf-8")
    assert "load_signing_private_key()" in text
    assert "messagebox.showerror" in text
    assert text.index("load_signing_private_key()") < text.index('root.title("离线许可证签发")')


def test_recovery_smoke_contains_required_pass_marker():
    text = (ROOT / "scripts/l1_license_recovery_smoke.ps1").read_text(encoding="utf-8")
    assert "OFFLINE_LICENSE_RC1_2_3_SELF_REVIEW_PASS" in text
    assert "CLOCK_CORRECTED_TIME_RECOVERY_PASS" in text
    assert "installation_json" in text
    assert "installation_backup" in text


def test_rc12_packager_and_smoke_use_rc12_names():
    package_source = (ROOT / "scripts/package_l1.py").read_text(encoding="utf-8")
    smoke_source = (ROOT / "scripts/l1_offline_license_smoke.py").read_text(encoding="utf-8")
    assert "l1-rc1-2" in package_source
    assert "l1-rc1-2-3-windows.zip" in smoke_source


def test_generation_core_has_no_l1_2_changes():
    for relative in ("generation", "providers", "hot_sources"):
        assert (ROOT / relative).is_dir()
    assert "L1-RC1.2.3" in (ROOT / "STATUS.md").read_text(encoding="utf-8")
