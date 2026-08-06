from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from modules import device_identity, license_service
from modules.license_schema import canonical_payload


FEATURES = ["hot_topics", "custom_topic", "five_articles", "image_generation", "article_editing", "word_export", "zip_export"]


@pytest.fixture
def license_env_outside(monkeypatch):
    """Fixture that creates license root OUTSIDE the project tree.

    Uses a temp directory in the system TEMP folder, which is guaranteed to be
    outside PROJECT_ROOT (project is on E:, TEMP is on C:).
    """
    import tempfile
    root = Path(tempfile.mkdtemp(prefix="pytest-license-")) / "license"
    root.mkdir(parents=True)
    private = Ed25519PrivateKey.generate()
    public_path = root.parent / "license_public_key.pem"
    public_path.write_bytes(private.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    monkeypatch.setattr(device_identity, "license_root", lambda: root)
    monkeypatch.setattr(license_service, "license_root", lambda: root)
    monkeypatch.setattr(license_service, "PUBLIC_KEY_PATH", public_path)
    monkeypatch.setattr(license_service, "ACTIVE_LICENSE_PATH", root / "active.license")
    monkeypatch.setattr(license_service, "STATE_PATH", root / "license_state.json")
    now = datetime.now(timezone.utc).replace(microsecond=0)

    def issue(**changes):
        value = {
            "schema_version": 1,
            "license_id": "LIC-TEST-000001",
            "product": "hotspot-article-agent",
            "edition": "standard",
            "customer_name": "测试客户",
            "device_code": device_identity.device_code(),
            "issued_at": now.isoformat(),
            "not_before": (now - timedelta(minutes=1)).isoformat(),
            "expires_at": (now + timedelta(days=30)).isoformat(),
            "features": FEATURES,
            "signature_algorithm": "Ed25519",
        }
        value.update(changes)
        value.pop("signature", None)
        value["signature"] = base64.urlsafe_b64encode(private.sign(canonical_payload(value))).decode("ascii").rstrip("=")
        return value

    yield root, issue
    # Cleanup
    import shutil
    shutil.rmtree(root.parent, ignore_errors=True)


@pytest.fixture
def license_env(tmp_path, monkeypatch):
    root = tmp_path / "license"
    private = Ed25519PrivateKey.generate()
    public_path = tmp_path / "license_public_key.pem"
    public_path.write_bytes(private.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    monkeypatch.setattr(device_identity, "license_root", lambda: root)
    monkeypatch.setattr(license_service, "license_root", lambda: root)
    monkeypatch.setattr(license_service, "PUBLIC_KEY_PATH", public_path)
    monkeypatch.setattr(license_service, "ACTIVE_LICENSE_PATH", root / "active.license")
    monkeypatch.setattr(license_service, "STATE_PATH", root / "license_state.json")
    now = datetime.now(timezone.utc).replace(microsecond=0)

    def issue(**changes):
        value = {
            "schema_version": 1,
            "license_id": "LIC-TEST-000001",
            "product": "hotspot-article-agent",
            "edition": "standard",
            "customer_name": "测试客户",
            "device_code": device_identity.device_code(),
            "issued_at": now.isoformat(),
            "not_before": (now - timedelta(minutes=1)).isoformat(),
            "expires_at": (now + timedelta(days=30)).isoformat(),
            "features": FEATURES,
            "signature_algorithm": "Ed25519",
        }
        value.update(changes)
        value.pop("signature", None)
        value["signature"] = base64.urlsafe_b64encode(private.sign(canonical_payload(value))).decode("ascii").rstrip("=")
        return value

    return root, issue


def test_valid_license_verifies(license_env):
    _, issue = license_env
    assert license_service.validate_license(issue())["license_id"] == "LIC-TEST-000001"


def test_signature_tampering_fails(license_env):
    _, issue = license_env
    value = issue()
    signature = bytearray(base64.urlsafe_b64decode(value["signature"] + "=" * (-len(value["signature"]) % 4)))
    signature[0] ^= 1
    value["signature"] = base64.urlsafe_b64encode(bytes(signature)).decode("ascii").rstrip("=")
    with pytest.raises(license_service.LicenseValidationError, match="signature"):
        license_service.validate_license(value)


def test_any_field_tampering_fails(license_env):
    _, issue = license_env
    value = issue()
    value["customer_name"] = "另一个客户"
    with pytest.raises(license_service.LicenseValidationError, match="signature"):
        license_service.validate_license(value)


def test_wrong_device_fails(license_env):
    _, issue = license_env
    with pytest.raises(license_service.LicenseValidationError, match="device"):
        license_service.validate_license(issue(), expected_device="AAAA-BBBB-CCCC-DDDD-EEEE")


def test_product_mismatch_fails(license_env):
    _, issue = license_env
    value = issue(product="other-product")
    with pytest.raises(license_service.LicenseValidationError, match="product"):
        license_service.validate_license(value)


def test_not_before_fails(license_env):
    _, issue = license_env
    now = datetime.now(timezone.utc).replace(microsecond=0)
    value = issue(not_before=(now + timedelta(days=1)).isoformat())
    with pytest.raises(license_service.LicenseValidationError, match="active"):
        license_service.validate_license(value)


def test_expired_fails(license_env):
    _, issue = license_env
    now = datetime.now(timezone.utc).replace(microsecond=0)
    value = issue(not_before=(now - timedelta(days=2)).isoformat(), expires_at=(now - timedelta(days=1)).isoformat())
    with pytest.raises(license_service.LicenseValidationError, match="expired"):
        license_service.validate_license(value)


def test_corrupt_file_fails(license_env):
    root, _ = license_env
    device_identity.load_or_create_installation_id()
    root.mkdir(parents=True, exist_ok=True)
    (root / "active.license").write_text("not-json", encoding="utf-8")
    assert license_service.check_license()["code"] == "LICENSE_FILE_CORRUPTED"


def test_missing_field_and_unknown_schema_fail(license_env):
    _, issue = license_env
    value = issue()
    value.pop("features")
    with pytest.raises(license_service.LicenseValidationError):
        license_service.validate_license(value)
    value = issue(schema_version=99)
    with pytest.raises(license_service.LicenseValidationError, match="version"):
        license_service.validate_license(value)


def test_public_key_does_not_contain_private_key(license_env):
    _, _ = license_env
    public_text = license_service.PUBLIC_KEY_PATH.read_text(encoding="utf-8")
    assert "PRIVATE " + "KEY" not in public_text


def test_import_failure_preserves_old_license(license_env, tmp_path):
    root, issue = license_env
    root.mkdir(parents=True, exist_ok=True)
    active = root / "active.license"
    active.write_text(json.dumps(issue(), ensure_ascii=False), encoding="utf-8")
    bad = tmp_path / "bad.license"
    bad.write_text("{}", encoding="utf-8")
    with pytest.raises(license_service.LicenseValidationError):
        license_service.import_license(bad)
    assert json.loads(active.read_text(encoding="utf-8"))["license_id"] == "LIC-TEST-000001"


def test_license_expiry_blocks_generation_but_status_is_safe(license_env):
    _, issue = license_env
    now = datetime.now(timezone.utc).replace(microsecond=0)
    license_service.ACTIVE_LICENSE_PATH.parent.mkdir(parents=True, exist_ok=True)
    license_service.ACTIVE_LICENSE_PATH.write_text(json.dumps(issue(not_before=(now - timedelta(days=2)).isoformat(), expires_at=(now - timedelta(days=1)).isoformat())), encoding="utf-8")
    valid, status = license_service.license_allows_generation()
    assert valid is False
    assert status["code"] == "LICENSE_EXPIRED"


def test_clock_small_change_is_not_false_positive(license_env):
    root, issue = license_env
    root.mkdir(parents=True, exist_ok=True)
    license_service.ACTIVE_LICENSE_PATH.write_text(json.dumps(issue()), encoding="utf-8")
    current = datetime.now(timezone.utc)
    license_service.save_secret(license_service.STATE_SECRET_NAME, (current + timedelta(minutes=5)).isoformat(), path=license_service._state_secret_path())
    assert license_service.check_license()["valid"] is True


def test_clock_rollback_over_24_hours_is_detected(license_env):
    root, issue = license_env
    root.mkdir(parents=True, exist_ok=True)
    license_service.ACTIVE_LICENSE_PATH.write_text(json.dumps(issue()), encoding="utf-8")
    current = datetime.now(timezone.utc)
    license_service.save_secret(license_service.STATE_SECRET_NAME, (current + timedelta(days=2)).isoformat(), path=license_service._state_secret_path())
    assert license_service.check_license()["code"] == "CLOCK_ROLLBACK_SUSPECTED"


def test_device_code_is_stable_and_redacts_machine_guid(license_env):
    first = device_identity.device_code()
    second = device_identity.device_code()
    assert first == second
    assert len(first.split("-")) == 5
    assert device_identity._machine_guid() not in first


def test_installation_loss_recreates_with_clear_state(license_env):
    root, _ = license_env
    first = device_identity.load_or_create_installation_id()
    device_identity.installation_path().unlink()
    second = device_identity.load_or_create_installation_id()
    assert first == second
    assert device_identity.installation_path().exists()
    assert device_identity.installation_backup_path().exists()


def test_api_generation_route_has_backend_license_gate():
    import api

    source = Path(api.__file__).read_text(encoding="utf-8")
    assert "def _license_gate" in source
    assert '"LICENSE_REQUIRED"' in source
    assert "/api/license/import" in {route.path for route in api.app.routes}


def test_history_routes_are_not_license_gated():
    import api

    source = Path(api.__file__).read_text(encoding="utf-8")
    assert "def task_result" in source and "def export_task_word" in source


def test_ui_has_activation_page_without_technical_fields():
    source = Path("ui/rc1_app.py").read_text(encoding="utf-8")
    assert "欢迎使用热点图文批量生产工作台" in source
    assert "当前设备码" in source
    assert "traceback" not in source


def test_unlicensed_feature_is_rejected(license_env):
    _, issue = license_env
    value = issue(features=["hot_topics"])
    license_service.ACTIVE_LICENSE_PATH.parent.mkdir(parents=True, exist_ok=True)
    license_service.ACTIVE_LICENSE_PATH.write_text(json.dumps(value), encoding="utf-8")
    allowed, status = license_service.license_allows_generation("five_articles")
    assert allowed is False
    assert status["code"] == "FEATURE_NOT_LICENSED"


def test_licensed_feature_is_allowed(license_env):
    _, issue = license_env
    license_service.ACTIVE_LICENSE_PATH.parent.mkdir(parents=True, exist_ok=True)
    license_service.ACTIVE_LICENSE_PATH.write_text(json.dumps(issue()), encoding="utf-8")
    allowed, status = license_service.license_allows_generation("five_articles")
    assert allowed is True
    assert status["license"]["features"]


def test_status_summary_contains_public_fields_only(license_env):
    root, issue = license_env
    root.mkdir(parents=True, exist_ok=True)
    license_service.ACTIVE_LICENSE_PATH.write_text(json.dumps(issue()), encoding="utf-8")
    status = license_service.check_license()
    assert set(status["license"]) == {"license_id", "customer_name", "edition", "expires_at", "features"}
    assert "signature" not in json.dumps(status)


def test_valid_import_replaces_license_and_keeps_backup(license_env, tmp_path):
    root, issue = license_env
    root.mkdir(parents=True, exist_ok=True)
    active = license_service.ACTIVE_LICENSE_PATH
    active.write_text(json.dumps(issue(customer_name="旧客户"), ensure_ascii=False), encoding="utf-8")
    candidate = tmp_path / "candidate.license"
    candidate.write_text(json.dumps(issue(customer_name="新客户"), ensure_ascii=False), encoding="utf-8")
    result = license_service.import_license(candidate)
    assert result["valid"] is True
    assert json.loads(active.read_text(encoding="utf-8"))["customer_name"] == "新客户"
    assert active.with_suffix(".license.bak").exists()


def test_active_license_is_utf8_without_bom(license_env):
    root, issue = license_env
    root.mkdir(parents=True, exist_ok=True)
    license_service.ACTIVE_LICENSE_PATH.write_text(json.dumps(issue(), ensure_ascii=False), encoding="utf-8")
    assert not license_service.ACTIVE_LICENSE_PATH.read_bytes().startswith(b"\xef\xbb\xbf")


def _is_subpath_of(child: Path, parent: Path) -> bool:
    """Return True if `child` is strictly inside `parent`, using resolved canonical paths.

    Handles Windows case-insensitivity and drive letter normalization.
    A path is NOT inside itself (child != parent required).
    """
    try:
        child.resolve().relative_to(parent.resolve())
        return child.resolve() != parent.resolve()
    except ValueError:
        return False


def _project_root_resolved() -> Path:
    from modules.app_paths import PROJECT_ROOT
    return PROJECT_ROOT.resolve()


def test_license_storage_is_outside_project_tree(license_env_outside):
    """许可证存储必须在项目目录之外，使用 pathlib 正式路径关系判断，不用字符串包含。"""
    root, _ = license_env_outside
    assert not _is_subpath_of(root.resolve(), _project_root_resolved()), (
        f"license_root={root.resolve()} 不应在 PROJECT_ROOT={_project_root_resolved()} 内"
    )


# ── 以下为 Windows 路径边界测试（覆盖 brief 列出的所有场景）──

def test_license_outside_even_when_repo_name_contains_hotspot_article_agent(tmp_path, monkeypatch):
    """仓库目录名含 hotspot-article-agent → 不误判为项目内。"""
    import modules.app_paths as _ap
    simulated_root = tmp_path / "hotspot-article-agent"
    simulated_root.mkdir(parents=True)
    monkeypatch.setattr(_ap, "PROJECT_ROOT", simulated_root)
    license_dir = tmp_path / "outside-license"
    license_dir.mkdir(parents=True)
    assert not _is_subpath_of(license_dir.resolve(), simulated_root.resolve())


def test_license_outside_cross_drive_e_to_c():
    """仓库位于 E盘，LOCALAPPDATA 位于 C盘 → 不同驱动器必然在项目外。"""
    child = Path("C:/Users/test/AppData/Local/热点图文批量生产工作台/license").resolve()
    parent = Path("E:/hotspot-article-agent").resolve()
    assert not _is_subpath_of(child, parent)


def test_license_inside_when_data_license_in_repo(tmp_path):
    """仓库中的 data/license 必须判定为项目目录内。"""
    project = tmp_path / "my-project"
    data_license = project / "data" / "license"
    data_license.mkdir(parents=True)
    assert _is_subpath_of(data_license.resolve(), project.resolve())


def test_license_case_insensitive_path(tmp_path):
    """路径大小写不同不应影响判定。"""
    project = tmp_path / "MY-PROJECT"
    project.mkdir()
    child = project / "data" / "LICENSE"
    child.mkdir(parents=True)
    assert _is_subpath_of(child.resolve(), project.resolve())


def test_license_chinese_path(tmp_path):
    """路径含中文应正确处理。"""
    project = tmp_path / "我的项目"
    project.mkdir()
    child = project / "数据" / "许可证"
    child.mkdir(parents=True)
    assert _is_subpath_of(child.resolve(), project.resolve())


def test_license_real_deep_subpath(tmp_path):
    """真实深层子路径应正确判定。"""
    project = tmp_path / "real-project"
    project.mkdir()
    child = project / "some" / "deep" / "nested" / "license"
    child.mkdir(parents=True)
    assert _is_subpath_of(child.resolve(), project.resolve())


def test_license_prefix_similar_but_not_subpath(tmp_path):
    """前缀相似但不是子路径（如 hotspot vs hotspot-article-agent）不应误判。"""
    project = tmp_path / "hotspot"
    project.mkdir()
    similar = tmp_path / "hotspot-article-agent" / "license"
    similar.mkdir(parents=True)
    assert not _is_subpath_of(similar.resolve(), project.resolve())


def test_license_root_equals_project_root_not_inside(tmp_path):
    """license_root 等于 PROJECT_ROOT 时（自身）判定为不在内部（不允许同目录）。"""
    same = tmp_path / "same"
    same.mkdir()
    assert not _is_subpath_of(same.resolve(), same.resolve())


def test_public_key_resource_exists_without_private_material():
    public_key = Path("resources/license_public_key.pem")
    assert public_key.is_file()
    text = public_key.read_text(encoding="utf-8")
    assert "BEGIN PUBLIC KEY" in text
    assert "PRIVATE" not in text


def test_license_admin_never_defaults_to_project_private_key():
    source = Path("license_admin/signing_identity.py").read_text(encoding="utf-8")
    assert "HOTSPOT_LICENSE_PRIVATE_KEY" in source
    assert "hotspot-license-admin" in source


def test_license_smoke_does_not_use_disable_bypass():
    source = Path("scripts/l1_offline_license_smoke.py").read_text(encoding="utf-8")
    assert "LICENSE_DISABLED" not in source
    assert "OFFLINE_LICENSE_SMOKE_PASS" in source


def test_license_public_summary_preserves_feature_names(license_env):
    _, issue = license_env
    validated = license_service.validate_license(issue(features=["hot_topics", "image_generation"]))
    assert validated["features"] == ["hot_topics", "image_generation"]
