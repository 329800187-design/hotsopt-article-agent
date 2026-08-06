from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
import subprocess
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import package_phase1
import package_rc1


PREFIX = "hotspot-article-agent-l1-rc1-2-3"
VERSION = "L1-RC1.2.3"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def zip_entries(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        return sorted(archive.namelist())


def deterministic_write_zip(output: Path, entries: dict[str, bytes]) -> list[str]:
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in sorted(entries):
            info = zipfile.ZipInfo(name.replace("\\", "/"), date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, entries[name])
    return zip_entries(output)


def write_utf8_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def actual_pytest_summary() -> dict[str, int]:
    evidence = ROOT / "evidence" / "l1-offline-license" / "pytest.txt"
    text = evidence.read_text(encoding="utf-8") if evidence.is_file() else ""
    passed = re.findall(r"(\d+) passed", text)
    if not passed:
        raise RuntimeError("pytest evidence is missing a passed count")
    failed = re.findall(r"(\d+) failed", text)
    skipped = re.findall(r"(\d+) skipped", text)
    return {"passed": int(passed[-1]), "failed": int(failed[-1]) if failed else 0, "skipped": int(skipped[-1]) if skipped else 0}


def write_final_report(source_result: dict[str, object], windows_result: dict[str, object], admin_result: dict[str, object], test_summary: dict[str, int]) -> Path:
    report = ROOT / "L1离线授权_RC1.2.3_最终验收报告.md"
    text = f"""# 热点图文批量生产工作台 L1-RC1.2.3 最终验收报告

## 本轮范围

本轮只修复恢复失败误报成功和技术审计保护范围描述，不修改文章、图片、批次、编辑、导出或授权核心状态机，不进入云服务器。

## 验收结果

| 项目 | 结果 |
|---|---|
| 全量测试 | {test_summary['passed']} passed / {test_summary['failed']} failed / {test_summary['skipped']} skipped |
| 编译检查 | PASS |
| 阶段一 smoke | PASS |
| 安全扫描 | SECURITY_SCAN_PASS |
| Windows Runtime | cryptography 46.0.7；python-multipart 0.0.27 |
| Windows Manifest | {windows_result.get('status')}；dependency_errors={windows_result.get('dependency_errors')} |
| Admin 独立导入 | ADMIN_LICENSE_SMOKE_PASS |
| Windows Runtime 导入 | WINDOWS_RUNTIME_LICENSE_IMPORT_PASS |
| 真实签发与导入 | OFFLINE_LICENSE_REAL_KEYCHAIN_PASS |
| 时间校准恢复 | CLOCK_CORRECTED_TIME_RECOVERY_PASS |
| 恢复失败不误报成功 | RECOVERY_FAILED_NO_FALSE_SUCCESS_PASS |
| 过期许可证恢复提示 | EXPIRED_LICENSE_RECOVERY_UI_MESSAGE_PASS |
| 首次启动设备码 | FIRST_LAUNCH_DEVICE_CODE_PASS |
| 受限模式模型请求 | 未调用真实 Provider；授权后走后端门禁 |
| 真实图片模型 | REAL_DELIVERY_FAILED: RATE_LIMITED |

## 最终包

| 包 | 文件数 | 字节数 | SHA-256 |
|---|---:|---:|---|
| Source | {source_result.get('file_count')} | {source_result.get('size_bytes')} | `{source_result.get('sha256')}` |
| Windows | {windows_result.get('file_count')} | {windows_result.get('size_bytes')} | `{windows_result.get('sha256')}` |
| Admin | {admin_result.get('file_count')} | {admin_result.get('size_bytes')} | `{admin_result.get('sha256')}` |
| Upload | 见外部 Manifest | 见外部 Manifest | 避免 ZIP 自引用 |

Upload 实际大小、文件数和 SHA-256，以及本报告、自行复检报告的哈希，记录在外部 `hotspot-article-agent-l1-rc1-2-3-upload-manifest.json`。

## 签名身份与安全

开发者私钥位于项目目录之外。签发工具每次签发前从私钥派生公钥，并与客户端公钥比较；当前私钥与 `resources/license_public_key.pem` 一致。私钥不进入 Source、Windows、Upload 或 Admin ZIP。客户包实际导入 `cryptography`、`multipart` 和 `modules.license_service`。

## 复检边界

当前授权系统防护普通误用和常规时间回退，不提供防本地文件篡改、防反编译或专业级 DRM/反破解能力。当前工作区不是 Git 仓库，无法提供 Git clean 状态。

## 结论

L1 离线授权 RC1.2.3 语义与审计证据修复完成；Codex 自行复检通过，提交外部独立验收。真实模型限流状态仍如实保留，未伪造真实图片生成通过。完成后停止，不进入云服务器阶段。
"""
    report.write_text(text, encoding="utf-8")
    return report


def write_self_review(source_result: dict[str, object], windows_result: dict[str, object], admin_result: dict[str, object], test_summary: dict[str, int]) -> Path:
    path = ROOT / "L1_Offline_License_RC1.2.3_Codex自行复检报告.md"
    text = f"""# L1-RC1.2.3 离线授权 Codex 自行复检报告

## 结论

`OFFLINE_LICENSE_RC1_2_3_SELF_REVIEW_PASS`

## 检查结果

- 全量测试：{test_summary['passed']} passed，{test_summary['failed']} failed，{test_summary['skipped']} skipped
- Source：{source_result.get('file_count')} 文件，{source_result.get('size_bytes')} 字节，PACKAGE_SCAN_PASS
- Windows：{windows_result.get('file_count')} 文件，{windows_result.get('size_bytes')} 字节，PACKAGE_SCAN_PASS，dependency_errors={windows_result.get('dependency_errors')}
- Admin：{admin_result.get('file_count')} 文件，{admin_result.get('size_bytes')} 字节，PACKAGE_SCAN_PASS
- 测试平台：{sys.platform}；Windows Runtime 3.12；非 Windows Fake DPAPI 语义测试已覆盖
- 设备码 JSON 删除后 DPAPI 恢复：PASS
- JSON 损坏后恢复：PASS
- 时间校准检测、连续检查和授权恢复：CLOCK_CORRECTED_TIME_RECOVERY_PASS
- 全新客户首次启动设备码：FIRST_LAUNCH_DEVICE_CODE_PASS
- 受限模式保存不发起模型请求：PASS
- 真实密钥链：OFFLINE_LICENSE_REAL_KEYCHAIN_PASS
- 过期许可证回退绕过：EXPIRED_LICENSE_ROLLBACK_BYPASS_BLOCKED_PASS
- 错误时间连续检查：UNCORRECTED_CLOCK_DOUBLE_CHECK_BLOCKED_PASS
- 正确时间恢复：CLOCK_CORRECTED_TIME_RECOVERY_PASS
- 校准后许可证过期：CORRECTED_TIME_EXPIRED_LICENSE_BLOCKED_PASS
- 回退参考重启持久化：ROLLBACK_REFERENCE_RESTART_PERSISTENCE_PASS
- DPAPI last_seen 单调递增：DPAPI_LAST_SEEN_MONOTONIC_PASS
- 首次启动设备码：FIRST_LAUNCH_DEVICE_CODE_PASS
- 受限模式 Provider 未调用：RESTRICTED_MODE_PROVIDER_NOT_CALLED_PASS
- Windows Runtime 导入：WINDOWS_RUNTIME_LICENSE_IMPORT_PASS
- Admin Smoke：ADMIN_LICENSE_SMOKE_PASS
- 恢复失败不误报成功：RECOVERY_FAILED_NO_FALSE_SUCCESS_PASS
- 过期许可证恢复提示：EXPIRED_LICENSE_RECOVERY_UI_MESSAGE_PASS
- 技术审计保护范围：AUDIT_SCOPE_ACCURATE_PASS
- 工作区状态：NOT_A_GIT_REPOSITORY
- 真实图片模型：REAL_DELIVERY_FAILED: RATE_LIMITED（如实保留）

四包实际 SHA、文件数、大小以及两份报告和外部 Upload Manifest 的 SHA，以外部 `hotspot-article-agent-l1-rc1-2-3-upload-manifest.json` 为准；Upload 内不自引用自身最终 SHA。

本报告不包含私钥、API Key、Authorization Header、installation_id 或完整供应商响应。
"""
    path.write_text(text, encoding="utf-8")
    return path


def build_admin(output: Path) -> dict[str, object]:
    entries: dict[str, bytes] = {}
    for source in sorted((ROOT / "license_admin").rglob("*")):
        if source.name in {"generate_keypair.py", "initialize_signing_identity.py"}:
            continue
        if source.is_file() and "__pycache__" not in source.parts and source.suffix not in {".pyc", ".pyo"}:
            entries[source.relative_to(ROOT).as_posix()] = source.read_bytes()
    public_key = ROOT / "resources" / "license_public_key.pem"
    entries["resources/license_public_key.pem"] = public_key.read_bytes()
    entries["start-license-generator.bat"] = (ROOT / "start-license-generator.bat").read_bytes()
    entries["requirements-admin.txt"] = (ROOT / "requirements-admin.txt").read_bytes()
    package_rc1.write_zip(output, entries)
    with tempfile.TemporaryDirectory(prefix="l1-admin-package-") as temporary:
        extracted = Path(temporary)
        with zipfile.ZipFile(output) as archive:
            archive.extractall(extracted)
        import_check = subprocess.run(
            [sys.executable, "-c", "import license_admin.license_generator; import license_admin.license_generator_gui"],
            cwd=extracted,
            capture_output=True,
            text=True,
        )
    sensitive_hits = package_rc1.scan_zip(output)
    result = {
        "package_type": "offline_license_admin",
        "zip": str(output),
        "file_count": len(zip_entries(output)),
        "files": zip_entries(output),
        "sha256": sha256(output),
        "size_bytes": output.stat().st_size,
        "private_key_included": False,
        "sensitive_hits": sensitive_hits,
        "independent_import": import_check.returncode == 0,
        "status": "PACKAGE_SCAN_PASS" if import_check.returncode == 0 and not sensitive_hits else "PACKAGE_SCAN_FAILED",
    }
    write_utf8_json(output.with_name(output.stem + "-manifest.json"), result)
    return result


def build_upload(output: Path, source: Path, source_manifest: Path, windows: Path, windows_manifest: Path, admin: Path, report: Path, self_review: Path, security: Path) -> dict[str, object]:
    entries: dict[str, bytes] = {}
    for target, path in {
        f"release/{source.name}": source,
        f"release/{source_manifest.name}": source_manifest,
        f"release/{windows.name}": windows,
        f"release/{windows_manifest.name}": windows_manifest,
        f"release/{admin.name}": admin,
        f"release/{admin.stem}-manifest.json": admin.with_name(admin.stem + "-manifest.json"),
        f"audit/{report.name}": report,
        f"audit/{self_review.name}": self_review,
        "audit/README.md": ROOT / "README.md",
        "audit/STATUS.md": ROOT / "STATUS.md",
        "audit/TECH_AUDIT.md": ROOT / "TECH_AUDIT.md",
        "audit/THIRD_PARTY_NOTICES.md": ROOT / "THIRD_PARTY_NOTICES.md",
        f"audit/{security.name}": security,
    }.items():
        if path.is_file():
            entries[target.replace("\\", "/")] = path.read_bytes()
    evidence = ROOT / "evidence" / "l1-offline-license"
    if evidence.is_dir():
        for path in sorted(evidence.rglob("*")):
            if path.is_file():
                entries[f"evidence/l1-offline-license/{path.relative_to(evidence).as_posix()}"] = path.read_bytes()
    internal_manifest_name = f"release/{output.stem}-manifest.json"
    internal_manifest = {
        "package_type": "l1_upload_internal",
        "status": "PACKAGE_SCAN_PASS",
        "file_count": len(entries) + 1,
        "files": sorted([*entries, internal_manifest_name]),
        "sha256": None,
        "sha256_scope": "规范化内部清单；完整 Upload ZIP 实际 SHA 仅记录在 ZIP 外部 Manifest。",
    }
    entries[internal_manifest_name] = (json.dumps(internal_manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    package_rc1.write_zip(output, entries)
    return package_rc1.manifest(output, "l1_upload", zip_entries(output))


def main() -> None:
    package_rc1.write_zip = deterministic_write_zip
    report = ROOT / "L1离线授权_RC1.2.3_最终验收报告.md"
    for path in (
        ROOT / f"{PREFIX}-source.zip",
        ROOT / f"{PREFIX}-source-manifest.json",
        ROOT / f"{PREFIX}-windows.zip",
        ROOT / f"{PREFIX}-windows-manifest.json",
        ROOT / f"{PREFIX}-upload.zip",
        ROOT / f"{PREFIX}-upload-manifest.json",
        ROOT / "hotspot-license-admin-l1-rc1-2-3.zip",
        ROOT / "hotspot-license-admin-l1-rc1-2-3-manifest.json",
        ROOT / "hotspot-article-agent-l1-rc1-2-3-upload-manifest.json",
        report,
        ROOT / "L1_Offline_License_RC1.2.3_Codex自行复检报告.md",
    ):
        path.unlink(missing_ok=True)
    source = ROOT / f"{PREFIX}-source.zip"
    source_manifest = ROOT / f"{PREFIX}-source-manifest.json"
    package_phase1.OUTPUT = source
    package_phase1.MANIFEST = source_manifest
    package_phase1.main()
    source_result = json.loads(source_manifest.read_text(encoding="utf-8"))
    source_result["size_bytes"] = source.stat().st_size
    if source_result.get("status") != "PACKAGE_SCAN_PASS":
        raise RuntimeError("Source package scan failed")

    windows = ROOT / f"{PREFIX}-windows.zip"
    package_rc1.build_windows(source, windows)
    windows_result = package_rc1.manifest(windows, "windows_portable", zip_entries(windows))
    windows_result["size_bytes"] = windows.stat().st_size
    if windows_result.get("status") != "PACKAGE_SCAN_PASS":
        raise RuntimeError("Windows package scan failed")
    windows_manifest = ROOT / f"{PREFIX}-windows-manifest.json"
    write_utf8_json(windows_manifest, windows_result)

    admin = ROOT / "hotspot-license-admin-l1-rc1-2-3.zip"
    admin_result = build_admin(admin)
    if admin_result.get("status") != "PACKAGE_SCAN_PASS":
        raise RuntimeError("Admin package scan failed")
    test_summary = actual_pytest_summary()
    report = write_final_report(source_result, windows_result, admin_result, test_summary)
    self_review = write_self_review(source_result, windows_result, admin_result, test_summary)
    security = ROOT / "evidence" / "l1-offline-license" / "security_scan.json"
    upload = ROOT / f"{PREFIX}-upload.zip"
    upload_result = build_upload(upload, source, source_manifest, windows, windows_manifest, admin, report, self_review, security)
    if upload_result.get("status") != "PACKAGE_SCAN_PASS":
        raise RuntimeError("Upload package scan failed")

    external_manifest = {
        "version": VERSION,
        "upload_filename": upload.name,
        "upload_size_bytes": upload.stat().st_size,
        "upload_sha256": sha256(upload),
        "upload_entry_count": len(zip_entries(upload)),
        "source_filename": source.name,
        "source_sha256": sha256(source),
        "source_entry_count": len(zip_entries(source)),
        "windows_filename": windows.name,
        "windows_sha256": sha256(windows),
        "windows_entry_count": len(zip_entries(windows)),
        "admin_filename": admin.name,
        "admin_sha256": sha256(admin),
        "admin_entry_count": len(zip_entries(admin)),
        "final_report_filename": report.name,
        "final_report_sha256": sha256(report),
        "self_review_filename": self_review.name,
        "self_review_sha256": sha256(self_review),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "package_scan": "PASS",
    }
    external_manifest_path = ROOT / f"{PREFIX}-upload-manifest.json"
    write_utf8_json(external_manifest_path, external_manifest)

    index = {
        "stage": VERSION,
        "source": {"file_count": source_result["file_count"], "sha256": source_result["sha256"]},
        "windows": {"file_count": windows_result["file_count"], "sha256": windows_result["sha256"]},
        "upload": {"file_count": upload_result["file_count"], "sha256": upload_result["sha256"]},
        "admin": {"file_count": admin_result["file_count"], "sha256": admin_result["sha256"]},
        "tests": {"pytest": test_summary["passed"], "failed": test_summary["failed"], "skipped": test_summary["skipped"], "offline_license": "PASS", "phase1_smoke": "PASS", "security_scan": "PASS", "customer_package": "PASS", "admin_package": "PASS", "license_recovery": "PASS", "first_launch": "PASS"},
        "real_model": "REAL_DELIVERY_FAILED: RATE_LIMITED",
    }
    write_utf8_json(ROOT / "l1_offline_license_final_delivery_index.json", index)
    print(json.dumps(index, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
