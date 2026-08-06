from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from modules import device_identity, license_service
from modules.license_schema import canonical_payload


FEATURES = ["hot_topics", "custom_topic", "five_articles", "image_generation", "article_editing", "word_export", "zip_export"]


@pytest.fixture
def rc122_license_env(tmp_path, monkeypatch):
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
            "license_id": "LIC-RC122-000001",
            "product": "hotspot-article-agent",
            "edition": "standard",
            "customer_name": "RC1.2.2 测试客户",
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


def _install(root: Path, value: dict) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "active.license").write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _set_future_reference(base: datetime, days: int = 2) -> datetime:
    reference = base + timedelta(days=days)
    license_service.save_secret(license_service.STATE_SECRET_NAME, reference.isoformat(), path=license_service._state_secret_path())
    return reference


def test_expired_license_cannot_recover_after_clock_rollback(rc122_license_env):
    root, base, issue = rc122_license_env
    correct_now = base + timedelta(days=2)
    _install(root, issue(expires_at=base.isoformat()))
    assert license_service.check_license(now=correct_now)["code"] == "LICENSE_EXPIRED"
    assert license_service.check_license(now=base)["code"] == "CLOCK_ROLLBACK_SUSPECTED"
    assert license_service.check_system_time(now=base + timedelta(seconds=10))["recovery_check_count"] == 0
    assert license_service.check_system_time(now=base + timedelta(seconds=20))["recoverable"] is False
    result = license_service.recover_clock_rollback(now=base + timedelta(seconds=20))
    assert result["recovered"] is False
    assert result["code"] == "CLOCK_ROLLBACK_SUSPECTED"


def test_uncorrected_clock_double_check_stays_blocked(rc122_license_env):
    root, base, issue = rc122_license_env
    _install(root, issue())
    reference = _set_future_reference(base)
    assert license_service.check_license(now=base)["code"] == "CLOCK_ROLLBACK_SUSPECTED"
    first = license_service.check_system_time(now=base + timedelta(seconds=10))
    second = license_service.check_system_time(now=base + timedelta(seconds=20))
    assert first["clock_status"] == "suspected"
    assert second["clock_status"] == "suspected"
    assert second["recovery_check_count"] == 0
    assert second["recovery_pending"] is False
    assert second["recoverable"] is False
    assert license_service.clock_status()["rollback_reference_utc"] == reference.isoformat()


def test_corrected_time_recovers_only_after_two_qualified_checks(rc122_license_env):
    root, base, issue = rc122_license_env
    _install(root, issue())
    reference = _set_future_reference(base)
    assert license_service.check_license(now=base)["code"] == "CLOCK_ROLLBACK_SUSPECTED"
    corrected = reference + timedelta(seconds=10)
    assert license_service.check_system_time(now=corrected)["recovery_check_count"] == 1
    assert license_service.check_system_time(now=corrected + timedelta(seconds=10))["recovery_ready"] is True
    result = license_service.recover_clock_rollback(now=corrected + timedelta(seconds=10))
    assert result["recovered"] is True
    assert license_service.check_license(now=corrected + timedelta(seconds=20))["valid"] is True
    assert license_service._parse_time(license_service._load_last_seen()) >= reference


def test_corrected_time_expired_license_remains_expired(rc122_license_env):
    root, base, issue = rc122_license_env
    reference = base + timedelta(days=2)
    _install(root, issue(expires_at=(reference - timedelta(hours=1)).isoformat()))
    _set_future_reference(base)
    assert license_service.check_license(now=base)["code"] != "LICENSE_EXPIRED"
    corrected = reference + timedelta(seconds=10)
    license_service.check_system_time(now=corrected)
    license_service.check_system_time(now=corrected + timedelta(seconds=10))
    result = license_service.recover_clock_rollback(now=corrected + timedelta(seconds=10))
    assert result["recovered"] is False
    assert result["code"] == "LICENSE_EXPIRED"
    assert license_service.check_license(now=corrected + timedelta(seconds=20))["code"] == "LICENSE_EXPIRED"


def test_rollback_reference_survives_state_reload(rc122_license_env):
    root, base, issue = rc122_license_env
    _install(root, issue())
    reference = _set_future_reference(base)
    license_service.check_license(now=base)
    saved = json.loads(license_service.STATE_PATH.read_text(encoding="utf-8"))
    assert saved["rollback_reference_utc"] == reference.isoformat()
    assert license_service.check_system_time(now=base + timedelta(seconds=10))["recovery_check_count"] == 0
    reloaded = json.loads(license_service.STATE_PATH.read_text(encoding="utf-8"))
    assert reloaded["rollback_reference_utc"] == reference.isoformat()
    assert reloaded["clock_status"] == "suspected"


def test_dpapi_last_seen_is_monotonic_during_rollback(rc122_license_env):
    root, base, issue = rc122_license_env
    _install(root, issue())
    reference = _set_future_reference(base)
    license_service.check_license(now=base)
    assert license_service._parse_time(license_service._load_last_seen()) >= reference


def test_fake_dpapi_creates_non_secret_backup_file(rc122_license_env):
    _, _, _ = rc122_license_env
    installation_id = device_identity.load_or_create_installation_id()
    backup = device_identity.installation_backup_path()
    assert backup.exists()
    text = backup.read_text(encoding="utf-8")
    if __import__("os").name != "nt":
        assert "FAKE_DPAPI_TEST_BLOB" in text
    assert installation_id not in text


def test_rc122_recovery_smoke_has_separate_blocked_and_corrected_markers():
    source = (Path(__file__).resolve().parents[1] / "scripts/l1_license_recovery_smoke.ps1").read_text(encoding="utf-8")
    assert "CLOCK_ROLLBACK_STILL_BLOCKED_PASS" in source
    assert "CLOCK_CORRECTED_TIME_RECOVERY_PASS" in source
    assert "corrected_time >= previous" in source
