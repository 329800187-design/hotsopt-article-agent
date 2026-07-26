from __future__ import annotations

import base64
import ctypes
import json
import logging
import os
import tempfile
from ctypes import wintypes
from pathlib import Path
from typing import Any

from modules.app_paths import config_dir


logger = logging.getLogger(__name__)


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


CRYPTPROTECT_UI_FORBIDDEN = 0x01
_CREDENTIAL_FILE = "credentials.dat"


def credential_path() -> Path:
    return config_dir() / _CREDENTIAL_FILE


def _blob_from_bytes(data: bytes) -> DATA_BLOB:
    buffer = ctypes.create_string_buffer(data)
    blob = DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    blob._buffer = buffer  # type: ignore[attr-defined]
    return blob


def _bytes_from_blob(blob: DATA_BLOB) -> bytes:
    if not blob.pbData:
        return b""
    try:
        return ctypes.string_at(blob.pbData, blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob.pbData)


def protect_bytes(data: bytes) -> bytes:
    if os.name != "nt":
        raise RuntimeError("DPAPI is only available on Windows")
    input_blob = _blob_from_bytes(data)
    output_blob = DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(input_blob),
        None,
        None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output_blob),
    )
    if not ok:
        raise ctypes.WinError()
    return _bytes_from_blob(output_blob)


def unprotect_bytes(data: bytes) -> bytes:
    if os.name != "nt":
        raise RuntimeError("DPAPI is only available on Windows")
    input_blob = _blob_from_bytes(data)
    output_blob = DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(input_blob),
        None,
        None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output_blob),
    )
    if not ok:
        raise ctypes.WinError()
    return _bytes_from_blob(output_blob)


def _read_store(path: Path | None = None) -> dict[str, Any]:
    target = path or credential_path()
    if not target.exists():
        return {"version": 1, "secrets": {}}
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        backup = target.with_suffix(target.suffix + ".bak")
        if backup.exists():
            backup = target.with_suffix(target.suffix + ".bak.1")
        try:
            target.replace(backup)
            logger.warning("credential store backup created after read failure: %s", type(exc).__name__)
        except OSError as exc:
            logger.warning("credential store backup failed: %s", type(exc).__name__)
        return {"version": 1, "secrets": {}}


def _write_store(data: dict[str, Any], path: Path | None = None) -> None:
    target = path or credential_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=target.name, suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        Path(temporary).replace(target)
    finally:
        try:
            Path(temporary).unlink(missing_ok=True)
        except OSError:
            pass


def save_secret(name: str, secret: str, path: Path | None = None) -> str:
    secret = str(secret or "")
    reference = f"dpapi:{name}"
    data = _read_store(path)
    data.setdefault("secrets", {})[reference] = base64.b64encode(protect_bytes(secret.encode("utf-8"))).decode("ascii")
    _write_store(data, path)
    return reference


def load_secret(reference: str | None, path: Path | None = None) -> str:
    if not reference:
        return ""
    data = _read_store(path)
    encrypted = (data.get("secrets") or {}).get(reference)
    if not encrypted:
        return ""
    return unprotect_bytes(base64.b64decode(encrypted)).decode("utf-8")


def delete_secret(reference: str | None, path: Path | None = None) -> None:
    if not reference:
        return
    data = _read_store(path)
    secrets = data.setdefault("secrets", {})
    if reference in secrets:
        secrets.pop(reference, None)
        _write_store(data, path)
