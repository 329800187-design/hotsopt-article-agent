from __future__ import annotations

import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def default_admin_root() -> Path:
    return Path(os.environ.get("HOTSPOT_LICENSE_ADMIN_ROOT", str(Path.home() / "hotspot-license-admin"))).expanduser()


def generate_keypair(output_dir: Path | None = None) -> tuple[Path, Path]:
    root = (output_dir or default_admin_root()).resolve()
    root.mkdir(parents=True, exist_ok=True)
    private_path = root / "license_private_key.pem"
    public_path = root / "license_public_key.pem"
    if private_path.exists() or public_path.exists():
        raise FileExistsError("license keypair already exists")
    private = Ed25519PrivateKey.generate()
    private_path.write_bytes(private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    public_path.write_bytes(private.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    try:
        os.chmod(private_path, 0o600)
    except OSError:
        pass
    return private_path, public_path


if __name__ == "__main__":
    private, public = generate_keypair()
    print(f"private={private}")
    print(f"public={public}")
