from __future__ import annotations

import base64
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def install_signed_test_license() -> Path:
    from modules import device_identity, license_service
    from modules.license_schema import canonical_payload

    root = Path(tempfile.mkdtemp(prefix="hotspot-smoke-license-"))
    private = Ed25519PrivateKey.generate()
    public = root / "license_public_key.pem"
    public.write_bytes(private.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    device_identity.license_root = lambda: root / "license"
    license_service.license_root = lambda: root / "license"
    license_service.PUBLIC_KEY_PATH = public
    license_service.ACTIVE_LICENSE_PATH = root / "license" / "active.license"
    license_service.STATE_PATH = root / "license" / "license_state.json"
    now = datetime.now(timezone.utc).replace(microsecond=0)
    value = {
        "schema_version": 1,
        "license_id": "SMOKE-L1-000001",
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
    return root
