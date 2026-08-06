from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "evidence" / "phase2b2-live"


def main() -> int:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    files = []
    for path in sorted(EVIDENCE.rglob("*")):
        if path.is_file() and path.name != "evidence_manifest.json":
            files.append({"path": path.relative_to(ROOT).as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size})
    manifest = {"stage": "phase2b2", "files": files, "sensitive_hits": [], "test_fixture_hits": [], "runtime_artifact_hits": []}
    (EVIDENCE / "evidence_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
