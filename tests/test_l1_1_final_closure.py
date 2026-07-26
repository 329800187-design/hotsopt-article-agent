from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization

from license_admin.signing_identity import SigningIdentityError, load_signing_private_key, public_key_path


ROOT = Path(__file__).resolve().parents[1]


def test_runtime_requirements_include_license_dependencies():
    text = (ROOT / "requirements-runtime.txt").read_text(encoding="utf-8")
    assert "cryptography==46.0.7" in text
    assert "python-multipart==0.0.27" in text


def test_general_and_runtime_requirements_are_compatible():
    text = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "cryptography>=42,<47" in text
    assert "python-multipart>=0.0.20,<1" in text


def test_runtime_packager_reads_requirements_not_fixed_list():
    source = (ROOT / "scripts/package_rc1.py").read_text(encoding="utf-8")
    assert "_requirements_for_runtime()" in source
    assert 'pending = ["streamlit"' not in source


def test_package_l1_aborts_on_each_manifest_failure():
    source = (ROOT / "scripts/package_l1.py").read_text(encoding="utf-8")
    assert source.count("PACKAGE_SCAN_PASS") >= 4
    assert "Source package scan failed" in source
    assert "Windows package scan failed" in source
    assert "Admin package scan failed" in source
    assert "Upload package scan failed" in source


def test_admin_schema_is_self_contained():
    source = (ROOT / "license_admin/license_generator.py").read_text(encoding="utf-8")
    assert "modules.license_schema" not in source
    assert "license_admin.license_schema" in source


def test_admin_start_files_exist():
    assert (ROOT / "start-license-generator.bat").is_file()
    assert (ROOT / "requirements-admin.txt").is_file()
    assert (ROOT / "license_admin/license_schema.py").is_file()


def test_admin_gui_uses_admin_package():
    source = (ROOT / "license_admin/license_generator_gui.py").read_text(encoding="utf-8")
    assert "license_admin.license_generator" in source


def test_signing_identity_has_no_regeneration_in_loader():
    source = (ROOT / "license_admin/signing_identity.py").read_text(encoding="utf-8")
    assert "Ed25519PrivateKey.generate" not in source
    assert "不匹配" in source


def test_real_developer_private_key_matches_client_public_key():
    private = Path.home() / "hotspot-license-admin/license_private_key.pem"
    if not private.is_file():
        pytest.skip("developer signing key is not present")
    key = load_signing_private_key(private)
    assert key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ) == public_key_path().read_bytes()


def test_mismatched_private_key_is_rejected(tmp_path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    path = tmp_path / "other.pem"
    path.write_bytes(Ed25519PrivateKey.generate().private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    with pytest.raises(SigningIdentityError, match="不匹配"):
        load_signing_private_key(path)


def test_missing_private_key_is_rejected(tmp_path):
    with pytest.raises(SigningIdentityError, match="未找到"):
        load_signing_private_key(tmp_path / "missing.pem")


def test_admin_package_smoke_script_has_pass_marker():
    source = (ROOT / "scripts/l1_admin_package_smoke.ps1").read_text(encoding="utf-8")
    assert "ADMIN_LICENSE_SMOKE_PASS" in source
    assert "HOTSPOT_LICENSE_PRIVATE_KEY" in source


def test_customer_package_smoke_imports_required_modules():
    source = (ROOT / "scripts/l1_customer_package_smoke.ps1").read_text(encoding="utf-8")
    assert "import cryptography" in source
    assert "import multipart" in source
    assert "license_service.check_license()" in source


def test_real_license_smoke_uses_final_package_key():
    source = (ROOT / "scripts/l1_offline_license_smoke.py").read_text(encoding="utf-8")
    assert "l1-rc1-2-3-windows.zip" in source
    assert "license_private_key.pem" in source
    assert "monkeypatch" not in source
    assert "OFFLINE_LICENSE_REAL_KEYCHAIN_PASS" in source


def test_machine_guid_failure_is_fail_closed():
    from modules import device_identity

    original = device_identity._machine_guid
    try:
        device_identity._machine_guid = lambda: (_ for _ in ()).throw(device_identity.DeviceIdentityUnavailable())
        with pytest.raises(device_identity.DeviceIdentityUnavailable):
            device_identity.device_code()
    finally:
        device_identity._machine_guid = original


def test_weak_machine_guid_fallback_is_removed():
    source = (ROOT / "modules/device_identity.py").read_text(encoding="utf-8")
    assert "machine-guid-unavailable" not in source
    assert "DeviceIdentityUnavailable" in source


def test_restricted_ui_keeps_content_and_settings():
    source = (ROOT / "ui/rc1_app.py").read_text(encoding="utf-8")
    assert "_render_restricted_app" in source
    assert "_content(restricted=True)" in source
    assert "已有内容仍可查看、编辑和导出" in source


def test_activation_page_has_copy_and_recheck():
    source = (ROOT / "ui/rc1_app.py").read_text(encoding="utf-8")
    assert "st.code(device_value, language=None)" in source
    assert "重新检查许可证" in source
    assert "navigator.clipboard.writeText" not in source


def test_backend_generation_gate_remains_in_api():
    source = (ROOT / "api.py").read_text(encoding="utf-8")
    assert "_license_gate" in source
    assert "LICENSE_REQUIRED" in source


def test_admin_package_builder_excludes_private_generation_files():
    source = (ROOT / "scripts/package_l1.py").read_text(encoding="utf-8")
    assert '"generate_keypair.py", "initialize_signing_identity.py"' in source
    assert "private_key_included" in source


def test_expected_rc11_package_names_are_configured():
    source = (ROOT / "scripts/package_l1.py").read_text(encoding="utf-8")
    assert "hotspot-article-agent-l1-rc1-2" in source
    assert "hotspot-license-admin-l1-rc1-2" in source


def test_client_public_key_is_not_private_material():
    text = (ROOT / "resources/license_public_key.pem").read_text(encoding="utf-8")
    assert "BEGIN PUBLIC KEY" in text
    assert "PRIVATE KEY" not in text


def test_customer_package_does_not_allow_license_bypass():
    for path in (ROOT / "start.bat", ROOT / "launcher.ps1", ROOT / "scripts/phase1_smoke_test.py"):
        assert "LICENSE_DISABLED" not in path.read_text(encoding="utf-8")
