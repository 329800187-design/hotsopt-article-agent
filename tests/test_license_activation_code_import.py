from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from modules import device_identity, license_service
from modules.license_schema import canonical_payload


FEATURES = [
    "hot_topics",
    "custom_topic",
    "five_articles",
    "image_generation",
    "article_editing",
    "word_export",
    "zip_export",
]


def test_base64_activation_code_import_accepts_hyphenated_device_code(tmp_path, monkeypatch):
    root = tmp_path / "license"
    private = Ed25519PrivateKey.generate()
    public_path = tmp_path / "license_public_key.pem"
    public_path.write_bytes(
        private.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    monkeypatch.setattr(device_identity, "license_root", lambda: root)
    monkeypatch.setattr(license_service, "license_root", lambda: root)
    monkeypatch.setattr(license_service, "PUBLIC_KEY_PATH", public_path)
    monkeypatch.setattr(license_service, "ACTIVE_LICENSE_PATH", root / "active.license")
    monkeypatch.setattr(license_service, "STATE_PATH", root / "license_state.json")

    now = datetime.now(timezone.utc).replace(microsecond=0)
    payload = {
        "schema_version": 1,
        "license_id": "LIC-ACTIVATION-0001",
        "product": "hotspot-article-agent",
        "edition": "standard",
        "customer_name": "Activation Test",
        "device_code": device_identity.device_code(),
        "issued_at": now.isoformat(),
        "not_before": (now - timedelta(minutes=1)).isoformat(),
        "expires_at": (now + timedelta(days=30)).isoformat(),
        "features": FEATURES,
        "signature_algorithm": "Ed25519",
    }
    payload["signature"] = base64.urlsafe_b64encode(
        private.sign(canonical_payload(payload))
    ).decode("ascii").rstrip("=")
    activation_code = base64.urlsafe_b64encode(
        (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    ).decode("ascii")

    result = license_service.import_license_text(activation_code)

    assert result["valid"] is True
    assert result["license"]["license_id"] == "LIC-ACTIVATION-0001"
    assert json.loads(license_service.ACTIVE_LICENSE_PATH.read_text(encoding="utf-8"))["device_code"] == payload["device_code"]
