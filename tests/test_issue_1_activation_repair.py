from __future__ import annotations

import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.rsa import generate_private_key

from license_admin import signing_identity
from modules import app_paths, device_identity
from modules.app_metadata import APP_VERSION, DATA_DIR_NAME, LICENSE_ADMIN_EXE_NAME


def _complete_identity(root: Path, installation_id: str) -> None:
    license_root = root / "license"
    license_root.mkdir(parents=True)
    (license_root / "installation.json").write_text(
        json.dumps({"schema_version": "1", "installation_id": installation_id}),
        encoding="utf-8",
    )
    (license_root / "installation.dat").write_text("test-dpapi-placeholder", encoding="utf-8")
    (license_root / "installation.initialized").write_text('{"initialized":"true"}', encoding="utf-8")


def test_windows_default_and_explicit_data_roots(monkeypatch, tmp_path):
    monkeypatch.delenv("HOTSPOT_DATA_ROOT", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(app_paths, "is_windows", lambda: True)
    assert app_paths.data_root() == (tmp_path / DATA_DIR_NAME).resolve()
    explicit = tmp_path / "explicit"
    monkeypatch.setenv("HOTSPOT_DATA_ROOT", str(explicit))
    assert app_paths.data_root() == explicit.resolve()


def test_all_application_paths_derive_from_one_root(monkeypatch, tmp_path):
    monkeypatch.setenv("HOTSPOT_DATA_ROOT", str(tmp_path))
    assert app_paths.config_dir() == tmp_path / "config"
    assert app_paths.database_path() == tmp_path / "data" / "hotspot_agent.db"
    assert app_paths.tasks_root() == tmp_path / "data" / "tasks"
    assert app_paths.research_root() == tmp_path / "data" / "research"
    assert app_paths.model_test_root() == tmp_path / "data" / "model-tests"
    assert app_paths.cache_path() == tmp_path / "cache" / "latest_topics.json"
    assert app_paths.exports_root() == tmp_path / "exports"
    assert app_paths.logs_root() == tmp_path / "logs"
    assert app_paths.runtime_root() == tmp_path / "runtime"
    assert app_paths.license_root() == tmp_path / "license"
    assert app_paths.updates_root() == tmp_path / "updates"


def test_legacy_short_directory_migrates_atomically(monkeypatch, tmp_path):
    target = tmp_path / "official"
    legacy = tmp_path / "short"
    _complete_identity(legacy, "same-installation-id")
    (legacy / "config").mkdir()
    (legacy / "config" / "settings.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(app_paths, "data_root", lambda: target)
    monkeypatch.setattr(app_paths, "legacy_data_roots", lambda: [legacy])
    result = app_paths.migrate_legacy_data()
    assert result["status"] == "migrated"
    assert json.loads((target / "license" / "installation.json").read_text())["installation_id"] == "same-installation-id"
    assert (target / "config" / "settings.json").is_file()
    assert (target / "logs" / app_paths.MIGRATION_REPORT_NAME).is_file()


def test_equal_legacy_identities_choose_one_without_changing_id(monkeypatch, tmp_path):
    target = tmp_path / "official"
    first, second = tmp_path / "old-a", tmp_path / "old-b"
    _complete_identity(first, "same-id")
    _complete_identity(second, "same-id")
    monkeypatch.setattr(app_paths, "data_root", lambda: target)
    monkeypatch.setattr(app_paths, "legacy_data_roots", lambda: [first, second])
    result = app_paths.migrate_legacy_data()
    assert result["status"] == "migrated"
    assert json.loads((target / "license" / "installation.json").read_text())["installation_id"] == "same-id"


def test_different_legacy_identities_stop_with_conflict(monkeypatch, tmp_path):
    target = tmp_path / "official"
    first, second = tmp_path / "old-a", tmp_path / "old-b"
    _complete_identity(first, "first-id")
    _complete_identity(second, "second-id")
    monkeypatch.setattr(app_paths, "data_root", lambda: target)
    monkeypatch.setattr(app_paths, "legacy_data_roots", lambda: [first, second])
    result = app_paths.migrate_legacy_data()
    assert result["status"] == "conflict"
    assert result["conflict_code"] == "IDENTITY_MIGRATION_CONFLICT"
    assert not (target / "license" / "installation.json").exists()


def test_existing_official_identity_is_never_overwritten(monkeypatch, tmp_path):
    target, legacy = tmp_path / "official", tmp_path / "old"
    _complete_identity(target, "official-id")
    _complete_identity(legacy, "legacy-id")
    monkeypatch.setattr(app_paths, "data_root", lambda: target)
    monkeypatch.setattr(app_paths, "legacy_data_roots", lambda: [legacy])
    result = app_paths.migrate_legacy_data()
    assert result["status"] == "not_needed"
    assert json.loads((target / "license" / "installation.json").read_text())["installation_id"] == "official-id"


def test_device_status_reports_safe_explicit_error(monkeypatch, tmp_path):
    monkeypatch.setattr(device_identity, "data_root", lambda: tmp_path)
    monkeypatch.setattr(device_identity, "license_root", lambda: tmp_path / "license")
    monkeypatch.setattr(device_identity, "launch_mode", lambda: "source")
    monkeypatch.setattr(device_identity, "migrate_legacy_data", lambda: {"status": "not_needed", "source": ""})
    monkeypatch.setattr(
        device_identity,
        "load_or_create_installation_id",
        lambda: (_ for _ in ()).throw(
            device_identity.InstallationIdentityError("MACHINE_GUID_UNAVAILABLE", "无法读取本机设备标识。")
        ),
    )
    result = device_identity.device_status()
    assert result["installation_error"] == "MACHINE_GUID_UNAVAILABLE"
    assert result["launch_mode"] == "source"
    assert "installation_id" not in result


def test_fresh_device_identity_is_stable_across_reloads(monkeypatch, tmp_path):
    license_dir = tmp_path / "license"
    secret_store: dict[str, str] = {}
    monkeypatch.setattr(device_identity, "license_root", lambda: license_dir)
    monkeypatch.setattr(device_identity, "migrate_legacy_data", lambda: {"status": "not_needed"})
    monkeypatch.setattr(device_identity, "_machine_guid", lambda: "stable-machine-guid")

    def save_secret(_name, value, *, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"protected":true}', encoding="utf-8")
        secret_store[str(path)] = value

    monkeypatch.setattr(device_identity, "save_secret", save_secret)
    monkeypatch.setattr(
        device_identity,
        "load_secret",
        lambda _name, *, path: secret_store[str(path)],
    )
    first = device_identity.device_code()
    second = device_identity.device_code()
    assert first == second
    assert (license_dir / "installation.json").is_file()
    assert (license_dir / "installation.dat").is_file()


def _write_private(path: Path, key) -> None:
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )


def test_signer_preflight_classifies_missing_and_ready(monkeypatch, tmp_path):
    private_path = tmp_path / "private.pem"
    public_path = tmp_path / "public.pem"
    monkeypatch.setattr(signing_identity, "public_key_path", lambda: public_path)
    missing = signing_identity.signer_preflight(private_path)
    assert missing["code"] == "LICENSE_PRIVATE_KEY_MISSING"
    key = Ed25519PrivateKey.generate()
    _write_private(private_path, key)
    public_path.write_bytes(
        key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    )
    ready = signing_identity.signer_preflight(private_path)
    assert ready["code"] == "LICENSE_SIGNER_READY"
    assert ready["keypair_matches"] is True


def test_signer_preflight_classifies_unsupported_and_mismatch(monkeypatch, tmp_path):
    private_path = tmp_path / "private.pem"
    public_path = tmp_path / "public.pem"
    monkeypatch.setattr(signing_identity, "public_key_path", lambda: public_path)
    _write_private(private_path, generate_private_key(public_exponent=65537, key_size=2048))
    public_path.write_text("invalid", encoding="utf-8")
    assert signing_identity.signer_preflight(private_path)["code"] == "LICENSE_PRIVATE_KEY_UNSUPPORTED"
    first, second = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    _write_private(private_path, first)
    public_path.write_bytes(
        second.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    )
    assert signing_identity.signer_preflight(private_path)["code"] == "LICENSE_KEYPAIR_MISMATCH"


def test_signer_preflight_classifies_corrupt_and_public_key_failures(monkeypatch, tmp_path):
    private_path = tmp_path / "private.pem"
    public_path = tmp_path / "public.pem"
    monkeypatch.setattr(signing_identity, "public_key_path", lambda: public_path)
    private_path.write_text("not a private key", encoding="utf-8")
    assert signing_identity.signer_preflight(private_path)["code"] == "LICENSE_PRIVATE_KEY_UNREADABLE"
    key = Ed25519PrivateKey.generate()
    _write_private(private_path, key)
    assert signing_identity.signer_preflight(private_path)["code"] == "LICENSE_PUBLIC_KEY_MISSING"
    public_path.write_text("not a public key", encoding="utf-8")
    assert signing_identity.signer_preflight(private_path)["code"] == "LICENSE_PUBLIC_KEY_INVALID"


def test_signer_launcher_priority_and_metadata_are_consistent():
    launcher = Path("start-license-generator.bat").read_text(encoding="utf-8")
    assert launcher.index(LICENSE_ADMIN_EXE_NAME) < launcher.index("Hotspot License Admin.exe")
    assert launcher.index("Hotspot License Admin.exe") < launcher.index(".venv\\Scripts\\python.exe")
    assert launcher.index(".venv\\Scripts\\python.exe") < launcher.index("py -3")
    assert launcher.index("py -3") < launcher.index("python -m")
    assert APP_VERSION in Path("README.md").read_text(encoding="utf-8")
    assert APP_VERSION in Path("STATUS.md").read_text(encoding="utf-8")
    assert APP_VERSION in Path("packaging/setup_bootstrapper.cs").read_text(encoding="utf-8")
