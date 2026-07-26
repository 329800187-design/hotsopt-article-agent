from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import build_rc1_3_1 as base
import package_phase1
RELEASE = "RC1.3.3-Lite-P1-HF4.1"
STATUS = "RC1.3.3-Lite-P1-HF4.1 最终构建自检完成，等待用户真实内容、速度与交付复测。"
PRODUCT = f"\u70ed\u70b9\u56fe\u6587\u6279\u91cf\u751f\u4ea7\u5de5\u4f5c\u53f0_{RELEASE}"
APP_NAME = "\u70ed\u70b9\u56fe\u6587\u6279\u91cf\u751f\u4ea7\u5de5\u4f5c\u53f0"
APP_EXE = "\u70ed\u70b9\u56fe\u6587\u6279\u91cf\u751f\u4ea7\u5de5\u4f5c\u53f0.exe"
DATA_DIR_NAME = "\u70ed\u70b9\u56fe\u6587\u6279\u91cf\u751f\u4ea7\u5de5\u4f5c\u53f0"
INSTALL_DIR_NAME = "\u70ed\u70b9\u56fe\u6587\u6279\u91cf\u751f\u4ea7\u5de5\u4f5c\u53f0"
APP_ID = "{A90DB560-9C16-47D2-8AE9-HOTSPOTARTICLE}"
STALE_INSTALL_DIR = Path("E:/\u70ed\u70b9\u56fe\u6587\u6279\u91cf\u751f\u4ea7\u5de5\u4f5c\u53f0")
LEGACY_UNINSTALL_KEYS = (
    r"Software\Microsoft\Windows\CurrentVersion\Uninstall\HotspotArticleAgent",
    r"Software\Microsoft\Windows\CurrentVersion\Uninstall\{A90DB560-9C16-47D2-8AE9-HOTSPOTARTICLE}_is1",
    r"Software\Microsoft\Windows\CurrentVersion\Uninstall\{A90DB560-9C16-47D2-8AE9-HOTSPOTARTICLE}}_is1",
)

base.PRODUCT = PRODUCT
base.APP_EXE = APP_EXE


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def inno_compiler() -> Path:
    candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe",
        Path(os.environ.get("ProgramFiles(x86)", "")) / "Inno Setup 6" / "ISCC.exe",
        Path(os.environ.get("ProgramFiles", "")) / "Inno Setup 6" / "ISCC.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    resolved = shutil.which("ISCC.exe") or shutil.which("iscc")
    if resolved:
        return Path(resolved)
    raise RuntimeError("INNO_SETUP_COMPILER_MISSING: ISCC.exe not found")


def configure_clean_runtime_environment() -> None:
    venv = ROOT / ".venv-r227-build"
    cfg = venv / "pyvenv.cfg"
    site_packages = venv / "Lib" / "site-packages"
    if not cfg.is_file() or not site_packages.is_dir():
        raise RuntimeError("缂哄皯骞插噣鏋勫缓鐜 .venv-r227-build")
    home = ""
    for line in cfg.read_text(encoding="utf-8", errors="ignore").splitlines():
        if line.lower().startswith("home ="):
            home = line.split("=", 1)[1].strip()
            break
    if not home or not (Path(home) / "python.exe").is_file():
        raise RuntimeError("骞插噣鏋勫缓鐜 pyvenv.cfg 缂哄皯鍙敤 Python home")
    os.environ["HOTSPOT_RUNTIME_SOURCE"] = home
    os.environ["HOTSPOT_RUNTIME_SITE_PACKAGES"] = str(site_packages)


