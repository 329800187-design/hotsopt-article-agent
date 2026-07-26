from __future__ import annotations

import hashlib
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey


class SigningIdentityError(RuntimeError):
    pass


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def public_key_path() -> Path:
    return project_root() / "resources" / "license_public_key.pem"


def private_key_path() -> Path:
    return Path(os.environ.get("HOTSPOT_LICENSE_PRIVATE_KEY", str(Path.home() / "hotspot-license-admin" / "license_private_key.pem"))).expanduser()


def _public_bytes(key: Ed25519PublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)


def public_key_sha256(path: Path | None = None) -> str:
    return hashlib.sha256((path or public_key_path()).read_bytes()).hexdigest()


def load_signing_private_key(path: Path | None = None) -> Ed25519PrivateKey:
    target = path or private_key_path()
    if not target.is_file():
        raise SigningIdentityError("未找到许可证签发私钥，请恢复开发者私钥备份。不要重新生成密钥，否则现有客户端将无法验证许可证。")
    try:
        key = serialization.load_pem_private_key(target.read_bytes(), password=None)
    except Exception as exc:
        raise SigningIdentityError("许可证签发私钥无法读取，请恢复开发者私钥备份。") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise SigningIdentityError("许可证签发私钥算法不受支持。")
    if not public_key_path().is_file():
        raise SigningIdentityError("客户端公钥不存在，无法安全签发许可证。")
    derived = _public_bytes(key.public_key())
    try:
        client_key = serialization.load_pem_public_key(public_key_path().read_bytes())
    except Exception as exc:
        raise SigningIdentityError("客户端公钥无法读取。") from exc
    if not isinstance(client_key, Ed25519PublicKey) or derived != _public_bytes(client_key):
        raise SigningIdentityError("当前签发私钥与客户端公钥不匹配，生成的许可证无法使用。")
    return key
