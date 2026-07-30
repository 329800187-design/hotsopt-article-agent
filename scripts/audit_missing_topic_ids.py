from __future__ import annotations

import json
import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "data" / "hotspot_agent.db"


def main() -> int:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    bad: list[tuple[str, str]] = []
    for row in conn.execute("select task_id, selected_topics from generation_tasks"):
        try:
            topics = json.loads(row["selected_topics"] or "[]")
        except Exception:
            bad.append((row["task_id"], "INVALID_JSON"))
            continue
        first = topics[0] if topics and isinstance(topics[0], dict) else None
        if not first or not first.get("id"):
            bad.append((row["task_id"], "MISSING_TOPIC_ID"))
    print("BAD_TOPIC_SNAPSHOTS=", bad)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