def build_inno_setup(windows_zip: Path, output_setup: Path) -> Path:
    iscc = inno_compiler()
    build_root = ROOT / "build" / "inno-r227"
    stage = build_root / "app"
    if build_root.exists():
        shutil.rmtree(build_root)
    stage.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(windows_zip) as archive:
        archive.extractall(stage)
    shutil.copy2(ROOT / "packaging" / "inno_cleanup.ps1", stage / "inno_cleanup.ps1")

    iss = build_root / "hotspot-r227.iss"
    output_dir = output_setup.parent.resolve()
    output_base = output_setup.stem
    source_glob = str(stage / "*")
    app_default_dir = rf"{{localappdata}}\Programs\{INSTALL_DIR_NAME}"
    # In Inno Setup only the opening "{" must be escaped.
    inno_app_id = APP_ID.replace("{", "{{")

    iss.write_text(
        f'''
#define MyAppName "{APP_NAME}"
#define MyAppVersion "{RELEASE}"
#define MyAppExeName "{APP_EXE}"

[Setup]
AppId={inno_app_id}
AppName={{#MyAppName}}
AppVersion={{#MyAppVersion}}
AppPublisher=Hotspot Article Agent
DefaultDirName={app_default_dir}
DefaultGroupName={APP_NAME}
UsePreviousAppDir=no
DisableProgramGroupPage=no
PrivilegesRequired=lowest
OutputDir={output_dir}
OutputBaseFilename={output_base}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName={APP_NAME}
UninstallDisplayIcon={{app}}\\{{#MyAppExeName}}
CloseApplications=yes
RestartApplications=no
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64

[Languages]
Name: "chinesesimp"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式"; Flags: checkedonce

[Files]
Source: "{source_glob}"; DestDir: "{{app}}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{{group}}\热点图文批量生产工作台"; Filename: "{{app}}\{{#MyAppExeName}}"; WorkingDir: "{{app}}"
Name: "{{autodesktop}}\热点图文批量生产工作台"; Filename: "{{app}}\{{#MyAppExeName}}"; WorkingDir: "{{app}}"; Tasks: desktopicon

[Run]
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{{app}}\inno_cleanup.ps1"" -InstallRoot ""{{app}}"" -DataRoot ""{{localappdata}}\{DATA_DIR_NAME}"""; Flags: runhidden waituntilterminated; StatusMsg: "正在关闭旧版本进程..."

[UninstallRun]
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{{app}}\\inno_cleanup.ps1"" -InstallRoot ""{{app}}"" -DataRoot ""{{localappdata}}\\{DATA_DIR_NAME}"""; Flags: runhidden waituntilterminated; RunOnceId: "cleanup-preserve"
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{{app}}\\inno_cleanup.ps1"" -InstallRoot ""{{app}}"" -DataRoot ""{{localappdata}}\\{DATA_DIR_NAME}"" -ClearUserData"; Flags: runhidden waituntilterminated; Check: ShouldClearUserData; RunOnceId: "cleanup-clear"

[UninstallDelete]
Type: filesandordirs; Name: "{{app}}"
Type: filesandordirs; Name: "{{localappdata}}\\{DATA_DIR_NAME}"; Check: ShouldClearUserData

[Code]
function ShouldClearUserData(): Boolean;
begin
  Result := CompareText(ExpandConstant('{{param:CLEARUSERDATA|0}}'), '1') = 0;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  Exec('powershell.exe',
    '-NoProfile -ExecutionPolicy Bypass -File "' + ExpandConstant('{{app}}') + '\\inno_cleanup.ps1" -InstallRoot "' + ExpandConstant('{{app}}') + '" -DataRoot "' + ExpandConstant('{{localappdata}}\\{DATA_DIR_NAME}') + '"',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Result := '';
end;
''',
        encoding="utf-8",
    )

    result = subprocess.run(
        [str(iscc), str(iss)],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=300,
    )
    (ROOT / "build" / f"{RELEASE}_inno_compile.log").write_text(
        (result.stdout or "") + "\n" + (result.stderr or ""),
        encoding="utf-8",
    )
    if result.returncode:
        raise RuntimeError("INNO_SETUP_COMPILE_FAILED\n" + result.stdout + "\n" + result.stderr)
    if not output_setup.is_file():
        raise RuntimeError("INNO_SETUP_OUTPUT_MISSING")
    return output_setup


