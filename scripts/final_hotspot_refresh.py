"""Capture the final real-network hotspot refresh evidence and enforce its gates."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hot_sources.service import HotTrendService
from modules.config_store import load_settings
from modules.database import get_store


def main() -> int:
    result = HotTrendService(load_settings(), store=get_store()).refresh()
    topics = result.get("topics") or []
    evidence = {
        "status": result.get("status"),
        "captured_at": result.get("captured_at"),
        "provider_diagnostics": result.get("provider_diagnostics") or [],
        "pre_dedupe_live_topics": int(result.get("pre_dedupe_live_topics") or 0),
        "deduplicated_live_topics": int(result.get("deduplicated_live_topics") or 0),
        "topics_with_url_or_identifier": int(result.get("topics_with_url_or_identifier") or 0),
        "topics_with_captured_at": int(result.get("topics_with_captured_at") or 0),
        "cached_topic_count": int(result.get("cached_topic_count") or 0),
        "elapsed_ms": int(result.get("elapsed_ms") or 0),
        "errors": result.get("errors") or [],
        "topics": [topic.to_dict() for topic in topics],
    }
    count = evidence["deduplicated_live_topics"]
    evidence["gates"] = {
        "deduplicated_live_topics_gte_200": count >= 200,
        "all_topics_have_url_or_identifier": evidence["topics_with_url_or_identifier"] == count,
        "all_topics_have_captured_at": evidence["topics_with_captured_at"] == count,
        "no_cached_topics_counted": evidence["cached_topic_count"] == 0,
    }
    logs = ROOT / "data" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "final_hotspot_refresh.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    lines = [
        f"status={evidence['status']}",
        f"captured_at={evidence['captured_at']}",
        f"pre_dedupe_live_topics={evidence['pre_dedupe_live_topics']}",
        f"deduplicated_live_topics={count}",
        f"topics_with_url_or_identifier={evidence['topics_with_url_or_identifier']}",
        f"topics_with_captured_at={evidence['topics_with_captured_at']}",
        f"cached_topic_count={evidence['cached_topic_count']}",
        f"elapsed_ms={evidence['elapsed_ms']}",
        "",
        "provider diagnostics:",
    ]
    for item in evidence["provider_diagnostics"]:
        lines.append(
            " | ".join(
                f"{key}={item.get(key, '')}"
                for key in (
                    "provider_name",
                    "request_url",
                    "http_status",
                    "content_type",
                    "raw_item_count",
                    "normalized_item_count",
                    "deduplicated_item_count",
                    "elapsed_ms",
                    "failure_code",
                    "failure_message",
                    "captured_at",
                )
            )
        )
    lines.extend(["", "gates:", *(f"{key}={value}" for key, value in evidence["gates"].items())])
    (logs / "final_hotspot_refresh.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[:9]))
    return 0 if result.get("status") == "online" and all(evidence["gates"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
