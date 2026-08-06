from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "evidence" / "phase2b3-1-live"


def main() -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    items = []
    for path in sorted(EVIDENCE.rglob("*")):
        if not path.is_file() or path.name == "evidence_manifest.json":
            continue
        relative = "evidence/phase2b3-1-live/" + path.relative_to(EVIDENCE).as_posix()
        items.append({"path": relative, "size": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    status = "REAL_INLINE_IMAGES_PENDING"
    smoke_path = EVIDENCE / "inline_images_live_smoke.json"
    if smoke_path.exists():
        try:
            status = str(json.loads(smoke_path.read_text(encoding="utf-8")).get("status") or status)
        except (OSError, json.JSONDecodeError):
            status = "REAL_INLINE_IMAGES_FAILED"
    manifest = {
        "stage": "2B.3.1",
        "status": status,
        "file_count": len(items),
        "files": items,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    (EVIDENCE / "evidence_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
