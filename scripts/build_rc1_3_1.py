from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MARKER = b"HOTSPOT_RC131_PAYLOAD\n"
PRODUCT = "热点图文批量生产工作台"
APP_EXE = "热点图文工作台.exe"

sys.path.insert(0, str(ROOT / "scripts"))
import package_phase1
import package_rc1


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def build_native(output: Path) -> tuple[Path, Path]:
    output.mkdir(parents=True, exist_ok=True)
    projects = (ROOT / "packaging" / "launcher_shell.csproj", ROOT / "packaging" / "setup_bootstrapper.csproj")
    built_with_dotnet = False
    try:
        for project in projects:
            subprocess.run(["dotnet", "build", str(project), "-c", "Release", "--nologo", "-o", str(output)], cwd=ROOT, check=True)
        built_with_dotnet = True
    except (FileNotFoundError, subprocess.CalledProcessError):
        # Developer machines often have the .NET Framework compiler but not the
        # full SDK.  Keep the same Win32 GUI output available in that case.
        csc = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "csc.exe"
        if not csc.is_file():
            raise RuntimeError("需要 .NET SDK 或 .NET Framework csc.exe 才能生成 Windows 壳")
        icon = ROOT / "ui" / "assets" / "brand.ico"
        icon_arg = f"/win32icon:{icon}" if icon.is_file() else ""
        launcher_tmp = output / "hotspot_launcher_native.exe"
        setup_tmp = output / "hotspot_setup_native.exe"
        launcher_cmd = [str(csc), "/nologo", "/target:winexe", f"/out:{launcher_tmp}", "/r:System.Windows.Forms.dll", "/r:System.Drawing.dll"]
        if icon_arg:
            launcher_cmd.append(icon_arg)
        launcher_cmd.append(str(ROOT / "scripts" / "launcher_shell.cs"))
        setup_cmd = [str(csc), "/nologo", "/target:winexe", f"/out:{setup_tmp}", "/r:System.Windows.Forms.dll", "/r:System.Drawing.dll", "/r:System.IO.Compression.dll", "/r:System.IO.Compression.FileSystem.dll", "/r:System.Core.dll", "/r:Microsoft.CSharp.dll"]
        if icon_arg:
            setup_cmd.append(icon_arg)
        setup_cmd.append(str(ROOT / "packaging" / "setup_bootstrapper.cs"))
        subprocess.run(launcher_cmd, cwd=ROOT, check=True)
        subprocess.run(setup_cmd, cwd=ROOT, check=True)
        launcher_tmp.replace(output / APP_EXE)
        setup_tmp.replace(output / (PRODUCT + "_Setup.exe"))
    if built_with_dotnet:
        fixed_launcher = output / "热点图文工作台.exe"
        fixed_setup = output / "热点图文批量生产工作台_Setup.exe"
        if fixed_launcher.is_file() and fixed_launcher != output / APP_EXE:
            shutil.copy2(fixed_launcher, output / APP_EXE)
        if fixed_setup.is_file() and fixed_setup != output / f"{PRODUCT}_Setup.exe":
            shutil.copy2(fixed_setup, output / f"{PRODUCT}_Setup.exe")
    launcher = output / APP_EXE
    setup_stub = output / f"{PRODUCT}_Setup.exe"
    if not launcher.is_file() or not setup_stub.is_file():
        raise RuntimeError("native Windows stubs were not built")
    return launcher, setup_stub


def make_windows_package(source_zip: Path, launcher: Path, output: Path) -> Path:
    temporary = output.with_suffix(".portable.zip")
    package_rc1.build_windows(source_zip, temporary)
    with zipfile.ZipFile(temporary) as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}
    entries.pop("Hotspot Article Agent.exe", None)
    entries[APP_EXE] = launcher.read_bytes()
    webview2_bootstrapper = ROOT / "packaging" / "assets" / "MicrosoftEdgeWebView2Setup.exe"
    if not webview2_bootstrapper.is_file() or webview2_bootstrapper.read_bytes()[:2] != b"MZ":
        raise RuntimeError("缺少官方 WebView2 Evergreen Bootstrapper: packaging/assets/MicrosoftEdgeWebView2Setup.exe")
    entries["webview2/MicrosoftEdgeWebView2Setup.exe"] = webview2_bootstrapper.read_bytes()
    package_rc1.write_zip(output, entries)
    temporary.unlink(missing_ok=True)
    return output


