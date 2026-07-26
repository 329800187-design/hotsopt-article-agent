from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def add_file(archive: zipfile.ZipFile, source: Path, target: str) -> None:
    archive.write(source, target.replace("\\", "/"))


def build_bundle(release_zip: Path, output: Path, report: Path | None = None, stage: str = "phase2a", evidence_dir: Path | None = None) -> Path:
    release_manifest = release_zip.with_name(
        f"{release_zip.stem}-manifest.json"
    )
    report = report or ROOT / "docs" / "阶段二_2A.3_状态机与真实网关最终验收报告.md"
    required = {
        "release/" + release_zip.name: release_zip,
        "release/" + release_manifest.name: release_manifest,
        "audit/STATUS.md": ROOT / "STATUS.md",
        "audit/README.md": ROOT / "README.md",
        "audit/TECH_AUDIT.md": ROOT / "TECH_AUDIT.md",
        "audit/THIRD_PARTY_NOTICES.md": ROOT / "THIRD_PARTY_NOTICES.md",
        "audit/" + report.name: report,
    }
    if evidence_dir:
        evidence_prefix = "evidence/" + evidence_dir.name
        for evidence in sorted(evidence_dir.rglob("*")):
            if evidence.is_file():
                required[evidence_prefix + "/" + evidence.relative_to(evidence_dir).as_posix()] = evidence
    missing = [str(path) for path in required.values() if not path.is_file()]
    if missing:
        raise FileNotFoundError("缺少交付文件:\n" + "\n".join(missing))

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="delivery_bundle_") as temp_dir:
        staging = Path(temp_dir)
        upload_readme = staging / "UPLOAD_README.md"
        release_hash = sha256(release_zip)
        upload_readme.write_text(
            "# 阶段二 2A.3 上传包\n\n"
            "请优先上传 `release/` 下的最终 ZIP。`audit/` 保存本次审核需要的状态、技术审计和验收报告。\n\n"
            f"最终发布 ZIP：`{release_zip.name}`\n"
            f"SHA-256：`{release_hash}`\n\n"
            "本包不包含本机配置、数据库、日志、缓存或 API Key。\n",
            encoding="utf-8",
        )
        upload_text = upload_readme.read_text(encoding="utf-8")
        upload_readme.write_text(upload_text.replace("2A.3", stage), encoding="utf-8")
        upload_readme.write_text(
            f"# {stage} upload bundle\n\n"
            "Upload the release ZIP under `release/`. Audit status, technical review, "
            "acceptance report, and live evidence are included separately.\n\n"
            f"Release ZIP: `{release_zip.name}`\n"
            f"SHA-256: `{release_hash}`\n\n"
            "This bundle excludes local settings, database files, logs, cache files, and API keys.\n",
            encoding="utf-8",
        )
        with zipfile.ZipFile(
            output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
        ) as archive:
            add_file(archive, upload_readme, "UPLOAD_README.md")
            for target, source in required.items():
                add_file(archive, source, target)

    return output


def main() -> None:
    parser = argparse.ArgumentParser(description="生成单一审核上传包")
    parser.add_argument("--release-zip", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--stage", default="phase2a")
    parser.add_argument("--evidence-dir", type=Path)
    args = parser.parse_args()

    release_zip = args.release_zip if args.release_zip.is_absolute() else ROOT / args.release_zip
    output = args.output or ROOT / f"{release_zip.stem}-upload.zip"
    if not output.is_absolute():
        output = ROOT / output
    report = args.report
    if report and not report.is_absolute():
        report = ROOT / report
    evidence_dir = args.evidence_dir
    if evidence_dir and not evidence_dir.is_absolute():
        evidence_dir = ROOT / evidence_dir
    bundle = build_bundle(release_zip, output, report=report, stage=args.stage, evidence_dir=evidence_dir)
    manifest = bundle.with_name(f"{bundle.stem}-manifest.json")
    with zipfile.ZipFile(bundle) as archive:
        names = sorted(info.filename for info in archive.infolist())
    manifest.write_text(
        json.dumps(
            {
                "bundle": str(bundle),
                "created_at": datetime.now(timezone.utc).isoformat(),
                "file_count": len(names),
                "files": names,
                "sha256": sha256(bundle),
                "release_zip": str(release_zip),
                "release_zip_sha256": sha256(release_zip),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"bundle={bundle}")
    print(f"manifest={manifest}")
    print(f"files={len(names)}")
    print(f"sha256={sha256(bundle)}")


if __name__ == "__main__":
    main()
