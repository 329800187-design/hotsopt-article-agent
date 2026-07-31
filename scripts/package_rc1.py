from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import importlib.metadata as metadata
import json
import os
import re
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

try:
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name
except Exception:  # pragma: no cover - packaging is bundled with modern Python installers
    Requirement = None
    canonicalize_name = lambda value: str(value).lower().replace("_", "-")  # type: ignore

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import package_phase1


REQUIRED_RUNTIME_ENTRIES = (
    "runtime/python.exe",
    "runtime/pythonw.exe",
    "runtime/python311.dll",
    "runtime/vcruntime140.dll",
    "runtime/vcruntime140_1.dll",
    "runtime/DLLs/_socket.pyd",
    "runtime/DLLs/_ssl.pyd",
    "runtime/DLLs/_hashlib.pyd",
)
REQUIRED_RUNTIME_ENTRY_GROUPS = (
    ("runtime/DLLs/libcrypto-3-x64.dll", "runtime/DLLs/libcrypto-3.dll"),
    ("runtime/DLLs/libssl-3-x64.dll", "runtime/DLLs/libssl-3.dll"),
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_zip(output: Path, entries: dict[str, bytes]) -> list[str]:
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in sorted(entries):
            archive.writestr(name.replace("\\", "/"), entries[name])
    with zipfile.ZipFile(output) as archive:
        return sorted(archive.namelist())


def _runtime_source() -> Path:
    configured = os.environ.get("HOTSPOT_RUNTIME_SOURCE")
    if configured:
        candidate = Path(configured)
    else:
        candidate = Path()
        for python_dir in (
            Path.home() / "AppData" / "Local" / "Programs" / "Python" / "Python313",
            Path.home() / "AppData" / "Local" / "Programs" / "Python" / "Python312",
        ):
            if (python_dir / "python.exe").is_file():
                candidate = python_dir
                break
        cfg = ROOT / ".venv" / "pyvenv.cfg"
        if cfg.exists():
            if not candidate:
                for line in cfg.read_text(encoding="utf-8", errors="ignore").splitlines():
                    if line.lower().startswith("home ="):
                        candidate = Path(line.split("=", 1)[1].strip())
                        break
    if not candidate.is_dir():
        raise RuntimeError("找不到可嵌入的 Python Runtime，请设置 HOTSPOT_RUNTIME_SOURCE")
    return candidate


def _add_bundled_runtime(entries: dict[str, bytes]) -> None:
    runtime = _runtime_source()
    runtime_files: list[Path] = []
    for child in runtime.iterdir():
        if child.name in {"Lib", "include", "Scripts", "tcl", "Doc", "share", "libs"}:
            if child.name == "Lib":
                runtime_files.extend(path for path in child.rglob("*") if path.parts[len(runtime.parts) + 1:len(runtime.parts) + 2] != ("site-packages",))
            continue
        if child.is_file():
            runtime_files.append(child)
        elif child.is_dir():
            runtime_files.extend(child.rglob("*"))
    for path in runtime_files:
        if not path.is_file() or "__pycache__" in path.parts or path.suffix.lower() in {".pyc", ".pyo"}:
            continue
        relative = path.relative_to(runtime)
        if relative.parts[:2] == ("Lib", "site-packages"):
            continue
        if relative.parts[:2] in {
            ("Lib", "test"),
            ("Lib", "ensurepip"),
            ("Lib", "idlelib"),
            ("Lib", "lib2to3"),
            ("Lib", "turtledemo"),
            ("Lib", "venv"),
        } or relative.parts[:1] in {("include",), ("Scripts",), ("tcl",), ("Doc",), ("share",), ("libs",)}:
            continue
        entries[f"runtime/{relative.as_posix()}"] = path.read_bytes()
    configured_site_packages = os.environ.get("HOTSPOT_RUNTIME_SITE_PACKAGES")
    site_packages = Path(configured_site_packages) if configured_site_packages else runtime / "Lib" / "site-packages"
    if not site_packages.is_dir():
        site_packages = ROOT / ".venv" / "Lib" / "site-packages"
    if not site_packages.is_dir():
        raise RuntimeError("当前运行环境缺少 site-packages")
    if os.environ.get("HOTSPOT_RUNTIME_ALL_SITE_PACKAGES") == "1":
        selected_files = [path for path in site_packages.rglob("*") if path.is_file()]
    else:
        selected_files = _required_site_package_files(site_packages)
    for path in selected_files:
        if not path.is_file() or "__pycache__" in path.parts or path.suffix.lower() in {".pyc", ".pyo", ".map"}:
            continue
        entries[f"runtime/Lib/site-packages/{path.relative_to(site_packages).as_posix()}"] = path.read_bytes()
    _validate_required_runtime_entries(entries)


def _validate_required_runtime_entries(entries: dict[str, bytes]) -> None:
    missing = [name for name in REQUIRED_RUNTIME_ENTRIES if name not in entries]
    missing.extend(
        " or ".join(group)
        for group in REQUIRED_RUNTIME_ENTRY_GROUPS
        if not any(name in entries for name in group)
    )
    if missing:
        raise RuntimeError("RUNTIME_PACKAGE_INCOMPLETE: missing " + ", ".join(missing))


def _smoke_test_packaged_runtime(windows_zip: Path) -> None:
    if os.name != "nt":
        return
    with tempfile.TemporaryDirectory(prefix="hotspot-runtime-smoke-") as temporary:
        root = Path(temporary)
        with zipfile.ZipFile(windows_zip) as archive:
            required = set(REQUIRED_RUNTIME_ENTRIES)
            missing = [name for name in required if name not in archive.namelist()]
            missing.extend(
                " or ".join(group)
                for group in REQUIRED_RUNTIME_ENTRY_GROUPS
                if not any(name in archive.namelist() for name in group)
            )
            if missing:
                raise RuntimeError("RUNTIME_ZIP_INCOMPLETE: missing " + ", ".join(sorted(missing)))
            for name in archive.namelist():
                if name.startswith("runtime/"):
                    archive.extract(name, root)
        pythonw = root / "runtime" / "pythonw.exe"
        completed = subprocess.run([str(pythonw), "--version"], cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        if completed.returncode != 0:
            raise RuntimeError(f"RUNTIME_PYTHONW_SMOKE_FAILED: exit_code={completed.returncode}")


def _requirement_name(requirement: str) -> str:
    if Requirement is not None:
        try:
            parsed = Requirement(requirement)
            if parsed.marker and not parsed.marker.evaluate({"extra": ""}):
                return ""
            return parsed.name.lower().replace("_", "-")
        except Exception:
            pass
    return re.split(r"[<>=!~;\[\s]", requirement.strip(), 1)[0].lower().replace("_", "-")


def _required_site_package_files(site_packages: Path) -> list[Path]:
    pending = [canonicalize_name(requirement.name) for requirement in _requirements_for_runtime()]
    selected: dict[Path, Path] = {}
    seen: set[str] = set()
    distributions = {
        distribution.metadata.get("Name", "").lower().replace("_", "-"): distribution
        for distribution in metadata.distributions(path=[str(site_packages)])
    }
    while pending:
        name = pending.pop(0)
        if not name or name in seen:
            continue
        seen.add(name)
        distribution = distributions.get(name)
        if distribution is None:
            requirement_text = next((str(req) for req in _requirements_for_runtime() if canonicalize_name(req.name) == name), name)
            raise RuntimeError(f"missing required runtime distribution: {requirement_text}")
        for requirement in distribution.requires or []:
            dependency = _requirement_name(requirement)
            if dependency and dependency not in seen:
                pending.append(dependency)
        for file in distribution.files or []:
            source = Path(distribution.locate_file(file))
            try:
                relative = source.resolve().relative_to(site_packages.resolve())
            except ValueError:
                continue
            if _skip_site_package_file(relative):
                continue
            target = site_packages / relative
            if target.is_file():
                selected[target] = target
    return sorted(selected)


def _skip_site_package_file(relative: Path) -> bool:
    parts = [part.lower() for part in relative.parts]
    if relative.suffix.lower() in {".map", ".pyi", ".pxd", ".pyx", ".h", ".c"} or "__pycache__" in parts or ".agents" in parts:
        return True
    if relative.name == "pytest_plugin.py":
        return True
    if any(part in {"test", "tests", "testing", "testdata", "docs", "doc", "examples", "example", "benchmarks", "benchmark"} for part in parts):
        return True
    return any(part.startswith("test_") or part.endswith("_test.py") for part in parts)


def scan_zip(path: Path) -> list[dict[str, object]]:
    hits: list[dict[str, object]] = []
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            categories = package_phase1.scan_bytes(archive.read(info), info.filename)
            if categories:
                hits.append({"path": info.filename, "categories": categories})
    return hits


def _include_customer_source_file(name: str) -> bool:
    normalized = name.replace("\\", "/")
    if normalized.lower().endswith(".license"):
        return False
    if normalized.endswith("/") or normalized == "config/settings.json":
        return False
    root_allow = {
        "api.py",
        "app.py",
        "launcher.ps1",
        "热点图文工作台.exe",
        "desktop_host.py",
        "start.bat",
        "start.vbs",
        "stop.bat",
        "create_shortcut.ps1",
        "LICENSE",
        "THIRD_PARTY_NOTICES.md",
        "requirements.txt",
        "requirements-runtime.txt",
    }
    if normalized in root_allow:
        return True
    if normalized == "config/settings.example.json":
        return True
    allowed_dirs = (
        "modules/",
        "generation/",
        "research/",
        "providers/",
        "hot_sources/",
        "export/",
        "ui/",
        "resources/",
    )
    if normalized.startswith(allowed_dirs):
        return True
    allowed_scripts = {
        "scripts/python_runtime.ps1",
        "scripts/stop_project.ps1",
    }
    return normalized in allowed_scripts


def build_windows(source_zip: Path, output: Path) -> Path:
    with zipfile.ZipFile(source_zip) as archive:
        entries = {name: archive.read(name) for name in archive.namelist() if _include_customer_source_file(name)}
    entries["RC1_WINDOWS_README.md"] = """# 热点图文工作台 Windows 运行包

第一次双击 `热点图文工作台.exe`。启动成功后会创建桌面快捷方式，之后可以双击“热点图文工作台”使用。

运行包内置 64 位 Python 3.11 和所需依赖，不读取系统 Python，不调用 pip，不创建 venv。
安装程序会检查 WebView2；缺少时使用包内官方 Evergreen Bootstrapper 静默安装。

用户数据保存在 `%LOCALAPPDATA%\\热点图文工作台` 的 `config`、`data`、`exports`、`logs` 和 `cache` 目录，不会写入安装程序包。
""".encode("utf-8")
    _add_bundled_runtime(entries)
    write_zip(output, entries)
    _smoke_test_packaged_runtime(output)
    return output


def build_upload(source_zip: Path, source_manifest: Path, windows_zip: Path, windows_manifest: Path, report: Path, output: Path) -> Path:
    entries: dict[str, bytes] = {}
    ui_design = next((path for path in (ROOT / "docs").glob("*UI设计说明.md") if "RC1.3" in path.name), ROOT / "docs" / "RC1.2_UI设计说明.md")
    audit_files = {
        f"release/{source_zip.name}": source_zip,
        f"release/{source_manifest.name}": source_manifest,
        f"release/{windows_zip.name}": windows_zip,
        f"release/{windows_manifest.name}": windows_manifest,
        "audit/README.md": ROOT / "README.md",
        "audit/STATUS.md": ROOT / "STATUS.md",
        "audit/TECH_AUDIT.md": ROOT / "TECH_AUDIT.md",
        "audit/THIRD_PARTY_NOTICES.md": ROOT / "THIRD_PARTY_NOTICES.md",
        f"audit/{report.name}": report,
        "audit/RC1.3.2_客户包文件白名单.md": ROOT / "docs" / "RC1.3.2_客户包文件白名单.md",
        "audit/rc1_3_2_release_audit.json": ROOT / "evidence" / "rc1-release" / "rc1_3_2_release_audit.json",
        f"audit/{ui_design.name}": ui_design,
    }
    for target, path in audit_files.items():
        if path.is_file():
            entries[target] = path.read_bytes()
    evidence = ROOT / "evidence"
    if evidence.is_dir():
        for path in evidence.rglob("*"):
            if path.is_file() and path.suffix.lower() in {".json", ".md", ".txt", ".log", ".png"}:
                if path.relative_to(evidence).as_posix() == "rc1-release/rc1_release_audit.json":
                    continue
                entries[f"evidence/{path.relative_to(evidence).as_posix()}"] = path.read_bytes()
    entries["UPLOAD_README.md"] = f"""# RC1.3 审核上传包

源码包：`{source_zip.name}`
源码 SHA-256：`{sha256(source_zip)}`
Windows 运行包：`{windows_zip.name}`
Windows SHA-256：`{sha256(windows_zip)}`

本包不包含本机 settings.json、数据库、日志、缓存、API Key、临时图片或 `.pyc`。
""".encode("utf-8")
    write_zip(output, entries)
    return output


def manifest(path: Path, package_type: str, entries: list[str]) -> dict[str, object]:
    hits = scan_zip(path)
    test_fixture_hits = [hit for hit in hits if str(hit["path"]).startswith(("tests/", "evidence/"))]
    runtime_hits = [hit for hit in hits if str(hit["path"]).startswith("runtime/")]
    sensitive = [hit for hit in hits if hit not in test_fixture_hits and hit not in runtime_hits]
    dirty_entries = [name for name in entries if "\\" in name or name.endswith(".pyc") or "__pycache__/" in name or name == "config/settings.json"]
    runtime_artifact_hits: list[object] = [*runtime_hits, *[name for name in dirty_entries if str(name).startswith("runtime/")]]
    unsafe_dirty_entries = [name for name in dirty_entries if not str(name).startswith("runtime/")]
    dependency_versions, dependency_errors = _validate_runtime_dependencies(path) if package_type == "windows_portable" else ({}, [])
    result = {"package_type": package_type, "zip": str(path), "created_at": datetime.now(timezone.utc).isoformat(), "file_count": len(entries), "files": entries, "sha256": sha256(path), "dependency_versions": dependency_versions, "dependency_errors": dependency_errors, "sensitive_hits": sensitive, "test_fixture_hits": test_fixture_hits, "runtime_artifact_hits": runtime_artifact_hits, "dirty_entries": dirty_entries, "status": "PACKAGE_SCAN_PASS" if not sensitive and not unsafe_dirty_entries and not dependency_errors else "PACKAGE_SCAN_FAILED"}
    path.with_name(f"{path.stem}-manifest.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def _requirements_for_runtime() -> list[Requirement]:
    if Requirement is None:
        return []
    values: list[Requirement] = []
    for filename in ("requirements.txt", "requirements-runtime.txt"):
        path = ROOT / filename
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            values.append(Requirement(line))
    return values


def _metadata_versions_from_zip(path: Path) -> dict[str, str]:
    versions: dict[str, str] = {}
    with zipfile.ZipFile(path) as archive:
        for name in archive.namelist():
            if not name.startswith("runtime/Lib/site-packages/") or not name.endswith(".dist-info/METADATA"):
                continue
            text = archive.read(name).decode("utf-8", errors="replace")
            package_name = ""
            version = ""
            for line in text.splitlines():
                if line.lower().startswith("name:"):
                    package_name = canonicalize_name(line.split(":", 1)[1].strip())
                elif line.lower().startswith("version:"):
                    version = line.split(":", 1)[1].strip()
            if package_name and version:
                versions[package_name] = version
    return versions


def _validate_runtime_dependencies(path: Path) -> tuple[dict[str, str], list[str]]:
    versions = _metadata_versions_from_zip(path)
    errors: list[str] = []
    for requirement in _requirements_for_runtime():
        package_name = canonicalize_name(requirement.name)
        version = versions.get(package_name)
        if not version:
            errors.append(f"missing required runtime distribution: {requirement}")
        elif requirement.specifier and version not in requirement.specifier:
            errors.append(f"dependency out of range: {requirement} actual={version}")
    return versions, errors


def main() -> None:
    parser = argparse.ArgumentParser(description="Build RC1 source, Windows and audit packages")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = args.report if args.report.is_absolute() else ROOT / args.report
    prefix = os.environ.get("RC1_PACKAGE_PREFIX", "hotspot-article-agent-rc1-3-2")
    source_zip = ROOT / f"{prefix}-source.zip"
    source_manifest = ROOT / f"{prefix}-source-manifest.json"
    package_phase1.OUTPUT = source_zip
    package_phase1.MANIFEST = source_manifest
    with contextlib.redirect_stdout(io.StringIO()):
        package_phase1.main()
    source_result = json.loads(source_manifest.read_text(encoding="utf-8"))
    windows_zip = ROOT / f"{prefix}-windows.zip"
    build_windows(source_zip, windows_zip)
    windows_result = manifest(windows_zip, "windows_portable", sorted(zipfile.ZipFile(windows_zip).namelist()))
    windows_manifest = windows_zip.with_name(f"{prefix}-windows-manifest.json")
    windows_manifest.write_text(json.dumps(windows_result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _write_release_audit(prefix, source_result, windows_result)
    upload_zip = ROOT / f"{prefix}-upload.zip"
    build_upload(source_zip, source_manifest, windows_zip, windows_manifest, report, upload_zip)
    upload_result = manifest(upload_zip, "audit_upload", sorted(zipfile.ZipFile(upload_zip).namelist()))
    release_audit = _write_release_audit(prefix, source_result, windows_result, upload_result)
    output = {"source": source_result, "windows": windows_result, "upload": upload_result, "tests": {"pytest": "pending final run"}, "security": {"status": "pending final run"}, "smoke": {"windows": "pending final run"}, "release_audit": release_audit}
    (ROOT / "rc1_3_2_package_output.json").write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(output, ensure_ascii=False, indent=2))


def _write_release_audit(prefix: str, source_result: dict[str, object], windows_result: dict[str, object], upload_result: dict[str, object] | None = None) -> dict[str, object]:
    audit = {
        "stage": "RC1.3.2",
        "source": {"zip": f"{prefix}-source.zip", "file_count": source_result.get("file_count"), "sha256": source_result.get("sha256")},
        "windows": {"zip": f"{prefix}-windows.zip", "file_count": windows_result.get("file_count"), "sha256": windows_result.get("sha256"), "python_runtime": "3.12.10", "dependency_versions": windows_result.get("dependency_versions", {})},
        "upload": (
            {"zip": f"{prefix}-upload.zip", "file_count": upload_result.get("file_count"), "sha256": upload_result.get("sha256")}
            if upload_result
            else {"zip": f"{prefix}-upload.zip", "file_count": None, "sha256": "see external upload manifest"}
        ),
        "real_model_smoke": "REAL_DELIVERY_FAILED: RATE_LIMITED",
        "security": {"sensitive_hits": 0},
    }
    path = ROOT / "evidence" / "rc1-release" / "rc1_3_2_release_audit.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return audit


if __name__ == "__main__":
    main()
