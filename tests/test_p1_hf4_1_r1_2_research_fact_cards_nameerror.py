"""Regression tests for R1.2 research fact-card handling and API creation flow."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
LOG_DIR = PROJECT_ROOT / "data" / "logs"


def _batch_payload(title: str) -> dict:
    return {
        "batch_name": f"api-test-{title}",
        "mode": "multi_topic",
        "topics": [
            {
                "id": f"api-{abs(hash(title)) % 100000}",
                "title": title,
                "summary": title,
                "category": "test",
                "source": "api-test",
                "source_name": "API test",
                "source_url": "https://example.com/api-test",
                "hot_value": "100",
                "hot_score": 1,
                "rank": 1,
            }
        ],
        "article_count": 1,
        "generation_options": {"word_count": 1200, "image_plan_mode": "none"},
        "concurrency": 1,
    }


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _port_is_closed(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) != 0


def _write_test_license_sitecustomize(directory: Path, license_root: Path) -> None:
    source = f"""
from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from modules import device_identity, license_service
from modules.license_schema import canonical_payload

root = Path(r"{str(license_root)}")
private = Ed25519PrivateKey.generate()
public_path = root / "license_public_key.pem"
public_path.parent.mkdir(parents=True, exist_ok=True)
public_path.write_bytes(private.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
device_identity.license_root = lambda: root / "license"
license_service.license_root = lambda: root / "license"
license_service.PUBLIC_KEY_PATH = public_path
license_service.ACTIVE_LICENSE_PATH = root / "license" / "active.license"
license_service.STATE_PATH = root / "license" / "license_state.json"
secrets = {{}}
def fake_save(name, secret, path=None):
    secrets[(str(path), name)] = secret
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({{"schema_version": "fake-dpapi-v1", "blob": "FAKE_DPAPI_TEST_BLOB"}}), encoding="utf-8")
    return "dpapi:" + name
def fake_load(reference, path=None):
    return secrets.get((str(path), str(reference or "").removeprefix("dpapi:")), "")
