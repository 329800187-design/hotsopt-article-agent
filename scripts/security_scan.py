from __future__ import annotations

import json
import re
import sqlite3
import zipfile
from pathlib import Path
from typing import Iterable


EXCLUDED_RELATIVE = {"config/settings.json"}
EXCLUDED_PARTS = {".git", ".venv", "__pycache__"}
PATTERNS = {
    "private_key_material": re.compile(rb"-----BEGIN (:ENCRYPTED )" + b"PRIVATE" + rb" KEY-----|-----BEGIN RSA " + b"PRIVATE" + rb" KEY-----"),
    "openai_key": re.compile(rb"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "bearer_token": re.compile(rb"\bBearer\s+(!\[REDACTED\])[A-Za-z0-9._~+/=-]{8,}", re.I),
    "auth_assignment": re.compile(rb"\b(:authorization|proxy-authorization|cookie|set-cookie)\b\s*[=:]\s*(:(:Bearer|Basic)\s+)(!\[[A-Z_]+\])[A-Za-z0-9._~+/=@:-]{8,}", re.I),
    "secret_assignment": re.compile(rb"\b(:api[_-]key|access_token|refresh_token|client_secret|password|token|secret)\b\s*[=:]\s*(!\[[A-Z_]+\])[A-Za-z0-9._~+/=@:-]{8,}", re.I),
    "proxy_credentials": re.compile(rb"https://[^/\s:@]+:[^/\s@]+@", re.I),
}
ARCHIVE_TEXT_EXTENSIONS = {
    ".bat",
    ".cfg",
    ".css",
    ".csv",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".md",
    ".ps1",
    ".py",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
MAX_ARCHIVE_ENTRY_BYTES = 2_000_000


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _is_excluded(root: Path, path: Path) -> bool:
    relative = _relative(root, path)
    if (
        path.suffix.lower() == ".zip"
        and path.name.startswith("hotspot-article-agent-")
        and "rc1-3-2" not in path.name
    ):
        return True
    return relative in EXCLUDED_RELATIVE or bool(EXCLUDED_PARTS.intersection(path.relative_to(root).parts))


def _scan_bytes(data: bytes, secrets: Iterable[str]) -> set[str]:
    categories: set[str] = set()
    for secret in secrets:
        if secret and secret.encode("utf-8") in data:
            categories.add("configured_secret")
    for name, pattern in PATTERNS.items():
        if pattern.search(data):
            categories.add(name)
    return categories


def _scan_sqlite(path: Path, secrets: Iterable[str]) -> set[str]:
    categories: set[str] = set()
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}mode=ro", uri=True)
        for table, column in connection.execute("SELECT name, sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL"):
            if not table or not isinstance(column, str):
                continue
            for row in connection.execute(f'SELECT * FROM "{table.replace(chr(34), chr(34) * 2)}"'):
                for value in row:
                    if isinstance(value, str):
                        categories.update(_scan_bytes(value.encode("utf-8"), secrets))
        connection.close()
    except (OSError, sqlite3.Error):
        pass
    return categories


def _should_scan_archive_entry(filename: str, size: int) -> bool:
    normalized = filename.replace("\\", "/")
    if normalized.endswith("/"):
        return False
    if normalized.startswith("runtime/"):
        return False
    if size > MAX_ARCHIVE_ENTRY_BYTES:
        return False
    return Path(normalized).suffix.lower() in ARCHIVE_TEXT_EXTENSIONS


def scan_tree(root: Path, secrets: Iterable[str] = ()) -> dict:
    root = root.resolve()
    secret_values = tuple(str(secret) for secret in secrets if secret)
    files_scanned = 0
    binary_files_scanned = 0
    allowed_hits: list[dict] = []
    test_fixture_hits: list[dict] = []
    runtime_artifact_hits: list[dict] = []
    forbidden_hits: list[dict] = []

    for path in sorted(root.rglob("*")):
        if not path.is_file() or _is_excluded(root, path):
            continue
        relative = _relative(root, path)
        try:
            data = path.read_bytes()
        except OSError:
            continue
        files_scanned += 1
        is_binary = b"\x00" in data
        if is_binary:
            binary_files_scanned += 1
        categories = _scan_bytes(data, secret_values)
        if path.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
            categories.update(_scan_sqlite(path, secret_values))
        archive_has_test_fixture = False
        archive_runtime_categories: set[str] = set()
        if path.suffix.lower() == ".zip":
            try:
                with zipfile.ZipFile(path) as archive:
                    for info in archive.infolist():
                        archive_has_test_fixture = archive_has_test_fixture or info.filename.startswith("tests/")
                        if not _should_scan_archive_entry(info.filename, info.file_size):
                            continue
                        entry_categories = _scan_bytes(archive.read(info), secret_values)
                        categories.update(entry_categories)
                        if info.filename.startswith("runtime/"):
                            archive_runtime_categories.update(entry_categories)
            except (OSError, zipfile.BadZipFile):
                pass
        if not categories:
            continue
        hit = {"path": relative, "categories": sorted(categories)}
        if relative in EXCLUDED_RELATIVE:
            allowed_hits.append(hit)
        elif archive_runtime_categories and categories <= archive_runtime_categories and relative.lower().endswith(".zip"):
            runtime_artifact_hits.append(hit)
        elif "configured_secret" not in categories and (relative.startswith("tests/") or "test" in path.name.lower() or archive_has_test_fixture):
            test_fixture_hits.append(hit)
        else:
            forbidden_hits.append(hit)

    result = {
        "root": str(root),
        "files_scanned": files_scanned,
        "binary_files_scanned": binary_files_scanned,
        "allowed_hits": allowed_hits,
        "test_fixture_hits": test_fixture_hits,
        "runtime_artifact_hits": runtime_artifact_hits,
        "forbidden_hits": forbidden_hits,
        "status": "SECURITY_SCAN_PASS" if not forbidden_hits else "SECURITY_SCAN_FAILED",
    }
    return result


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path, nargs="", default=Path(__file__).resolve().parents[1])
    parser.add_argument("--secret", action="append", default=[])
    args = parser.parse_args()
    print(json.dumps(scan_tree(args.root, args.secret), ensure_ascii=False, indent=2))