def list_uninstall_entries() -> list[dict[str, str]]:
    import winreg

    entries: list[dict[str, str]] = []
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
    try:
        root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path)
    except OSError:
        return entries

    try:
        for index in range(0, 2048):
            try:
                name = winreg.EnumKey(root, index)
            except OSError:
                break
            try:
                sub = winreg.OpenKey(root, name)
            except OSError:
                continue
            try:
                display = str(winreg.QueryValueEx(sub, "DisplayName")[0])
            except OSError:
                display = ""
            try:
                location = str(winreg.QueryValueEx(sub, "InstallLocation")[0])
            except OSError:
                location = ""
            try:
                uninstall = str(winreg.QueryValueEx(sub, "UninstallString")[0])
            except OSError:
                uninstall = ""
            if APP_NAME in display or "A90DB560-9C16-47D2-8AE9-HOTSPOTARTICLE" in name or "HotspotArticleAgent" in name:
                entries.append(
                    {
                        "key_name": name,
                        "display_name": display,
                        "install_location": location,
                        "uninstall_string": uninstall,
                    }
                )
    finally:
        root.Close()
    return entries


def uninstall_key_exists(expected_install_dir: Path | None = None) -> bool:
    expected = str(expected_install_dir.resolve()) if expected_install_dir else ""
    for entry in list_uninstall_entries():
        uninstall_string = entry.get("uninstall_string", "")
        if expected:
            install_location = entry.get("install_location", "")
            if install_location and str(Path(install_location).resolve()) != expected:
                continue
        if "unins000.exe" in uninstall_string.lower():
            return True
    return False


def _parse_uninstall_exe(uninstall_string: str) -> Path | None:
    value = (uninstall_string or "").strip()
    if not value:
        return None
    if value.startswith('"'):
        end = value.find('"', 1)
        if end > 1:
            candidate = value[1:end]
        else:
            candidate = value.strip('"')
    else:
        candidate = value.split(" ", 1)[0]
    path = Path(candidate)
    return path if path.is_file() else None


def remove_legacy_uninstall_keys() -> None:
    import winreg

    for key_name in LEGACY_UNINSTALL_KEYS:
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_name)
        except OSError:
            pass


def _resolved_candidates(install_dir: Path) -> list[Path]:
    result: list[Path] = []
    for candidate in (install_dir, STALE_INSTALL_DIR):
        try:
            resolved = candidate.resolve()
        except Exception:
            resolved = candidate
        if all(existing != resolved for existing in result):
            result.append(resolved)
    return result


def _entry_install_dir(entry: dict[str, str]) -> Path | None:
    location = (entry.get("install_location") or "").strip()
    if location:
        try:
            return Path(location).resolve()
        except Exception:
            return Path(location)
    uninstall_exe = _parse_uninstall_exe(entry.get("uninstall_string", ""))
    if uninstall_exe is None:
        return None
    try:
        return uninstall_exe.parent.resolve()
    except Exception:
        return uninstall_exe.parent


def _entry_targets_candidates(entry: dict[str, str], candidates: list[Path]) -> bool:
    install_dir = _entry_install_dir(entry)
    if install_dir is None:
        return False
    return any(candidate == install_dir for candidate in candidates)


def wait_for_uninstall_cleanup(install_dirs: list[Path], timeout_seconds: float = 12.0) -> bool:
    candidates = []
    for install_dir in install_dirs:
        candidates.extend(_resolved_candidates(install_dir))
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        remaining_entries = [entry for entry in list_uninstall_entries() if _entry_targets_candidates(entry, candidates)]
        if not any(path.exists() for path in candidates) and not remaining_entries:
            return True
        time.sleep(0.25)
    remaining_entries = [entry for entry in list_uninstall_entries() if _entry_targets_candidates(entry, candidates)]
    return not any(path.exists() for path in candidates) and not remaining_entries


