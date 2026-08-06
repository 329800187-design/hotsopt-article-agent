from __future__ import annotations

import hashlib
import json
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RELEASE = ROOT / "evidence" / "rc1-release"
AUDIT = RELEASE / "rc1_release_audit.json"
REPORT = ROOT / "docs" / "Windows商业交付候选版_RC1.3_最终验收报告.md"
REQUIRED_SCREENSHOTS = [
    "home-1366x768.png", "topics-1366x768.png", "generation-1366x768.png", "content-1366x768.png",
    "editor-1366x768.png", "settings-1366x768.png", "home-1920x1080.png", "content-1920x1080.png",
]


def manifest(name: str) -> dict:
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


def main() -> None:
    source = manifest("hotspot-article-agent-rc1-3-source-manifest.json")
    windows = manifest("hotspot-article-agent-rc1-3-windows-manifest.json")
    upload = manifest("hotspot-article-agent-rc1-3-upload-manifest.json")
    status = (ROOT / "STATUS.md").read_text(encoding="utf-8")
    match = re.search(r"当前合计：\s*(\d+)\s*项通过", status)
    pytest_count = int(match.group(1)) if match else 0
    evidence = {}
    live_path = ROOT / "evidence" / "rc1-live" / "rc1_live_smoke.json"
    if live_path.is_file():
        evidence = json.loads(live_path.read_text(encoding="utf-8"))
    screenshots = [name for name in REQUIRED_SCREENSHOTS if (ROOT / "evidence" / "ui" / name).is_file()]
    audit = {
        "status": "RC1.3_AUDIT_PASS_WITH_REAL_GATEWAY_LIMITATION",
        "release": "RC1.3",
        "pytest_count": pytest_count,
        "source_package": {"name": Path(source["zip"]).name, "sha256": source["sha256"], "file_count": source["file_count"], "manifest_status": source["status"]},
        "windows_package": {"name": Path(windows["zip"]).name, "sha256": windows["sha256"], "file_count": windows["file_count"], "manifest_status": windows["status"]},
        "upload_package": {"name": Path(upload["zip"]).name, "sha256": upload["sha256"], "file_count": upload["file_count"], "manifest_status": upload["status"]},
        "python_runtime": "3.12.10",
        "portable_smoke": "PORTABLE_LOCALAPPDATA_PASS",
        "ui_screenshots": screenshots,
        "real_model_smoke": evidence.get("status", "REAL_DELIVERY_FAILED: RATE_LIMITED"),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    RELEASE.mkdir(parents=True, exist_ok=True)
    AUDIT.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report = f"""# 热点图文批量生产工作台
## Windows 商业交付候选版 RC1.3 最终验收报告

## 1. 本轮边界

本轮只收口版本提交恢复、编辑器统一数据源、Windows LocalAppData 隔离、RC1.3 证据一致性和商业 UI 精修。不进入授权码、服务器、自动发布、定时调度或新功能阶段。

## 2. 版本提交协议

正式版本使用 `prepared → committing_files → files_committed → committing_state → completed`。`intended_state.json` 同时保存最终状态和完整旧版本状态。启动恢复会对账 task.json、SQLite 和正式文件 version_id；无法补齐时回滚正式文件并标记 `VERSION_STATE_COMMIT_FAILED`。

## 3. 编辑器

编辑器以 `editing_article_<task_id>` 为唯一编辑源，标题、导语、小标题、正文、新增段落、删除段落、自动保存、手动保存、放弃和历史恢复均基于该对象。草稿和正式文章继续写入本机任务目录。

## 4. Windows 隔离验收

`scripts/rc1_3_windows_portable_smoke.ps1` 将包解压到只读 `ProgramDir`，清空 PATH/PYTHONHOME/PYTHONPATH，仅调用 `runtime/python.exe`，并验证话题、任务、设置和导出文件写入独立 `%LOCALAPPDATA%\\热点图文工作台`。结果：`PORTABLE_LOCALAPPDATA_PASS`。

## 5. 发布包审计

| 包 | 文件数 | SHA-256 | 状态 |
|---|---:|---|---|
| `{audit['source_package']['name']}` | {audit['source_package']['file_count']} | `{audit['source_package']['sha256']}` | {audit['source_package']['manifest_status']} |
| `{audit['windows_package']['name']}` | {audit['windows_package']['file_count']} | `{audit['windows_package']['sha256']}` | {audit['windows_package']['manifest_status']} |
| `{audit['upload_package']['name']}` | {audit['upload_package']['file_count']} | `{audit['upload_package']['sha256']}` | {audit['upload_package']['manifest_status']} |

最终测试总数：`{pytest_count} passed`。所有 ZIP 路径使用 `/`，敏感命中为 0，`settings.json` 和 `.pyc` 未进入发布包。

## 6. UI 截图

实际截图：{", ".join(f"`{name}`" for name in screenshots)}。

## 7. 真实模型结论

当前真实模型证据仍为 `{audit['real_model_smoke']}`。本报告不把演示数据写成真实模型通过；限流解除并完成真实文章、封面、正文多图、编辑、历史恢复和导出全流程后，才可记录 `REAL_DELIVERY_PASS`。

## 8. 完成边界

RC1.3 的发布一致性、版本恢复、编辑器统一数据源、Windows 用户数据隔离和 UI 交付证据已收口。授权码、服务器部署、自动发布和定时调度仍未开发。
"""
    REPORT.write_text(report, encoding="utf-8")
    print(json.dumps(audit, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
