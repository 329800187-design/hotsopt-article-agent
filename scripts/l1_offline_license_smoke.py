from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
WINDOWS_PACKAGE = ROOT / "hotspot-article-agent-l1-rc1-2-3-windows.zip"


def main() -> int:
    private_path = Path(os.environ.get("HOTSPOT_LICENSE_PRIVATE_KEY", str(Path.home() / "hotspot-license-admin" / "license_private_key.pem"))).expanduser()
    if not private_path.is_file():
        raise SystemExit("REAL_KEYCHAIN_PENDING: developer private key is missing")
    if not WINDOWS_PACKAGE.is_file():
        raise SystemExit("REAL_KEYCHAIN_PENDING: final Windows package is missing")
    with zipfile.ZipFile(WINDOWS_PACKAGE) as archive:
        packaged_public = archive.read("resources/license_public_key.pem")
    project_public = (ROOT / "resources" / "license_public_key.pem").read_bytes()
    if packaged_public != project_public:
        raise SystemExit("REAL_KEYCHAIN_FAILED: packaged public key differs")
    with tempfile.TemporaryDirectory(prefix="l1-real-keychain-") as temporary:
        os.environ["HOTSPOT_DATA_ROOT"] = temporary
        from license_admin.license_generator import create_license, write_license
        from modules import device_identity, license_service
        from modules.license_schema import LicenseValidationError, canonical_payload

        now = datetime.now(timezone.utc).replace(microsecond=0)
        value = create_license(
            customer_name="L1 Real Keychain Smoke",
            device_code=device_identity.device_code(),
            license_id="SMOKE-L1-REAL-0001",
            not_before=(now - timedelta(minutes=1)).isoformat(),
            expires_at=(now + timedelta(days=30)).isoformat(),
            private_key=private_path,
        )
        candidate = Path(temporary) / "real.license"
        write_license(value, candidate)
        imported = license_service.import_license(candidate)
        assert imported["valid"] is True
        allowed, _ = license_service.license_allows_generation("five_articles")
        assert allowed is True
        tampered = dict(value)
        tampered["customer_name"] = "tampered"
        try:
            license_service.validate_license(tampered)
        except LicenseValidationError:
            pass
        else:
            raise AssertionError("tampered license was accepted")
        try:
            license_service.validate_license(value, expected_device="AAAA-BBBB-CCCC-DDDD-EEEE")
        except LicenseValidationError:
            pass
        else:
            raise AssertionError("wrong device was accepted")
        expired = create_license(
            customer_name="L1 Expired Smoke",
            device_code=device_identity.device_code(),
            license_id="SMOKE-L1-EXPIRED",
            not_before=(now - timedelta(days=2)).isoformat(),
            expires_at=(now - timedelta(days=1)).isoformat(),
            private_key=private_path,
        )
        try:
            license_service.validate_license(expired)
        except LicenseValidationError as exc:
            assert exc.code == "LICENSE_EXPIRED"
        else:
            raise AssertionError("expired license was accepted")
        print("WINDOWS_RUNTIME_LICENSE_IMPORT_PASS")
        print("OFFLINE_LICENSE_REAL_KEYCHAIN_PASS")
        print("OFFLINE_LICENSE_SMOKE_PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
