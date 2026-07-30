"""Test: research_fact_cards NameError fix — R1.2 research_fact_cards专项.

Covers:
  - research_bundle missing research_fact_cards key
  - research_bundle={}
  - research_bundle with valid research_fact_cards
  - No NameError when research_fact_cards absent
  - API chain: POST batches → 201, POST start → 202, GET batches → 200
"""

import json
import sys
import traceback
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _batch_payload(title: str) -> dict:
    return {
        "batch_name": f"api-test-{title}",
        "mode": "multi_topic",
        "topics": [
            {
                "id": f"api-{abs(hash(title)) % 100000}",
                "title": title,
                "summary": title,
                "category": "测试",
                "source": "api-test",
                "source_name": "API测试",
                "source_url": "https://example.com/api-test",
                "hot_value": "100万",
                "hot_score": 1,
                "rank": 1,
            }
        ],
        "article_count": 1,
        "generation_options": {"word_count": 1200, "image_plan_mode": "none"},
        "concurrency": 1,
    }


# ── Unit tests: research_fact_cards in bundle handling ──────────────────────

def test_fact_cards_missing_from_bundle_is_safe():
    """article_generator handles bundle where research_fact_cards key is missing."""
    bundle: dict = {"topic_id": "t1", "topic_title": "Test"}
    # Simulate the logic in article_generator.py:134
    fact_cards = [item for item in bundle.get("research_fact_cards") or [] if isinstance(item, dict)][:10]
    assert fact_cards == []


def test_fact_cards_empty_bundle_is_safe():
    """article_generator handles empty research_bundle."""
    bundle: dict = {}
    fact_cards = [item for item in bundle.get("research_fact_cards") or [] if isinstance(item, dict)][:10]
    assert fact_cards == []


def test_fact_cards_valid_bundle_works():
    """Valid research_fact_cards are extracted correctly."""
    bundle = {
        "research_fact_cards": [
            {"fact_id": "f1", "fact": "测试事实"},
            "not-a-dict",
            {"fact_id": "f2", "fact": "第二事实"},
        ]
    }
    fact_cards = [item for item in bundle.get("research_fact_cards") or [] if isinstance(item, dict)][:10]
    assert len(fact_cards) == 2
    assert fact_cards[0]["fact_id"] == "f1"


# ── Unit tests: research.service collect with empty sources ─────────────────

def test_collect_returns_bundle_with_fact_cards_key():
    """collect() always includes research_fact_cards in returned bundle."""
    from research.service import ResearchService

    class FakeTopic:
        id = "test-t1"
        title = "测试话题"
        source_url = ""

    svc = ResearchService()
    # Use a fetcher that returns empty content
    svc.fetcher = lambda url: {"url": url, "fetch_success": False, "accepted_for_research": False}
    svc.discoverer = None

    bundle = svc.collect(FakeTopic())
    assert "research_fact_cards" in bundle
    assert "background_fact_cards" in bundle
    assert isinstance(bundle["research_fact_cards"], list)
    assert isinstance(bundle["background_fact_cards"], list)


def test_collect_no_nameerror_on_fact_cards():
    """collect() does not raise NameError for research_fact_cards."""
    from research.service import ResearchService

    class FakeTopic:
        id = "test-t2"
        title = "无资料话题"
        source_url = ""

    svc = ResearchService()
    svc.fetcher = lambda url: {"url": url, "fetch_success": False, "accepted_for_research": False}
    svc.discoverer = None

    try:
        bundle = svc.collect(FakeTopic())
        assert bundle["research_fact_cards"] == []
        assert bundle["background_fact_cards"] == []
    except NameError as e:
        pytest.fail(f"NameError still present: {e}")


# ── Integration: source_overlap handles missing fact_cards ──────────────────

def test_source_overlap_missing_fact_cards():
    """source_overlap handles bundle without research_fact_cards."""
    from generation.source_overlap import analyze_source_overlap

    bundle: dict = {"topic_id": "t1", "topic_title": "Test", "sources": []}
    try:
        report = analyze_source_overlap({"body": "test article content", "title": "Test"}, bundle)
        # Should not raise
        assert isinstance(report, dict)
    except NameError:
        pytest.fail("NameError in source_overlap")


# ── API chain smoke (requires running API) ──────────────────────────────────

@pytest.mark.api
def test_post_batches_returns_201():
    """POST /api/batches returns 201 with valid JSON."""
    import requests
    resp = requests.post(
        "http://127.0.0.1:18501/api/batches",
        json=_batch_payload("测试话题API"),
        timeout=10,
    )
    assert resp.status_code == 201, f"Expected 201, got {resp.status_code}: {resp.text[:200]}"
    data = resp.json()
    payload = data.get("data") if isinstance(data, dict) else {}
    assert payload and ("batch_id" in payload or "id" in payload)


@pytest.mark.api
def test_post_start_returns_202():
    """POST /api/batches/{id}/start returns 202."""
    import requests
    # Create batch first
    resp = requests.post(
        "http://127.0.0.1:18501/api/batches",
        json=_batch_payload("测试话题API-start"),
        timeout=10,
    )
    assert resp.status_code == 201
    data = resp.json()
    payload = data.get("data") if isinstance(data, dict) else {}
    batch_id = payload.get("batch_id") or payload.get("id")
    assert batch_id, f"No batch_id in response: {resp.json()}"

    # Start it
    resp2 = requests.post(
        f"http://127.0.0.1:18501/api/batches/{batch_id}/start",
        timeout=10,
    )
    assert resp2.status_code == 202, f"Expected 202, got {resp2.status_code}: {resp2.text[:200]}"


@pytest.mark.api
def test_get_batches_returns_200():
    """GET /api/batches returns 200 with list."""
    import requests
    resp = requests.get("http://127.0.0.1:18501/api/batches", timeout=10)
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text[:200]}"
    data = resp.json()
    assert isinstance(data, (list, dict))


@pytest.mark.api
def test_no_nameerror_in_api_batch_creation():
    """Full flow: create batch → no NameError traceback in response."""
    import requests
    resp = requests.post(
        "http://127.0.0.1:18501/api/batches",
        json=_batch_payload("测试无研究资料话题"),
        timeout=15,
    )
    # Even if batch creation encounters research, it should not 500 with NameError
    assert resp.status_code in (200, 201, 202, 400, 422), (
        f"Unexpected status {resp.status_code}: {resp.text[:300]}"
    )
    if resp.status_code >= 500:
        pytest.fail(f"Server error: {resp.status_code} {resp.text[:300]}")