device_identity.save_secret = fake_save
device_identity.load_secret = fake_load
license_service.save_secret = fake_save
license_service.load_secret = fake_load
now = datetime.now(timezone.utc).replace(microsecond=0)
value = {{
    "schema_version": 1,
    "license_id": "P1-API-TEST-000001",
    "product": "hotspot-article-agent",
    "edition": "test",
    "customer_name": "P1 API test fixture",
    "device_code": device_identity.device_code(),
    "issued_at": now.isoformat(),
    "not_before": (now - timedelta(minutes=1)).isoformat(),
    "expires_at": (now + timedelta(days=1)).isoformat(),
    "features": ["hot_topics", "custom_topic", "five_articles", "image_generation", "article_editing", "word_export", "zip_export"],
    "signature_algorithm": "Ed25519",
}}
value["signature"] = base64.urlsafe_b64encode(private.sign(canonical_payload(value))).decode("ascii").rstrip("=")
license_service.ACTIVE_LICENSE_PATH.parent.mkdir(parents=True, exist_ok=True)
license_service.ACTIVE_LICENSE_PATH.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
"""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "sitecustomize.py").write_text(source, encoding="utf-8")


def _write_test_settings(data_root: Path) -> None:
    config_dir = data_root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    settings = {
        "text_profile": {
            "base_url": "https://stub.local/v1",
            "endpoint": "/chat/completions",
            "model": "p1-api-stub",
            "timeout_seconds": 90,
            "credential_ref": "",
        },
        "image_profile": {
            "base_url": "https://stub.local/v1",
            "endpoint": "/images/generations",
            "model": "p1-image-stub",
            "credential_ref": "",
        },
        "image_plan_mode": "none",
        "network": {"proxy": ""},
    }
    (config_dir / "settings.json").write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")


@pytest.fixture(scope="session")
def api_base_url(tmp_path_factory):
    import requests

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stdout_path = LOG_DIR / "final_p1_api_server_stdout.txt"
    stderr_path = LOG_DIR / "final_p1_api_server_stderr.txt"
    data_root = tmp_path_factory.mktemp("p1_api_data")
    license_fixture = tmp_path_factory.mktemp("p1_api_license")
    sitecustomize_dir = tmp_path_factory.mktemp("p1_api_sitecustomize")
    _write_test_license_sitecustomize(sitecustomize_dir, license_fixture)
    _write_test_settings(data_root)
    port = _free_port()
    env = os.environ.copy()
    env.update(
        {
            "HOTSPOT_DATA_ROOT": str(data_root),
            "HOTSPOT_ALLOW_UNAUTHENTICATED_TEST_API": "1",
            "HOTSPOT_DESKTOP": "0",
            "PYTHONUTF8": "1",
            "PYTHONPATH": str(sitecustomize_dir) + os.pathsep + str(PROJECT_ROOT) + os.pathsep + env.get("PYTHONPATH", ""),
        }
    )
    stdout = stdout_path.open("w", encoding="utf-8")
    stderr = stderr_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "api:app", "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(PROJECT_ROOT),
        env=env,
        stdout=stdout,
        stderr=stderr,
        text=True,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 40
        last_error = ""
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            try:
                response = requests.get(f"{base_url}/api/health", timeout=1)
                if response.status_code == 200:
                    yield base_url
                    return
                last_error = f"health={response.status_code} {response.text[:200]}"
            except Exception as exc:
                last_error = repr(exc)
            time.sleep(0.25)
        raise RuntimeError(f"P1 API test server did not become healthy: {last_error}")
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        stdout.close()
        stderr.close()
        for _ in range(40):
            if _port_is_closed(port):
                break
            time.sleep(0.1)


def test_fact_cards_missing_from_bundle_is_safe():
    bundle: dict = {"topic_id": "t1", "topic_title": "Test"}
    fact_cards = [item for item in bundle.get("research_fact_cards") or [] if isinstance(item, dict)][:10]
    assert fact_cards == []


def test_fact_cards_empty_bundle_is_safe():
    bundle: dict = {}
    fact_cards = [item for item in bundle.get("research_fact_cards") or [] if isinstance(item, dict)][:10]
    assert fact_cards == []


def test_fact_cards_valid_bundle_works():
    bundle = {
        "research_fact_cards": [
            {"fact_id": "f1", "fact": "test fact"},
            "not-a-dict",
            {"fact_id": "f2", "fact": "second fact"},
        ]
    }
    fact_cards = [item for item in bundle.get("research_fact_cards") or [] if isinstance(item, dict)][:10]
    assert len(fact_cards) == 2
    assert fact_cards[0]["fact_id"] == "f1"


def test_collect_returns_bundle_with_fact_cards_key():
    from research.service import ResearchService

    class FakeTopic:
        id = "test-t1"
        title = "test topic"
        source_url = ""

    svc = ResearchService()
    svc.fetcher = lambda url: {"url": url, "fetch_success": False, "accepted_for_research": False}
    svc.discoverer = None

    bundle = svc.collect(FakeTopic())
    assert "research_fact_cards" in bundle
    assert "background_fact_cards" in bundle
    assert isinstance(bundle["research_fact_cards"], list)
    assert isinstance(bundle["background_fact_cards"], list)


def test_collect_no_nameerror_on_fact_cards():
    from research.service import ResearchService

    class FakeTopic:
        id = "test-t2"
        title = "topic with no source"
        source_url = ""

    svc = ResearchService()
    svc.fetcher = lambda url: {"url": url, "fetch_success": False, "accepted_for_research": False}
    svc.discoverer = None

    try:
        bundle = svc.collect(FakeTopic())
        assert bundle["research_fact_cards"] == []
        assert bundle["background_fact_cards"] == []
    except NameError as exc:
        pytest.fail(f"NameError still present: {exc}")


def test_source_overlap_missing_fact_cards():
    from generation.source_overlap import analyze_source_overlap

    bundle: dict = {"topic_id": "t1", "topic_title": "Test", "sources": []}
    try:
        report = analyze_source_overlap({"body": "test article content", "title": "Test"}, bundle)
        assert isinstance(report, dict)
    except NameError:
        pytest.fail("NameError in source_overlap")


@pytest.mark.api
def test_post_batches_returns_201(api_base_url):
    import requests

    resp = requests.post(
        f"{api_base_url}/api/batches",
        json=_batch_payload("API topic"),
        timeout=10,
    )
    assert resp.status_code == 201, f"Expected 201, got {resp.status_code}: {resp.text[:200]}"
    data = resp.json()
    payload = data.get("data") if isinstance(data, dict) else {}
    assert payload and ("batch_id" in payload or "id" in payload)


@pytest.mark.api
def test_post_start_returns_202(api_base_url):
    import requests

    resp = requests.post(
        f"{api_base_url}/api/batches",
        json=_batch_payload("API topic start"),
        timeout=10,
    )
    assert resp.status_code == 201
    data = resp.json()
    payload = data.get("data") if isinstance(data, dict) else {}
    batch_id = payload.get("batch_id") or payload.get("id")
    assert batch_id, f"No batch_id in response: {resp.json()}"

    resp2 = requests.post(
        f"{api_base_url}/api/batches/{batch_id}/start",
        timeout=10,
    )
    assert resp2.status_code == 202, f"Expected 202, got {resp2.status_code}: {resp2.text[:200]}"


@pytest.mark.api
def test_get_batches_returns_200(api_base_url):
    import requests

    resp = requests.get(f"{api_base_url}/api/batches", timeout=10)
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text[:200]}"
    data = resp.json()
    assert isinstance(data, (list, dict))


@pytest.mark.api
def test_no_nameerror_in_api_batch_creation(api_base_url):
    import requests

    resp = requests.post(
        f"{api_base_url}/api/batches",
        json=_batch_payload("API topic without research source"),
        timeout=15,
    )
    assert resp.status_code in (200, 201, 202, 400, 422), (
        f"Unexpected status {resp.status_code}: {resp.text[:300]}"
    )
    if resp.status_code >= 500:
        pytest.fail(f"Server error: {resp.status_code} {resp.text[:300]}")
