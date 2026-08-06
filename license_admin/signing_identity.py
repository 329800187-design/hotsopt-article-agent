from __future__ import annotations

import hashlib
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from modules.app_metadata import APP_VERSION, BUILD_COMMIT


class SigningIdentityError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


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


def _load_signing_private_key(path: Path | None = None) -> Ed25519PrivateKey:
    target = path or private_key_path()
    if not target.is_file():
        raise SigningIdentityError("LICENSE_PRIVATE_KEY_MISSING", "未找到许可证签发私钥，请恢复原始开发者私钥备份。")
    try:
        key = serialization.load_pem_private_key(target.read_bytes(), password=None)
    except Exception as exc:
        raise SigningIdentityError("LICENSE_PRIVATE_KEY_UNREADABLE", "许可证签发私钥无法读取，请恢复原始备份。") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise SigningIdentityError("LICENSE_PRIVATE_KEY_UNSUPPORTED", "许可证签发私钥算法不受支持。")
    if not public_key_path().is_file():
        raise SigningIdentityError("LICENSE_PUBLIC_KEY_MISSING", "客户端公钥不存在，无法安全签发许可证。")
    derived = _public_bytes(key.public_key())
    try:
        client_key = serialization.load_pem_public_key(public_key_path().read_bytes())
    except Exception as exc:
        raise SigningIdentityError("LICENSE_PUBLIC_KEY_INVALID", "客户端公钥无法读取。") from exc
    if not isinstance(client_key, Ed25519PublicKey) or derived != _public_bytes(client_key):
        raise SigningIdentityError("LICENSE_KEYPAIR_MISMATCH", "当前签发私钥与客户端公钥不匹配，已拒绝签发。")
    return key


def load_signing_private_key(path: Path | None = None) -> Ed25519PrivateKey:
    return _load_signing_private_key(path)


def signer_preflight(path: Path | None = None) -> dict[str, object]:
    target = path or private_key_path()
    public = public_key_path()
    code = "LICENSE_SIGNER_READY"
    message = "签发身份预检通过。"
    matches = False
    try:
        _load_signing_private_key(target)
        matches = True
    except SigningIdentityError as exc:
        code = exc.code
        message = exc.message
    return {
        "code": code,
        "message": message,
        "private_key_path": str(target),
        "private_key_exists": target.is_file(),
        "public_key_path": str(public),
        "keypair_matches": matches,
        "app_version": APP_VERSION,
        "build_commit": BUILD_COMMIT,
        "ready": code == "LICENSE_SIGNER_READY",
    }
