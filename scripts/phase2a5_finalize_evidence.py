from __future__ import annotations

import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.config_store import load_settings
from scripts.security_scan import scan_tree


EVIDENCE = ROOT / "evidence" / "phase2a5-live"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    source = ROOT / "outputs" / "phase2a_live_smoke.json"
    if not source.exists():
        print("EVIDENCE_BLOCKED: live smoke output is missing")
        return 1
    shutil.copy2(source, EVIDENCE / "live_smoke_redacted.json")
    restart_path = EVIDENCE / "restart_evidence.json"
    if restart_path.exists():
        restart_value = json.loads(restart_path.read_text(encoding="utf-8-sig"))
        restart_path.write_text(json.dumps(restart_value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    task_path = EVIDENCE / "task_redacted.json"
    if task_path.exists():
        task_value = json.loads(task_path.read_text(encoding="utf-8-sig"))
        article = task_value.get("article") if isinstance(task_value.get("article"), dict) else {}
        task_path.write_text(json.dumps({
            "task_id": task_value.get("task_id"),
            "status": task_value.get("status"),
            "stage": task_value.get("stage"),
            "article": {"title": article.get("title"), "summary": article.get("summary"), "status": article.get("status")},
            "model_info": task_value.get("model_info"),
            "attempt_history": task_value.get("attempt_history", []),
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    settings = load_settings()
    scan = scan_tree(ROOT, [str(settings.get("text_profile", {}).get("api_key") or ""), str(settings.get("image_profile", {}).get("api_key") or "")])
    (EVIDENCE / "security_scan.json").write_text(json.dumps(scan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    files = []
    for path in sorted(EVIDENCE.iterdir()):
        if path.is_file() and path.name != "evidence_manifest.json":
            files.append({"path": path.relative_to(ROOT).as_posix(), "bytes": path.stat().st_size, "sha256": sha256(path)})
    manifest = {
        "stage": "phase2a5",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "files": files,
        "security_status": scan["status"],
        "status": "EVIDENCE_READY" if scan["status"] == "SECURITY_SCAN_PASS" else "EVIDENCE_SECURITY_FAILED",
    }
    (EVIDENCE / "evidence_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0 if manifest["status"] == "EVIDENCE_READY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
