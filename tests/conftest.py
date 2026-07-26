from __future__ import annotations

import os
import base64
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


os.environ.setdefault("HOTSPOT_ALLOW_UNAUTHENTICATED_TEST_API", "1")


def _install_signed_test_license(root: Path) -> None:
    from modules import device_identity, license_service
    from modules.license_schema import canonical_payload

    device_identity.license_root = lambda: root / "license"
    root.mkdir(parents=True, exist_ok=True)
    private = Ed25519PrivateKey.generate()
    public_path = root / "license_public_key.pem"
    public_path.write_bytes(private.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    license_service.PUBLIC_KEY_PATH = public_path
    license_service.license_root = lambda: root / "license"
    license_service.ACTIVE_LICENSE_PATH = root / "license" / "active.license"
    license_service.STATE_PATH = root / "license" / "license_state.json"
    now = datetime.now(timezone.utc).replace(microsecond=0)
    value = {
        "schema_version": 1,
        "license_id": "TEST-L1-000001",
        "product": "hotspot-article-agent",
        "edition": "test",
        "customer_name": "测试夹具",
        "device_code": device_identity.device_code(),
        "issued_at": now.isoformat(),
        "not_before": (now - timedelta(minutes=1)).isoformat(),
        "expires_at": (now + timedelta(days=1)).isoformat(),
        "features": ["hot_topics", "custom_topic", "five_articles", "image_generation", "article_editing", "word_export", "zip_export"],
        "signature_algorithm": "Ed25519",
    }
    value["signature"] = base64.urlsafe_b64encode(private.sign(canonical_payload(value))).decode("ascii").rstrip("=")
    license_service.ACTIVE_LICENSE_PATH.parent.mkdir(parents=True, exist_ok=True)
    license_service.ACTIVE_LICENSE_PATH.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


@pytest.fixture(autouse=True)
def signed_test_license(monkeypatch, tmp_path):
    if os.name != "nt":
        secrets: dict[tuple[str, str], str] = {}

        def fake_save(name: str, secret: str, path: Path | None = None) -> str:
            secrets[(str(path), name)] = secret
            if path is not None:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    json.dumps({"schema_version": "fake-dpapi-v1", "blob": "FAKE_DPAPI_TEST_BLOB"}),
                    encoding="utf-8",
                )
            return f"dpapi:{name}"

        def fake_load(reference: str | None, path: Path | None = None) -> str:
            return secrets.get((str(path), str(reference or "").removeprefix("dpapi:")), "")

        from modules import device_identity, license_service

        monkeypatch.setattr(device_identity, "save_secret", fake_save)
        monkeypatch.setattr(device_identity, "load_secret", fake_load)
        monkeypatch.setattr(license_service, "save_secret", fake_save)
        monkeypatch.setattr(license_service, "load_secret", fake_load)
    _install_signed_test_license(tmp_path / "license-root")
