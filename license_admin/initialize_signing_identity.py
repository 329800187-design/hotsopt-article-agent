from __future__ import annotations

import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from license_admin.signing_identity import private_key_path, public_key_path, public_key_sha256


def initialize() -> tuple[Path, Path]:
    private_path = private_key_path()
    private_path.parent.mkdir(parents=True, exist_ok=True)
    if private_path.exists():
        private = serialization.load_pem_private_key(private_path.read_bytes(), password=None)
        if not isinstance(private, Ed25519PrivateKey):
            raise RuntimeError("现有私钥不是 Ed25519，未修改任何文件。")
    else:
        private = Ed25519PrivateKey.generate()
        private_path.write_bytes(private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        try:
            os.chmod(private_path, 0o600)
        except OSError:
            pass
    public_path.parent.mkdir(parents=True, exist_ok=True)
    public_path.write_bytes(private.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    print(f"公钥 SHA-256：{public_key_sha256()}")
    print(f"私钥位置：{private_path}")
    print(f"公钥位置：{public_path}")
    return private_path, public_path


if __name__ == "__main__":
    initialize()
