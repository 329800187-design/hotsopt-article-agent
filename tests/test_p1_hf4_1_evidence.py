from __future__ import annotations

import importlib
from pathlib import Path

from fastapi.testclient import TestClient

import api
from hot_sources.service import HotTrendService
from modules.database import SQLiteStore


ROOT = Path(__file__).resolve().parents[1]


def _client_with_store(tmp_path: Path) -> tuple[TestClient, SQLiteStore]:
    store = SQLiteStore(tmp_path / "hf4_1_url.sqlite")
    api.store = store
    api.service = HotTrendService(store=store)
    return TestClient(api.app), store


def test_URL_INPUT_NOT_TOPIC_TITLE_PASS(monkeypatch, tmp_path: Path):
    client, store = _client_with_store(tmp_path)

    class FakeResearchService:
        def fetcher(self, url: str):
            return {
                "fetch_success": True,
                "title": "微信文章真实标题",
                "summary": "这里是抓取到的摘要。",
                "content": "这里是抓取到的正文事实。",
            }

    monkeypatch.setattr(api, "ResearchService", FakeResearchService)

    fetched = client.post("/api/topics/url-fetch", json={"url": "https://mp.weixin.qq.com/s/xxxx"}).json()["data"]
    created = client.post(
        "/api/topics/manual",
        json={
            "title": fetched["title"],
            "summary": fetched["content"],
            "reference_url": "https://mp.weixin.qq.com/s/xxxx",
        },
    ).json()["data"]

    assert created["title"] == "微信文章真实标题"
    assert created["title"] != "https://mp.weixin.qq.com/s/xxxx"
    assert created["source_url"] == "https://mp.weixin.qq.com/s/xxxx"


def test_WECHAT_URL_TITLE_EXTRACTION_PASS(monkeypatch, tmp_path: Path):
    client, _ = _client_with_store(tmp_path)

    class FakeResearchService:
        def fetcher(self, url: str):
            assert url == "https://mp.weixin.qq.com/s/xxxx"
            return {
                "fetch_success": True,
                "title": "微信公众号测试标题",
                "summary": "抓取到的文章摘要。",
                "content": "抓取到的文章正文事实。",
            }

    monkeypatch.setattr(api, "ResearchService", FakeResearchService)
    body = client.post("/api/topics/url-fetch", json={"url": "https://mp.weixin.qq.com/s/xxxx"}).json()

    assert body["success"] is True
    assert body["data"]["title"] == "微信公众号测试标题"
    assert body["data"]["content"].startswith("抓取到的文章正文事实")


def test_URL_STORED_AS_REFERENCE_PASS():
    ui_source = (ROOT / "ui" / "rc1_app.py").read_text(encoding="utf-8")
    assert '"/topics/url-fetch"' in ui_source
    assert '"title": fetched_title[:300]' in ui_source
    assert '"reference_url": raw_input' in ui_source
    assert '"reference_url": url' in ui_source
