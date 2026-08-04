from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from modules.app_paths import PROJECT_ROOT, config_dir, ensure_user_data_dirs, settings_path
from modules.credential_store import delete_secret, load_secret, save_secret
from generation.image_budget import recommended_word_count
from providers.registry import default_profile


ROOT = PROJECT_ROOT
CONFIG_DIR = config_dir()
SETTINGS_PATH = settings_path()
EXAMPLE_PATH = ROOT / "config" / "settings.example.json"
logger = logging.getLogger(__name__)


def _default_text_profile() -> dict[str, Any]:
    profile = default_profile("text").to_runtime_profile()
    profile.update({
        "api_key": "",
        "headers": {},
        "timeout_seconds": 180,
        "response_format": "json_object",
        "enabled": True,
    })
    return profile


def _default_image_profile() -> dict[str, Any]:
    profile = default_profile("image").to_runtime_profile()
    profile.update({
        "api_key": "",
        "headers": {},
        "timeout_seconds": 180,
        "response_type": "auto",
        "enabled": True,
    })
    return profile

DEFAULT_SETTINGS: dict[str, Any] = {
    "app_mode": "production",
    "demo_mode": False,
    "research_gate_enabled": True,
    "phase2a_word_count": 1200,
    "image_plan_mode": "none",
    "image_call_budget_per_article": 0,
    "image_call_budget_per_batch": 0,
    "image_unit_price": 0.10,
    "max_auto_retries": 0,
    "share_text_image_credentials": False,
    "hot_source_url": "https://api-hot.imsyy.top/toutiao",
    "hot_cache_ttl_seconds": 21600,
    "network": {"mode": "system", "http_proxy": "", "https_proxy": "", "timeout_seconds": 15, "verify_ssl": True},
    "verified_text_model": None,
    "verified_text_base_url": None,
    "verified_text_endpoint": None,
    "verified_at": None,
    "last_text_model_test_at": None,
    "resolved_text_model": None,
    "resolved_text_provider": None,
    "resolved_text_base_url_hash": None,
    "resolved_text_verified_at": None,
    "resolved_text_capability_status": "",
    "resolved_text_parser_mode": "",
    "text_profile": _default_text_profile(),
    "image_profile": _default_image_profile(),
}


