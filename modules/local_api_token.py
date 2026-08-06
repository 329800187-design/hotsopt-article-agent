from __future__ import annotations

import base64
import os
import secrets
from pathlib import Path

from modules.app_paths import runtime_root
from modules.credential_store import protect_bytes, unprotect_bytes


TOKEN_FILE = "local-api-token.dat"


def token_path() -> Path:
    return runtime_root() / TOKEN_FILE


def _valid_token(value: str) -> bool:
    raw = value.encode("utf-8")
    if len(raw) >= 32:
        return True
    try:
        return len(base64.b64decode(value, validate=True)) >= 32
    except Exception:
        return False


def read_token(path: Path | None = None) -> str:
    target = path or token_path()
    if not target.exists():
        return ""
    try:
        encrypted = base64.b64decode(target.read_text(encoding="utf-8").strip(), validate=True)
        value = unprotect_bytes(encrypted).decode("utf-8")
        return value if _valid_token(value) else ""
    except Exception:
        return ""


def write_token(value: str, path: Path | None = None) -> str:
    if not _valid_token(value):
        raise ValueError("local API token must contain at least 256 bits of entropy")
    target = path or token_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(base64.b64encode(protect_bytes(value.encode("utf-8"))).decode("ascii"), encoding="utf-8")
    return value


def get_or_create_token(path: Path | None = None) -> str:
    existing = read_token(path)
    if existing:
        return existing
    return write_token(secrets.token_urlsafe(48), path)


def delete_token(path: Path | None = None) -> None:
    target = path or token_path()
    try:
        target.unlink(missing_ok=True)
    except OSError:
        pass
