"""Collect one real hotlist snapshot for RC1.3.3-Lite evidence."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hot_sources.service import HotTrendService
from modules.config_store import load_settings
from modules.database import get_store
from research.service import ResearchService


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    service = HotTrendService(load_settings(), store=get_store())
    result = service.refresh()
    evidence = dict(result.get("hotlist_evidence") or {})
    topics = result.get("topics") or []
    research_bundle = ResearchService().collect(topics[0]) if topics and result.get("status") == "online" else {}
    evidence.update({
        "release": "RC1.3.3-Lite-R2",
        "text_model_calls": 0,
        "image_model_calls": 0,
        "is_real_network_capture": result.get("status") == "online",
        "top20": evidence.get("topics") or [{"rank": item.rank, "title": item.title, "hot_value": item.hot_value, "category": item.category, "source_name": item.source_name, "source_url": item.source_url, "captured_at": item.captured_at} for item in topics[:20]],
        "selected_topic": {"id": topics[0].id, "title": topics[0].title, "source_url": topics[0].source_url} if topics else {},
        "research_bundle": research_bundle,
        "candidate_link_count": research_bundle.get("candidate_link_count", 0),
        "accepted_source_count": research_bundle.get("accepted_source_count", 0),
        "rejected_source_count": research_bundle.get("rejected_source_count", 0),
        "independent_publisher_count": research_bundle.get("independent_publisher_count", 0),
        "cross_verified_fact_count": research_bundle.get("cross_verified_fact_count", 0),
        "official_source_count": research_bundle.get("official_source_count", 0),
        "research_status": research_bundle.get("research_status", "not_run"),
        "rejection_reasons": {str(item.get("rejection_reason") or "unknown"): sum(1 for source in research_bundle.get("rejected_sources", []) if str(source.get("rejection_reason") or "unknown") == str(item.get("rejection_reason") or "unknown")) for item in research_bundle.get("rejected_sources", [])},
        "errors": result.get("errors") or [],
        "last_error": result.get("last_error") or "",
    })
    output = ROOT / "build" / "RC1.3.3-Lite-R2_真实热点证据.json"
    output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(evidence, ensure_ascii=False, indent=2))
    return 0 if result.get("status") != "offline" else 1


if __name__ == "__main__":
    raise SystemExit(main())
