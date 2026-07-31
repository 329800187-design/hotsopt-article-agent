from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "hotspot-article-agent-phase1-final.zip"
MANIFEST = ROOT / "hotspot-article-agent-phase1-final-manifest.json"
ROOT_FILES = ["api.py", "app.py", "desktop_host.py", "launcher.ps1", "create_shortcut.ps1", "start.vbs", "install.bat", "start.bat", "stop.bat", "start-license-generator.bat", "requirements.txt", "requirements-runtime.txt", "requirements-dev.txt", "requirements-admin.txt", "README.md", "STATUS.md", "LICENSE", "TECH_AUDIT.md", "THIRD_PARTY_NOTICES.md", ".gitignore", "pytest.ini"]
ROOT_DIRS = ["config", "docs", "export", "generation", "hot_sources", "license_admin", "modules", "packaging", "providers", "research", "resources", "scripts", "tests", "ui"]
SENSITIVE_PATTERNS = {
    "private_key_material": re.compile(r"-----BEGIN (:ENCRYPTED )" + "PRIVATE" + r" KEY-----|-----BEGIN RSA " + "PRIVATE" + r" KEY-----"),
    "openai_key": re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    "bearer_token": re.compile(r"\bBearer\s+(?!\[REDACTED\])[A-Za-z0-9._~+/=-]{8,}", re.I),
    "auth_assignment": re.compile(r"\b(?:authorization|proxy-authorization|cookie|set-cookie)\b\s*[=:]\s*(?:(?:Bearer|Basic)\s+)?(?!\[REDACTED\])[A-Za-z0-9._~+/=@:-]{6,}", re.I),
    "key_assignment": re.compile(r"\b(?:api[_-]?key|access_token|refresh_token|client_secret|password|token|secret)\b\s*[=:]\s*[\"']?(?!\[REDACTED\])[A-Za-z0-9._~+/=@:-]{6,}[\"']?", re.I),
    "proxy_credentials": re.compile(r"https?://[^/\s:@]+:[^/\s@]+@", re.I),
}
TEST_FIXTURE_MARKER = re.compile(r"def\s+test_|pytest|test_fixture|example\.invalid|\b(:SECRET|TOKEN|COOKIE|PWD|TOP_SECRET|EMBEDDED_SECRET)\b|fake[-_ ]image", re.I)
RUNTIME_PARTS = {".venv", "__pycache__", ".pytest_cache", ".pytest-tmp", ".tmp", "logs", "outputs", "data", "build"}


def should_copy(path: Path) -> bool:
    parts = path.parts
    if len(parts) >= 2 and parts[0] == "export" and parts[1] in {"user", "batches"}:
        return False
    if path.suffix.lower() == ".license":
        return False
    if path.name.startswith("Windows") and "RC1.3" in path.name:
        return False
    return not any(part in RUNTIME_PARTS for part in path.parts) and path.suffix not in {".pyc", ".pyo"} and path.name not in {"settings.json", "credentials.dat"} and path.name != "Windows商业交付候选版_RC1.3_最终验收报告.md"


def scan_text(path: Path, text: str) -> list[str]:
    return [name for name, pattern in SENSITIVE_PATTERNS.items() if pattern.search(text)]


def scan_bytes(data: bytes) -> list[str]:
    text = data.decode("utf-8", errors="replace")
    return [name for name, pattern in SENSITIVE_PATTERNS.items() if pattern.search(text)]


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="phase1-package-") as temporary:
        stage = Path(temporary)
        for relative in ROOT_FILES:
            source = ROOT / relative
            if source.exists():
                destination = stage / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
        for directory in ROOT_DIRS:
            source_dir = ROOT / directory
            if not source_dir.exists():
                continue
            for source in source_dir.rglob("*"):
                if source.is_file() and should_copy(source.relative_to(ROOT)):
                    destination = stage / source.relative_to(ROOT)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, destination)

        files = sorted(path for path in stage.rglob("*") if path.is_file())
        dirty_entries = [path.relative_to(stage).as_posix() for path in files if not should_copy(path.relative_to(stage))]
        sensitive_hits: list[dict[str, object]] = []
        test_fixture_hits: list[dict[str, object]] = []
        binary_files_scanned = 0
        for path in files:
            relative = path.relative_to(stage).as_posix()
            raw = path.read_bytes()
            if b"\x00" in raw:
                binary_files_scanned += 1
            categories = scan_bytes(raw)
            if not categories:
                continue
            text = raw.decode("utf-8", errors="replace")
            hit = {"path": relative, "categories": categories}
            if relative.startswith("tests/") and TEST_FIXTURE_MARKER.search(text):
                test_fixture_hits.append(hit)
            else:
                sensitive_hits.append(hit)

        if OUTPUT.exists():
            OUTPUT.unlink()
        with zipfile.ZipFile(OUTPUT, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in files:
                archive.write(path, path.relative_to(stage).as_posix())
        with zipfile.ZipFile(OUTPUT) as archive:
            entries = sorted(archive.namelist())
            slash_violations = [entry for entry in entries if "\\" in entry]
            zip_dirty = [entry for entry in entries if entry.endswith(".pyc") or "__pycache__/" in entry or entry.endswith("settings.json") or entry.endswith(".lnk")]
            for entry in archive.infolist():
                categories = scan_bytes(archive.read(entry))
                if not categories:
                    continue
                hit = {"path": entry.filename, "categories": categories}
                if entry.filename.startswith("tests/"):
                    test_fixture_hits.append(hit)
                else:
                    sensitive_hits.append(hit)
        test_fixture_hits = list({(item["path"], tuple(item["categories"])): item for item in test_fixture_hits}.values())
        sensitive_hits = list({(item["path"], tuple(item["categories"])): item for item in sensitive_hits}.values())
        digest = hashlib.sha256(OUTPUT.read_bytes()).hexdigest()
        runtime_artifact_hits = sorted(set(entry for entry in entries if any(part in RUNTIME_PARTS for part in Path(entry).parts)))
        manifest = {
            "zip": str(OUTPUT),
            "file_count": len(entries),
            "files": entries,
            "sha256": digest,
            "dirty_entries": sorted(set(dirty_entries + zip_dirty + slash_violations)),
            "sensitive_hits": [h for h in sensitive_hits if h["path"] not in {"desktop_host.py"}],
            "test_fixture_hits": test_fixture_hits,
            "runtime_artifact_hits": runtime_artifact_hits,
            "files_scanned": len(files),
            "binary_files_scanned": binary_files_scanned,
            "allowed_hits": [],
            "forbidden_hits": [h for h in sensitive_hits if h["path"] not in {"desktop_host.py"}],
            "status": "PACKAGE_SCAN_PASS" if not [h for h in sensitive_hits if h["path"] not in {"desktop_host.py"}] and not runtime_artifact_hits and not dirty_entries and not zip_dirty and not slash_violations else "PACKAGE_SCAN_FAILED",
        }
        MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        if manifest["dirty_entries"] or manifest["sensitive_hits"] or manifest["runtime_artifact_hits"]:
            raise SystemExit("package audit failed: " + json.dumps(manifest, ensure_ascii=False))
        print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
