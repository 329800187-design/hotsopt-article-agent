from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from generation.versioning import VersionCommitError, commit_candidate, recover_version_commits


def _write(path: Path, value: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _version_fixture(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    root = tmp_path / "task"
    candidate = tmp_path / "candidate"
    old_hashes = {
        "article.json": _write(root / "article.json", "old article"),
        "article.md": _write(root / "article.md", "old markdown"),
        "images/assets.json": _write(root / "images/assets.json", "old assets"),
        "images/section-1.png": _write(root / "images/section-1.png", "old image 1"),
        "images/section-2.png": _write(root / "images/section-2.png", "old image 2"),
    }
    _write(candidate / "article.json", "new article")
    _write(candidate / "article.md", "new markdown")
    _write(candidate / "images/assets.json", "new assets")
    _write(candidate / "images/section-1.png", "new image 1")
    _write(candidate / "images/section-3.png", "new image 3")
    return root, candidate, old_hashes


def test_commit_manifest_contains_delete_and_replaces_old_images(tmp_path):
    root, candidate, old_hashes = _version_fixture(tmp_path)
    record = commit_candidate(
        root,
        candidate,
        files=["article.json", "article.md", "images/assets.json", "images/section-1.png", "images/section-3.png"],
        files_to_delete=["images/section-2.png"],
    )
    assert record["files_to_replace"]
    assert "images/section-3.png" in record["files_to_create"]
    assert record["files_to_delete"] == ["images/section-2.png"]
    assert not (root / "images/section-2.png").exists()
    assert (root / "images/section-3.png").read_text(encoding="utf-8") == "new image 3"


def test_delete_permission_error_rolls_back_every_old_file(tmp_path, monkeypatch):
    root, candidate, old_hashes = _version_fixture(tmp_path)
    original_unlink = Path.unlink

    def fail_delete(path: Path, *args, **kwargs):
        if path == root / "images/section-2.png":
            raise PermissionError("locked")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_delete)
    with pytest.raises(VersionCommitError):
        commit_candidate(
            root,
            candidate,
            files=["article.json", "article.md", "images/assets.json", "images/section-1.png", "images/section-3.png"],
            files_to_delete=["images/section-2.png"],
        )
    for relative, expected in old_hashes.items():
        assert hashlib.sha256((root / relative).read_bytes()).hexdigest() == expected
    assert not (root / "images/section-3.png").exists()


def test_delete_interruption_after_one_file_rolls_back_and_recovery_is_idempotent(tmp_path, monkeypatch):
    root, candidate, old_hashes = _version_fixture(tmp_path)
    original_unlink = Path.unlink
    deleted = {"count": 0}

    def fail_after_first(path: Path, *args, **kwargs):
        if path.parent == root / "images" and path.name.startswith("section-"):
            deleted["count"] += 1
            if deleted["count"] == 2:
                raise PermissionError("interrupted")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_after_first)
    with pytest.raises(VersionCommitError):
        commit_candidate(root, candidate, files=["article.json"], files_to_delete=["images/section-1.png", "images/section-2.png"])
    for relative, expected in old_hashes.items():
        assert hashlib.sha256((root / relative).read_bytes()).hexdigest() == expected
    assert recover_version_commits(root) == []
    assert recover_version_commits(root) == []


def test_commit_record_is_json_auditable(tmp_path):
    root, candidate, _ = _version_fixture(tmp_path)
    record = commit_candidate(root, candidate, files=["article.json"], files_to_delete=["images/section-1.png", "images/section-2.png"])
    commit_files = list((root / ".attempts").glob("commit-*/commit.json"))
    persisted = json.loads(commit_files[0].read_text(encoding="utf-8"))
    assert persisted["status"] == "committed"
    assert persisted["files_to_delete"] == record["files_to_delete"]
