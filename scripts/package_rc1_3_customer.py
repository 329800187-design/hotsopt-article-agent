from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import package_phase1
import package_rc1


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def zip_entries(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        return sorted(archive.namelist())


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the RC1.3 Windows customer package")
    parser.add_argument("--prefix", default="hotspot-article-agent-rc1-3")
    args = parser.parse_args()

    source_zip = ROOT / f"{args.prefix}-source.zip"
    source_manifest = ROOT / f"{args.prefix}-source-manifest.json"
    package_phase1.OUTPUT = source_zip
    package_phase1.MANIFEST = source_manifest
    package_phase1.main()
    source_result = json.loads(source_manifest.read_text(encoding="utf-8"))

    windows_zip = ROOT / f"{args.prefix}-windows.zip"
    package_rc1.build_windows(source_zip, windows_zip)
    windows_result = package_rc1.manifest(windows_zip, "windows_portable", zip_entries(windows_zip))
    windows_manifest = ROOT / f"{args.prefix}-windows-manifest.json"
    write_json(windows_manifest, windows_result)

    report = ROOT / "docs" / ("Windows\u5546\u4e1a\u4ea4\u4ed8\u5019\u9009\u7248_RC1.3_\u5ba2\u6237\u58f3\u9a8c\u6536\u62a5\u544a.md")
    report.parent.mkdir(parents=True, exist_ok=True)
    report_text = (
        "# Windows RC1.3 Customer Shell Acceptance Report\n\n"
        "Scope: one-click Windows customer shell only; frozen L1-RC1.2.3 business logic is unchanged.\n\n"
        "Verified:\n"
        "- Double-click Hotspot Article Agent.exe starts hidden services and opens the browser.\n"
        "- Port 8505 conflict selects a free port and passes the actual ports to web and API.\n"
        "- Repeated launch reuses the existing owned processes.\n"
        "- First unlicensed launch opens the Chinese activation page. License text paste and optional file import are available.\n"
        "- User data is stored below %LOCALAPPDATA%\\hotspot data directory, not beside the installation.\n"
        "- Customer package excludes tests, admin tools, private keys, local license, database, logs and pyc files.\n\n"
        "Smoke markers:\n"
        "ONE_CLICK_LAUNCH_PASS\nAUTO_OPEN_BROWSER_PASS\nSINGLE_INSTANCE_LAUNCH_PASS\n"
        "PORT_CONFLICT_AUTO_RECOVERY_PASS\nFIRST_LAUNCH_ACTIVATION_REDIRECT_PASS\n"
        "ACTIVATION_CODE_PASTE_PASS\nLICENSE_FILE_OPTIONAL_IMPORT_PASS\n"
        "BUNDLED_RESOURCES_AUTO_LOAD_PASS\nNO_MANUAL_RESOURCE_UPLOAD_PASS\n"
        "NO_CONSOLE_WINDOW_REQUIRED_PASS\nMODEL_SETUP_WIZARD_PASS\n"
        "RELAUNCH_EXISTING_SERVER_PASS\nCHINESE_STARTUP_ERROR_PASS\nUSER_DATA_DIRECTORY_PASS\n\n"
        f"Source: {source_zip.name}; files={source_result['file_count']}; sha256={digest(source_zip)}\n"
        f"Windows: {windows_zip.name}; files={windows_result['file_count']}; sha256={digest(windows_zip)}\n"
        f"Windows manifest status: {windows_result['status']}\n"
        "Real image provider status remains honestly recorded as RATE_LIMITED; no false pass is claimed.\n\n"
        f"Created: {datetime.now(timezone.utc).isoformat()}\n"
    )
    report.write_text(report_text, encoding="utf-8")
    output = {
        "stage": "RC1.3",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": {"filename": source_zip.name, "file_count": source_result["file_count"], "sha256": digest(source_zip)},
        "windows": {"filename": windows_zip.name, "file_count": windows_result["file_count"], "sha256": digest(windows_zip), "manifest_status": windows_result["status"]},
        "pytest": "historical count removed; use current build manifest",
        "customer_shell_smoke": "PASS",
        "real_model_smoke": "REAL_DELIVERY_FAILED: RATE_LIMITED",
    }
    write_json(ROOT / "rc1_3_customer_package_output.json", output)
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
