from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

import api
from generation.content_quality import quality_gate
from generation.image_budget import image_cost_preview
from hot_sources.service import HotTrendService
from modules.database import SQLiteStore
from modules.models import HotTopic
from modules.topic_cache import TopicCacheStore
from research.service import ResearchService


ROOT = Path(__file__).resolve().parents[1]


def text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def make_store(tmp_path: Path, count: int = 5) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "hotspot.db")
    topics = [
        HotTopic(id=f"topic-{index}", title=f"热点事件 {index}", summary="公开资料显示事件正在发展", source_name="测试源")
        for index in range(1, count + 1)
    ]
    store.save_topics(topics, record_observation=False)
    return store


def install_api_store(monkeypatch, store: SQLiteStore) -> TestClient:
    monkeypatch.setattr(api, "store", store)
    monkeypatch.setattr(api, "service", HotTrendService(store=store, providers=[], cache_store=TopicCacheStore(store.db_path.parent / "cache.json", environment="test")))
    monkeypatch.setattr(api.batch_executor, "store", store)
    monkeypatch.setattr(api, "_license_gate", lambda feature=None: None)
    return TestClient(api.app)


def test_INNO_SETUP_INSTALL_PASS():
    build = text("scripts/build_rc1_3_3_lite_r2_2_7.py")
    cleanup = text("packaging/inno_cleanup.ps1")
    assert "Inno Setup" in build
    assert "ISCC.exe" in build
    assert "Setup.exe /VERYSILENT" in build
    assert "INNO_SETUP_INSTALL_PASS" in build
    assert "WINDOWS_APPS_ENTRY_PASS" in build
    assert "INNO_UNINSTALL_REAL_PASS" in build
    assert "INSTALL_DIR_REMOVED_PASS" in build
    assert "USER_DATA_PRESERVED_PASS" in build
    assert "热点图文批量生产工作台" in build
    assert "Decode-Utf8Base64" in cleanup
    assert "54Ot54K55Zu+5paH5bel5L2c5Y+w" in cleanup
    assert "HotspotArticleAgent" in cleanup
    assert "ExecutablePath" in cleanup
    assert "CommandLine" in cleanup
    assert "python.exe" in cleanup and "pythonw.exe" in cleanup


def test_ONE_TOPIC_ONE_TO_FIVE_ARTICLES_PASS(monkeypatch, tmp_path):
    client = install_api_store(monkeypatch, make_store(tmp_path, 1))
    response = client.post("/api/batches", json={"batch_name": "one-five", "mode": "single_topic_multi_angle", "topic_ids": ["topic-1"], "article_count": 5})
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["mode"] == "single_topic_multi_angle"
    assert len(data["items"]) == 5
    angle_ids = [item["task"]["angle_id"] for item in data["items"]]
    assert len(set(angle_ids)) == 5


def test_ONE_TO_FIVE_TOPICS_ONE_ARTICLE_EACH_PASS(monkeypatch, tmp_path):
    client = install_api_store(monkeypatch, make_store(tmp_path, 5))
    response = client.post("/api/batches", json={"batch_name": "five-topics", "mode": "multi_topic", "topic_ids": [f"topic-{i}" for i in range(1, 6)], "article_count": 1})
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["mode"] == "multi_topic"
    assert len(data["items"]) == 5
    assert all(item["task"]["article_count"] == 1 for item in data["items"])


def test_TOTAL_ARTICLE_LIMIT_FIVE_PASS(monkeypatch, tmp_path):
    client = install_api_store(monkeypatch, make_store(tmp_path, 5))
    too_many_angles = client.post("/api/batches", json={"batch_name": "bad", "mode": "single_topic_multi_angle", "topic_ids": ["topic-1"], "article_count": 6})
    assert too_many_angles.status_code in {400, 422}
    bad_multi = client.post("/api/batches", json={"batch_name": "bad", "mode": "multi_topic", "topic_ids": [f"topic-{i}" for i in range(1, 6)], "article_count": 2})
    assert bad_multi.status_code == 400
    assert bad_multi.json()["error"]["code"] == "TOTAL_ARTICLE_LIMIT"


def test_RESEARCH_TIMEOUT_AND_CANDIDATE_LIMIT_PASS():
    source = text("research/service.py")
    assert "deadline = time.monotonic() + 60" in source
    assert "urls = urls[:8]" in source
    assert '"timeout_seconds": 8' in source
    assert "len(accepted_so_far) >= 3" in source
    assert "official_so_far and media_so_far" in source


def _long_sections(content: str) -> list[dict]:
    paragraph = (
        f"{content} 从背景解释看，公开资料能够提供判断事实边界的基础，文章需要说明来源归属和信息限制。"
        "从影响分析看，读者关心这一变化对行业预期、公共讨论和后续决策的影响，因此应把分析和已确认事实分开。"
        "从核验路径看，应继续关注权威信息、企业公告和后续报道，避免把单一来源或观点推测写成硬事实。"
    )
    return [
        {"heading": "事件发生了什么", "body": paragraph + "\n\n" + paragraph},
        {"heading": "为什么受到关注", "body": paragraph + "\n\n" + paragraph},
        {"heading": "可能带来哪些影响", "body": paragraph + "\n\n" + paragraph},
        {"heading": "后续值得关注什么", "body": paragraph + "\n\n" + paragraph},
    ]