def _merge(default: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    result = dict(default)
    for key, value in current.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def load_settings() -> dict[str, Any]:
    ensure_user_data_dirs()
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not SETTINGS_PATH.exists():
        try:
            save_settings(DEFAULT_SETTINGS)
        except Exception as exc:
            _log_credential_failure(exc, "initial settings persistence")
            return _settings_with_runtime_secrets(dict(DEFAULT_SETTINGS), migration_error=True)
        return _settings_with_runtime_secrets(dict(DEFAULT_SETTINGS))
    try:
        with SETTINGS_PATH.open("r", encoding="utf-8") as handle:
            raw_settings = json.load(handle)
            settings = _merge(DEFAULT_SETTINGS, raw_settings)
        settings, word_count_migrated = _migrate_word_count_settings(settings)
        settings, migrated = _migrate_legacy_credentials(settings, raw_settings)
        migrated = migrated or word_count_migrated
        if migrated:
            save_settings(settings)
            settings = _merge(DEFAULT_SETTINGS, json.loads(SETTINGS_PATH.read_text(encoding="utf-8")))
        if _has_plaintext_key(settings):
            try:
                save_settings(settings)
            except Exception as exc:
                _log_credential_failure(exc, "plaintext credential migration")
                return _settings_with_runtime_secrets(settings, migration_error=True)
            with SETTINGS_PATH.open("r", encoding="utf-8") as handle:
                settings = _merge(DEFAULT_SETTINGS, json.load(handle))
        return _settings_with_runtime_secrets(settings)
    except (OSError, json.JSONDecodeError):
        return _settings_with_runtime_secrets(dict(DEFAULT_SETTINGS))


def _migrate_legacy_credentials(settings: dict[str, Any], raw_settings: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Preserve old single-key installs while creating two independent DPAPI refs."""
    raw_text = dict(raw_settings.get("text_profile") or {})
    raw_image = dict(raw_settings.get("image_profile") or {})
    legacy_key = str(raw_settings.get("api_key") or "")
    text_key = str(raw_text.get("api_key") or "")
    text_ref = str(raw_text.get("credential_ref") or "")
    image_has_key = bool(raw_image.get("api_key") or raw_image.get("credential_ref") or raw_image.get("has_api_key"))
    if not legacy_key and not text_key and not text_ref:
        return settings, False
    if image_has_key:
        return settings, False
    if legacy_key:
        source_key = legacy_key
    else:
        try:
            source_key = load_secret(text_ref) if text_ref else ""
        except Exception:
            source_key = ""
    if not source_key:
        return settings, False
    result = json.loads(json.dumps(settings))
    result.setdefault("image_profile", {})["api_key"] = source_key
    result["image_profile"]["has_api_key"] = True
    result["credential_migration_notice"] = "旧版本单一密钥已复制为独立文本/图片凭据，请分别测试。"
    result["share_text_image_credentials"] = False
    return result, True


def _migrate_word_count_settings(settings: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    result = json.loads(json.dumps(settings))
    migrated = False
    current = result.get("phase2a_word_count")
    normalized = recommended_word_count(current)
    if current != normalized:
        result["phase2a_word_count"] = normalized
        migrated = True
    return result, migrated


def save_settings(settings: dict[str, Any]) -> None:
    ensure_user_data_dirs()
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    persisted = _settings_for_persistence(settings)
    fd, temporary = tempfile.mkstemp(prefix="settings", suffix=".tmp", dir=str(CONFIG_DIR))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(persisted, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        Path(temporary).replace(SETTINGS_PATH)
    finally:
        try:
            Path(temporary).unlink(missing_ok=True)
        except OSError:
            pass


def public_settings(settings: dict[str, Any]) -> dict[str, Any]:
    result = json.loads(json.dumps(settings))
    for profile_name in ("text_profile", "image_profile"):
        if profile_name in result:
            has_key = bool(result[profile_name].get("has_api_key") or result[profile_name].get("credential_ref"))
            result[profile_name]["api_key"] = "***" if has_key else ""
    return result


def _has_plaintext_key(settings: dict[str, Any]) -> bool:
    for profile_name in ("text_profile", "image_profile"):
        value = str((settings.get(profile_name) or {}).get("api_key") or "")
        if value and value != "***":
            return True
    return False


def _settings_for_persistence(settings: dict[str, Any]) -> dict[str, Any]:
    persisted = _merge(DEFAULT_SETTINGS, json.loads(json.dumps(settings)))
    preserve_text_resolution = bool(persisted.pop("_preserve_text_resolution_on_save", False))
    persisted.pop("credential_migration_error", None)
    persisted.pop("credential_available", None)
    current = {}
    if SETTINGS_PATH.exists():
        try:
            current = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            current = {}
    text_resolution_invalidated = False
    existing_text_profile = dict((current.get("text_profile") or {}) if isinstance(current.get("text_profile"), dict) else {})
    new_text_profile = dict((persisted.get("text_profile") or {}) if isinstance(persisted.get("text_profile"), dict) else {})
    for key in ("provider_id", "base_url", "endpoint", "auth_type", "auth_header"):
        if str(existing_text_profile.get(key) or "").strip() != str(new_text_profile.get(key) or "").strip():
            text_resolution_invalidated = True
            break

    for profile_name in ("text_profile", "image_profile"):
        profile = dict(persisted.get(profile_name) or {})
        existing = dict((current.get(profile_name) or {}) if isinstance(current.get(profile_name), dict) else {})
        key_value = str(profile.get("api_key") or "")
        credential_ref = str(profile.get("credential_ref") or existing.get("credential_ref") or f"dpapi:{profile_name}_api_key")
        if profile.pop("clear_api_key", False):
            delete_secret(credential_ref)
            profile["has_api_key"] = False
            credential_ref = ""
        elif key_value and key_value != "***":
            if profile_name == "text_profile":
                text_resolution_invalidated = True
            credential_ref = save_secret(f"{profile_name}_api_key", key_value)
            profile["has_api_key"] = True
        else:
            profile["has_api_key"] = bool(profile.get("has_api_key") or existing.get("has_api_key") or existing.get("credential_ref"))
        profile["credential_ref"] = credential_ref if profile["has_api_key"] else ""
        profile.pop("api_key", None)
        persisted[profile_name] = profile
    if text_resolution_invalidated and not preserve_text_resolution:
        _clear_text_resolution_fields(persisted)
    return persisted


def _clear_text_resolution_fields(settings: dict[str, Any]) -> None:
    for key in (
        "resolved_text_model",
        "resolved_text_provider",
        "resolved_text_base_url_hash",
        "resolved_text_verified_at",
        "resolved_text_capability_status",
        "resolved_text_parser_mode",
        "verified_text_model",
        "verified_text_base_url",
        "verified_text_endpoint",
        "verified_at",
    ):
        settings[key] = "" if key.endswith(("status", "mode")) else None


def _log_credential_failure(error: Exception, operation: str) -> None:
    logger.warning("credential operation failed (%s): %s", operation, type(error).__name__)


def _settings_with_runtime_secrets(settings: dict[str, Any], migration_error: bool = False) -> dict[str, Any]:
    result = _merge(DEFAULT_SETTINGS, json.loads(json.dumps(settings)))
    credential_available = False
    for profile_name in ("text_profile", "image_profile"):
        profile = dict(result.get(profile_name) or {})
        key_value = str(profile.get("api_key") or "")
        if not key_value or key_value == "***":
            try:
                key_value = load_secret(str(profile.get("credential_ref") or ""))
            except Exception as exc:
                _log_credential_failure(exc, f"load {profile_name} credential")
                migration_error = True
                key_value = ""
        profile["api_key"] = key_value
        profile["has_api_key"] = bool(key_value or profile.get("has_api_key"))
        credential_available = credential_available or bool(key_value)
        result[profile_name] = profile
    result["credential_migration_error"] = bool(migration_error)
    result["credential_available"] = credential_available
    return result