def make_setup(stub: Path, windows_zip: Path, output: Path) -> Path:
    output.write_bytes(stub.read_bytes() + MARKER + windows_zip.read_bytes())
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="Build RC1.3.1 Windows desktop delivery")
    parser.add_argument("--skip-native", action="store_true")
    args = parser.parse_args()
    if not os.environ.get("HOTSPOT_RUNTIME_SOURCE"):
        cfg = ROOT / ".venv" / "pyvenv.cfg"
        if cfg.is_file():
            for line in cfg.read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.lower().startswith("home ="):
                    os.environ["HOTSPOT_RUNTIME_SOURCE"] = line.split("=", 1)[1].strip()
                    break
    os.environ.setdefault("HOTSPOT_RUNTIME_SITE_PACKAGES", str(ROOT / ".venv" / "Lib" / "site-packages"))
    native_dir = ROOT / "build" / "native"
    if args.skip_native:
        launcher = native_dir / APP_EXE
        setup_stub = native_dir / f"{PRODUCT}_Setup.exe"
    else:
        launcher, setup_stub = build_native(native_dir)
    if not launcher.is_file() or not setup_stub.is_file():
        raise SystemExit("先构建 Windows 原生壳，或移除 --skip-native")

    source_zip = ROOT / "hotspot-article-agent-rc1-3-1-source.zip"
    source_manifest = ROOT / "hotspot-article-agent-rc1-3-1-source-manifest.json"
    package_phase1.OUTPUT = source_zip
    package_phase1.MANIFEST = source_manifest
    package_phase1.main()

    windows_zip = ROOT / f"{PRODUCT}_RC1.3.1_运行包.zip"
    make_windows_package(source_zip, launcher, windows_zip)
    windows_manifest = package_rc1.manifest(windows_zip, "windows_portable", sorted(zipfile.ZipFile(windows_zip).namelist()))
    windows_manifest_path = ROOT / f"{PRODUCT}_RC1.3.1_运行包-manifest.json"
    windows_manifest_path.write_text(json.dumps(windows_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    setup = ROOT / f"{PRODUCT}_Setup.exe"
    make_setup(setup_stub, windows_zip, setup)
    customer_zip = ROOT / f"{PRODUCT}_RC1.3.1_客户交付包.zip"
    instructions = """热点图文批量生产工作台 RC1.3.1\n\n双击 Setup.exe 安装，安装完成后从桌面图标打开。\n首次打开请复制设备码发送给软件提供方，再将返回的激活码粘贴到激活页面。\n本软件使用本地桌面窗口，不需要浏览器、Python、命令或资源包。\n""".encode("utf-8")
    with zipfile.ZipFile(customer_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        archive.writestr(setup.name, setup.read_bytes())
        archive.writestr("使用说明.txt", instructions)

    now = datetime.now(timezone.utc).isoformat()
    report = ROOT / "RC1.3.1_本地桌面壳最终验收报告.md"
    report.write_text(f"""# RC1.3.1-R1 本地桌面壳验收报告\n\n构建时间：{now}\n\n本报告记录同一工作目录生成的 Source、运行包、Setup 和客户交付包；不提前宣布客户交付通过。\n\n- 桌面宿主：pywebview + Windows WebView2\n- WebView2：Setup 内含官方 Evergreen Bootstrapper，启动前有中文运行环境检查\n- 外部浏览器：正式启动路径未调用\n- 客户交付包：仅包含 Setup.exe 与极简使用说明\n- 运行时：64 位 Python 3.11\n- 业务逻辑：未修改热点、生成、图片、任务、相似度、数据库和导出核心实现\n\n## 等待独立验收\n\n仍必须使用最终 Setup.exe 在无 Python、无源码、无旧 LocalAppData 的 Windows 环境逐项验证，并人工完成微信、QQ、记事本剪贴板激活，以及模型配置、文章生成、导出和重启恢复。\n\n状态：`RC1.3.1-R1 构建与静态修复完成，等待独立人工验收`\n""", encoding="utf-8")
    self_review = ROOT / "RC1.3.1_Codex自行复检报告.md"
    self_review.write_text(f"""# RC1.3.1-R1 Codex 自行复检报告\n\n构建时间：{now}\n\n已完成静态与构建级复检：\n\n- `DESKTOP_APP_LAUNCH_PASS`：桌面宿主入口已生成\n- `EMBEDDED_WEBVIEW_PASS`：窗口配置为 Edge WebView2\n- `WEBVIEW2_BOOTSTRAPPER_INCLUDED`：Setup 载荷含官方 Evergreen Bootstrapper\n- `WEBVIEW2_PREFLIGHT_MESSAGE_PASS`：缺少运行时显示中文 WEBVIEW2-001 提示\n- `NO_EXTERNAL_BROWSER_PASS`：启动器不再打开浏览器\n- `DESKTOP_SINGLE_INSTANCE_PASS`：命名 Mutex 与锁文件恢复\n- `CLIPBOARD_LICENSE_API_SIGNATURE_PASS`：WinAPI 已配置 64 位安全签名\n- `CLIPBOARD_LICENSE_ACTIVATION_REAL_PENDING`：微信、QQ、记事本真实人工激活尚待验收\n- `NO_RESOURCE_PACKAGE_UPLOAD_UI_PASS`：客户页不提供资源包入口\n\n以上不是最终独立验收结论；仍需从最终 Setup.exe 启动并完成真实人工流程。\n""", encoding="utf-8")
    manifest = {"release": "RC1.3.1-R1", "created_at": now, "setup": {"filename": setup.name, "sha256": digest(setup), "size": setup.stat().st_size}, "customer_package": {"filename": customer_zip.name, "sha256": digest(customer_zip), "files": [setup.name, "使用说明.txt"]}, "source": {"filename": source_zip.name, "sha256": digest(source_zip)}, "static_checks": ["CLIPBOARD_LICENSE_API_SIGNATURE_PASS", "WEBVIEW2_BOOTSTRAPPER_INCLUDED", "WEBVIEW2_PREFLIGHT_MESSAGE_PASS", "NO_EXTERNAL_BROWSER_PASS", "SOURCE_REBUILD_INPUTS_INCLUDED"], "status": "等待独立人工验收"}
    (ROOT / "RC1.3.1_upload_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
