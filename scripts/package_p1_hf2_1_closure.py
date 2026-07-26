from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELEASE = "RC1.3.3-Lite-P1-HF4.1"
STATUS = "RC1.3.3-Lite-P1-HF4.1 最终构建自检完成，等待用户真实内容、速度与交付复测。"
REPORT_DATE = "2026-07-26"
SETUP = ROOT / "热点图文批量生产工作台_RC1.3.3-Lite-P1-HF4.1_Setup.exe"
CUSTOMER = ROOT / "热点图文批量生产工作台_RC1.3.3-Lite-P1-HF4.1_客户交付包.zip"
SOURCE_ZIP = ROOT / "热点图文批量生产工作台_RC1.3.3-Lite-P1-HF4.1_Source.zip"
SOURCE_MANIFEST = ROOT / "HF4.1_source_manifest.json"
ROOT_MANIFEST = ROOT / "HF4.1_upload_manifest.json"
CUSTOM_REPORT = ROOT / "P1-HF4.1_修复报告.md"
CUSTOM_LOGS_ZIP = ROOT / "P1-HF4.1_测试原始日志.zip"
CUSTOM_MANIFEST = ROOT / "P1-HF4.1_upload_manifest.json"
LOG_DIR = ROOT / "build" / "hf4_1_final_logs"
INNO_LOG = ROOT / "build" / f"{RELEASE}_inno_compile.log"
BUILD_LOG = LOG_DIR / "P1-HF4_build.log"
PYTEST_LOG = LOG_DIR / "P1-HF4_pytest.log"
PY_COMPILE_LOG = ROOT / "build" / "HF4.1_py_compile_report.json"
TEST_RECORD = LOG_DIR / f"{RELEASE}_test_record.json"
INSTALL_CHECK_JSON = LOG_DIR / f"{RELEASE}_install_uninstall_check.json"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def file_entry(path: Path) -> dict[str, object]:
    return {
        "filename": path.name,
        "path": str(path),
        "size": path.stat().st_size,
        "sha256": sha256(path),
    }


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    manifest_data = read_json(ROOT_MANIFEST)
    test_record = read_json(TEST_RECORD) if TEST_RECORD.is_file() else {}
    install_check = manifest_data.get("install_uninstall_check") or {}
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    INSTALL_CHECK_JSON.write_text(json.dumps(install_check, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    with zipfile.ZipFile(CUSTOM_LOGS_ZIP, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in (INNO_LOG, BUILD_LOG, PYTEST_LOG, PY_COMPILE_LOG, TEST_RECORD, INSTALL_CHECK_JSON, ROOT_MANIFEST, SOURCE_MANIFEST):
            if path.is_file():
                archive.write(path, path.relative_to(ROOT).as_posix())

    pytest_summary = str(test_record.get("pytest", {}).get("summary") or "")
    CUSTOM_REPORT.write_text(
        "\n".join(
            [
                "# P1-HF4.1 \u4fee\u590d\u62a5\u544a",
                "",
                f"\u4fee\u590d\u8f6e\u6b21\uff1a`{RELEASE}`",
                f"\u62a5\u544a\u65e5\u671f\uff1a{REPORT_DATE}",
                f"\u5f53\u524d\u72b6\u6001\uff1a`{STATUS}`",
                "",
                "## \u672c\u8f6e\u8303\u56f4",
                "",
                "\u672c\u8f6e\u4ec5\u6536\u53e3 HF4.1 \u6700\u7ec8\u6784\u5efa\u6240\u9700\u7684\u5b89\u88c5\u5305\u3001\u6e90\u7801\u5305\u3001\u4ea4\u4ed8\u5305\u3001\u62a5\u544a\u4e0e\u4e0a\u4f20\u6e05\u5355\u3002",
                "",
                "## \u7ed3\u679c\u6458\u8981",
                "",
                "1. \u5b89\u88c5\u4e0e\u5378\u8f7d\u7edf\u4e00\u57fa\u4e8e Inno Setup \u7684 unins000.exe",
                "2. \u5355\u70ed\u70b9\u591a\u7bc7\u6279\u6b21\u5e76\u53d1\u5143\u6570\u636e\u5df2\u7edf\u4e00\u4e3a\u6700\u591a 3",
                "3. URL \u8f93\u5165\u4e0d\u4f1a\u76f4\u63a5\u4f5c\u4e3a\u8bdd\u9898\u6807\u9898",
                "4. \u6587\u672c\u548c\u56fe\u7247\u7ee7\u7eed\u4f7f\u7528\u540c\u4e00\u4e2a API Key \u6587\u6848",
                "",
                "## \u672c\u5730\u9a8c\u8bc1",
                "",
                f"- \u5b9a\u5411 pytest\uff1a`{pytest_summary}`",
                f"- \u5b89\u88c5\u76ee\u5f55\uff1a`{install_check.get('install_dir', '')}`",
                f"- \u7528\u6237\u6570\u636e\u76ee\u5f55\uff1a`{install_check.get('data_dir', '')}`",
                "",
                "\u672c\u62a5\u544a\u4e0d\u5ba3\u5e03\u5ba2\u6237\u4ea4\u4ed8\u901a\u8fc7",
            ]
        ) + "\n",
        encoding="utf-8",
    )

    artifacts = [file_entry(path) for path in (SETUP, CUSTOMER, SOURCE_ZIP, CUSTOM_REPORT, CUSTOM_LOGS_ZIP) if path.is_file()]
    custom_manifest = {
        "release": RELEASE,
        "report_date": REPORT_DATE,
        "status": STATUS,
        "artifacts": artifacts,
        "install_uninstall_check": install_check,
        "verification": test_record,
        "notes": [
            "\u6700\u7ec8\u5b89\u88c5\u5378\u8f7d\u9a8c\u6536\u4ec5\u57fa\u4e8e Inno Setup \u5378\u8f7d\u5668 unins000.exe",
            "HF4.1 \u4fdd\u7559\u975e JSON \u6b63\u6587\u964d\u7ea7\u4e0e\u672c\u5730\u57fa\u7840\u7a3f\u5bfc\u51fa\u80fd\u529b",
            "\u672c\u8f6e\u4e0d\u5ba3\u5e03\u5ba2\u6237\u4ea4\u4ed8\u901a\u8fc7",
        ],
    }
    CUSTOM_MANIFEST.write_text(json.dumps(custom_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(CUSTOM_REPORT), "logs_zip": str(CUSTOM_LOGS_ZIP), "manifest": str(CUSTOM_MANIFEST)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