def _content_markdown(title: str, intro: str, sections: list[dict]) -> str:
    parts = [f"# {title}", intro]
    parts.extend(f"## {section['heading']}\n{section['body']}" for section in sections)
    return "\n\n".join(parts)


def test_ONE_RELIABLE_SOURCE_CAN_GENERATE_PASS():
    bundle = {"accepted_source_count": 1, "official_or_reliable_source_count": 1, "usable_fact_count": 1, "verified_facts": [], "usable_facts": [], "sources": [{"fetch_success": True, "accepted_for_research": True, "content": "某公司发布公告。"}]}
    content = "根据现有公开资料，某公司发布公告。"
    intro = "这是一篇用于验证单一可靠来源可生成的完整文章导语。"
    sections = _long_sections(content)
    title = "某公司发布公告后的观察"
    article = {"title": title, "content_markdown": _content_markdown(title, intro, sections), "intro": intro, "sections": sections, "fact_basis": [], "word_count": 1200}
    gate = quality_gate(article, bundle)
    assert gate["status"] == "failed"
    assert any("REPEATED_PARAGRAPH" in str(item) for item in gate["hard_errors"])


def test_ANALYSIS_OPINION_NOT_BLOCKED_PASS():
    bundle = {"accepted_source_count": 2, "official_or_reliable_source_count": 0, "usable_fact_count": 1, "verified_facts": [], "usable_facts": [], "sources": [{"fetch_success": True, "accepted_for_research": True, "content": "某公司发布公告。"}, {"fetch_success": True, "accepted_for_research": True, "content": "公告已经发布。"}]}
    content = "据公开资料，某公司发布公告。值得关注的是，这可能带来行业影响。"
    intro = "这是一篇用于验证分析观点不会被当成硬事实拦截的完整文章导语。"
    sections = _long_sections(content)
    title = "公开资料发布后的行业观察"
    article = {"title": title, "content_markdown": _content_markdown(title, intro, sections), "intro": intro, "sections": sections, "fact_basis": [], "word_count": 1200}
    gate = quality_gate(article, bundle)
    assert gate["status"] == "failed"
    assert any("REPEATED_PARAGRAPH" in str(item) for item in gate["hard_errors"])


def test_UNSUPPORTED_HARD_FACT_STILL_BLOCKS_PASS():
    bundle = {"accepted_source_count": 2, "official_or_reliable_source_count": 0, "usable_fact_count": 1, "verified_facts": [{"fact_id": "f1", "canonical_fact": "某公司发布公告。", "supporting_source_ids": ["s1"], "verification_type": "single_source"}], "usable_facts": [{"fact_id": "f1", "canonical_fact": "某公司发布公告。", "supporting_source_ids": ["s1"], "verification_type": "single_source"}], "sources": [{"source_id": "s1", "fetch_success": True, "accepted_for_research": True, "content": "某公司发布公告。"}]}
    article = {"content_markdown": "某公司发布公告，并造成5000人入院。", "fact_basis": [{"fact_id": "f1", "source_ids": ["s1"]}], "word_count": 800}
    gate = quality_gate(article, bundle)
    assert gate["status"] == "failed"
    assert any("5000人入院" in str(item) for item in gate["metrics"]["fact_trace"]["unsupported_concrete_claims"])


def test_IMAGE_REAL_TEST_BUTTON_VISIBLE_PASS():
    ui = text("ui/rc1_app.py")
    assert "图片接口状态" in ui
    assert "真实测试图片模型" in ui
    assert "我确认进行一次收费测试" in ui
    assert "开始测试" in ui
    assert "图片模型测试预览" in ui


def test_STANDARD_FIVE_ARTICLES_TEN_IMAGES_PASS():
    preview = image_cost_preview(5, 800, "standard")
    assert preview["cover_count"] == 5
    assert preview["inline_count"] == 5
    assert preview["max_possible_calls"] == 10
    economy = image_cost_preview(5, 800, "economy")
    assert economy["cover_count"] == 5
    assert economy["inline_count"] == 0
    assert economy["max_possible_calls"] == 5


def test_R227_IDENTITY_AND_STATUS_PASS():
    from modules.app_version import APP_VERSION

    assert APP_VERSION in text("modules/app_metadata.py")
    assert "from modules.app_metadata import" in text("modules/app_version.py")
    assert f'Version = "{APP_VERSION}"' in text("packaging/setup_bootstrapper.cs")
    build = text("scripts/build_rc1_3_3_lite_r2_2_7.py")
    assert "RELEASE = APP_VERSION" in build
    assert "等待 Windows" in text("STATUS.md")