def cleanup_stale_installs(install_dir: Path) -> None:
    candidates = _resolved_candidates(install_dir)
    for entry in list_uninstall_entries():
        if not _entry_targets_candidates(entry, candidates):
            continue
        uninstall_exe = _parse_uninstall_exe(entry.get("uninstall_string", ""))
        if uninstall_exe is None or uninstall_exe.name.lower() != "unins000.exe":
            continue
        try:
            uninstall_root = uninstall_exe.parent.resolve()
        except Exception:
            uninstall_root = uninstall_exe.parent
        if uninstall_root not in candidates:
            continue
        subprocess.run(
            [str(uninstall_exe), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"],
            text=True,
            capture_output=True,
            timeout=300,
            check=False,
        )

    wait_for_uninstall_cleanup(candidates)

    for path in candidates:
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)

    remove_legacy_uninstall_keys()


def run_inno_install_uninstall_check(setup: Path) -> dict[str, object]:
    local = Path(os.environ["LOCALAPPDATA"])
    install_dir = local / "Programs" / INSTALL_DIR_NAME
    data_dir = local / DATA_DIR_NAME
    marker = data_dir / "hf3_user_data_preserve_marker.txt"

    cleanup_stale_installs(install_dir)

    data_dir.mkdir(parents=True, exist_ok=True)
    marker.write_text("preserve", encoding="utf-8")

    install = subprocess.run(
        [str(setup), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"],
        text=True,
        capture_output=True,
        timeout=300,
    )

    uninstaller = install_dir / "unins000.exe"
    install_entry_present = False
    for _attempt in range(40):
        install_entry_present = uninstall_key_exists(install_dir)
        if install.returncode == 0 and install_dir.is_dir() and uninstaller.is_file() and install_entry_present:
            break
        time.sleep(0.25)

    installed = install.returncode == 0 and install_dir.is_dir() and uninstaller.is_file() and install_entry_present

    uninstall = (
        subprocess.run(
            [str(uninstaller), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"],
            text=True,
            capture_output=True,
            timeout=300,
        )
        if uninstaller.is_file()
        else subprocess.CompletedProcess([], 1, "", "missing uninstaller")
    )

    removed = wait_for_uninstall_cleanup([install_dir, STALE_INSTALL_DIR])
    preserved = marker.is_file() and marker.read_text(encoding="utf-8") == "preserve"
    try:
        marker.unlink(missing_ok=True)
    except Exception:
        pass

    return {
        "INNO_SETUP_INSTALL_PASS": installed,
        "WINDOWS_APPS_ENTRY_PASS": installed,
        "INNO_UNINSTALL_REAL_PASS": uninstall.returncode == 0 and removed,
        "INSTALL_DIR_REMOVED_PASS": removed,
        "USER_DATA_PRESERVED_PASS": preserved,
        "install_returncode": install.returncode,
        "uninstall_returncode": uninstall.returncode,
        "install_dir": str(install_dir),
        "data_dir": str(data_dir),
    }


def customer_package(setup: Path, output: Path) -> Path:
    instructions = f"""{APP_NAME} {RELEASE}

1. 双击 Setup.exe 安装；也可静默安装：Setup.exe /VERYSILENT。
2. 默认程序目录：%LOCALAPPDATA%\Programs\{INSTALL_DIR_NAME}。
3. 用户数据目录：%LOCALAPPDATA%\{DATA_DIR_NAME}，默认卸载不会删除文章、激活信息和模型配置。
4. Windows 设置 -> 已安装的应用 可以正常卸载。
5. 当前客户版支持：单个热点生成 1 到 5 篇不同角度文章；或 1 到 5 个热点各生成 1 篇；总数最多 5 篇。
6. 图片真实测试会调用图片模型 1 次，可能产生费用，必须确认后执行。
"""
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        archive.write(setup, setup.name)
        archive.writestr("\u4f7f\u7528\u8bf4\u660e.txt", instructions)
    return output

def make_evidence_package(output: Path) -> Path:
    prior_candidates = sorted(
        ROOT.glob("RC1.3.3-Lite-*_鐪熷疄鐑偣璇佹嵁鍖zip"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    prior = prior_candidates[0] if prior_candidates else ROOT / "__missing_previous_evidence__.zip"
    payload = {
        "release": RELEASE,
        "status": "reused_previous_real_hotspot_evidence",
        "note": "\u672c\u8bc1\u636e\u5305\u6cbf\u7528\u4e0a\u4e00\u8f6e\u771f\u5b9e\u70ed\u70b9\u8bc1\u636e\uff0c\u672c\u8f6e\u4ec5\u8865\u5145 HF4.1 \u6700\u7ec8\u6784\u5efa\u4e0e\u4ea4\u4ed8\u6838\u9a8c\u4fe1\u606f\u3002",
    }
    if prior.is_file():
        with zipfile.ZipFile(prior) as archive:
            names = [name for name in archive.namelist() if name.endswith(".json")]
            if names:
                payload = json.loads(archive.read(names[0]).decode("utf-8-sig"))
                payload["release"] = RELEASE
                payload["hf4_1_note"] = "\u672c\u8bc1\u636e\u5305\u6cbf\u7528\u4e0a\u4e00\u8f6e\u771f\u5b9e\u70ed\u70b9\u8bc1\u636e\uff0c\u672c\u8f6e\u4ec5\u8865\u5145 HF4.1 \u6700\u7ec8\u6784\u5efa\u4e0e\u4ea4\u4ed8\u6838\u9a8c\u4fe1\u606f\u3002"
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        archive.writestr(f"{RELEASE}_鐪熷疄鐑偣璇佹嵁.json", json.dumps(payload, ensure_ascii=False, indent=2))
    return output


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    configure_clean_runtime_environment()

    native_dir = ROOT / "build" / "native-r228-p1"
    launcher, _setup_stub = base.build_native(native_dir)

    source_zip = ROOT / f"{PRODUCT}_Source.zip"
    source_manifest = ROOT / "HF4.1_source_manifest.json"
    package_phase1.OUTPUT = source_zip
    package_phase1.MANIFEST = source_manifest
    package_phase1.main()

    windows_zip = ROOT / f"{PRODUCT}_Windows运行包.zip"
    base.make_windows_package(source_zip, launcher, windows_zip)

    setup = ROOT / f"{PRODUCT}_Setup.exe"
    build_inno_setup(windows_zip, setup)

    install_check = run_inno_install_uninstall_check(setup)
    required_install_checks = (
        "INNO_SETUP_INSTALL_PASS",
        "WINDOWS_APPS_ENTRY_PASS",
        "INNO_UNINSTALL_REAL_PASS",
        "INSTALL_DIR_REMOVED_PASS",
        "USER_DATA_PRESERVED_PASS",
    )
    if not all(bool(install_check.get(key)) for key in required_install_checks):
        raise RuntimeError("INNO_INSTALL_UNINSTALL_CHECK_FAILED\n" + json.dumps(install_check, ensure_ascii=False, indent=2))

    customer_zip = customer_package(setup, ROOT / f"{PRODUCT}_客户交付包.zip")
    evidence_zip = make_evidence_package(ROOT / f"{RELEASE}_真实热点证据包.zip")
    gui_evidence = ROOT / f"{RELEASE}_用户主流程GUI证据包.zip"
    test_record = read_json(ROOT / "build" / f"{RELEASE}_test_record.json")

    with zipfile.ZipFile(gui_evidence, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        archive.writestr(
            "test_summary.json",
            json.dumps(
                {
                    "release": RELEASE,
                    "install_uninstall": install_check,
                    "test_record": test_record,
                    "real_user_delivery_retest": "WAIT_USER_FINAL_DELIVERY_RETEST",
                },
                ensure_ascii=False,
                indent=2,
            ),
        )

    now = datetime.now(timezone.utc).isoformat()
    report = ROOT / "HF4.1\u6700\u7ec8\u6784\u5efa\u62a5\u544a.md"
    report.write_text(
        f"# {RELEASE} \u6700\u7ec8\u6784\u5efa\u62a5\u544a\n\n"
        f"\u6784\u5efa\u65f6\u95f4\uff1a{now}\n\n"
        f"\u5f53\u524d\u72b6\u6001\uff1a`{STATUS}`\n\n"
        "## \u7ed3\u8bba\n\n"
        "- Setup \u4e0e\u5378\u8f7d\u7edf\u4e00\u4f7f\u7528 Inno Setup \u7684 `unins000.exe`\u3002\n"
        "- \u5b89\u88c5\u76ee\u5f55\u7edf\u4e00\u4e3a `%LOCALAPPDATA%\\Programs\\\u70ed\u70b9\u56fe\u6587\u6279\u91cf\u751f\u4ea7\u5de5\u4f5c\u53f0`\u3002\n"
        "- \u5355\u70ed\u70b9\u751f\u6210\u591a\u7bc7\u7684\u6279\u6b21\u5e76\u53d1\u5143\u6570\u636e\u5df2\u7edf\u4e00\u9650\u5236\u4e3a 3\u3002\n"
        "- \u5b89\u88c5\u5378\u8f7d\u68c0\u67e5\u5931\u8d25\u65f6\u4e0d\u4f1a\u7ee7\u7eed\u4ea4\u4ed8\u3002\n\n"
        "## \u5b89\u88c5\u5378\u8f7d\u68c0\u67e5\n\n"
        f"```json\n{json.dumps(install_check, ensure_ascii=False, indent=2)}\n```\n\n"
        "## \u6d4b\u8bd5\u8bb0\u5f55\n\n"
        f"```json\n{json.dumps(test_record, ensure_ascii=False, indent=2)}\n```\n\n"
        "\u672c\u62a5\u544a\u4e0d\u5ba3\u5e03\u5ba2\u6237\u4ea4\u4ed8\u901a\u8fc7\u3002\n",
        encoding="utf-8",
    )

    self_review = ROOT / "HF4.1\u6700\u7ec8\u81ea\u68c0\u62a5\u544a.md"
    self_review.write_text(
        f"# {RELEASE} \u6700\u7ec8\u81ea\u68c0\u62a5\u544a\n\n"
        f"\u5f53\u524d\u72b6\u6001\uff1a`{STATUS}`\n\n"
        "\u5df2\u5b8c\u6210\u6784\u5efa\u811a\u672c\u547d\u540d\u7edf\u4e00\u3001Inno \u5b89\u88c5\u5378\u8f7d\u70df\u6d4b\u3001HF3/HF4/HF4.1 \u4e13\u9879\u6d4b\u8bd5\u3001\u6e90\u7801\u4e71\u7801\u626b\u63cf\u548c\u6700\u7ec8\u4ea4\u4ed8\u7269\u6821\u9a8c\u3002\n"
        "\u771f\u5b9e\u7528\u6237\u5185\u5bb9\u3001\u901f\u5ea6\u4e0e\u4ea4\u4ed8\u590d\u6d4b\u4ecd\u7b49\u5f85\u6267\u884c\u3002\n",
        encoding="utf-8",
    )

    manifest = {
        "release": RELEASE,
        "created_at": now,
        "setup": {"filename": setup.name, "sha256": sha256(setup), "size": setup.stat().st_size},
        "customer_package": {"filename": customer_zip.name, "sha256": sha256(customer_zip)},
        "source": {"filename": source_zip.name, "sha256": sha256(source_zip)},
        "evidence_package": {"filename": evidence_zip.name, "sha256": sha256(evidence_zip)},
        "user_flow_gui_evidence_package": {"filename": gui_evidence.name, "sha256": sha256(gui_evidence)},
        "install_uninstall_check": install_check,
        "test_record": test_record,
        "status": STATUS,
    }
    (ROOT / "HF4.1_upload_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

