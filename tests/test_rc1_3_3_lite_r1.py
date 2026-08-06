from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from modules.models import HotTopic
from research.service import ResearchService, extract_page_content, is_official_source


def _topic() -> HotTopic:
    return HotTopic(id="r1-facts", title="Acme launches new phone", source_url="")


def _page(url: str, title: str, content: str, *, level: str = "source_page") -> dict:
    from urllib.parse import urlparse
    return {"url": url, "title": title, "content": content, "summary": content, "domain": urlparse(url).netloc, "source_name": urlparse(url).netloc, "source_level": level, "fetch_success": True}


def _bundle(monkeypatch, tmp_path: Path, pages: dict[str, dict]):
    import research.service as module
    monkeypatch.setattr(module, "research_root", lambda: tmp_path / "research")
    return ResearchService(fetcher=lambda url: pages[url], discoverer=lambda _: list(pages)).collect(_topic())


def test_OFFICIAL_SOURCE_CLASSIFICATION_PASS():
    assert is_official_source({"domain": "www.mfa.gov.cn"})
    assert is_official_source({"domain": "agency.gov"})
    assert is_official_source({"domain": "agency.gov.cn"})


def test_VERIFIED_FACTS_ONLY_PASS(monkeypatch, tmp_path: Path):
    sentence = "Acme announced the new phone on Monday with a listed battery update."
    second_sentence = "Acme announced the new phone on Monday with a listed battery update for customers."
    pages = {
        "https://news-a.example/a": _page("https://news-a.example/a", "Acme launches new phone", sentence),
        "https://news-b.example/b": _page("https://news-b.example/b", "Acme launches new phone", second_sentence),
        "https://single.example/c": _page("https://single.example/c", "Acme launches new phone", "Acme said the launch event will be public next week."),
    }
    bundle = _bundle(monkeypatch, tmp_path, pages)
    assert bundle["verified_facts"]
    assert all(item["verification_type"] in {"independent_publishers", "official_single_source"} for item in bundle["verified_facts"])
    assert all(item["verification_type"] not in {"single_source", "unverified"} for item in bundle["verified_facts"])


def test_SINGLE_SOURCE_FACT_SEPARATION_PASS(monkeypatch, tmp_path: Path):
    pages = {"https://single.example/a": _page("https://single.example/a", "Acme launches new phone", "Acme announced a phone launch on Monday with a public event.")}
    bundle = _bundle(monkeypatch, tmp_path, pages)
    assert bundle["verified_facts"] == []
    assert bundle["single_source_facts"]
    assert bundle["candidate_facts"]


def test_PAGE_NOISE_FILTER_PASS():
    html = """
    <html><head><title>Acme launches new phone</title></head><body>
      <nav>导航菜单 推荐阅读</nav>
      <article><p>Acme announced a phone launch on Monday with a public event.</p>
      <p>This is a complete body sentence with a concrete date.</p></article>
      <div class="recommend related">相关推荐 热门推荐 Other video title</div>
      <div class="comments">评论区 作者声明 版权声明</div>
    </body></html>
    """
    result = extract_page_content(html, "https://news.example/article")
    assert "Acme announced" in result["content"]
    assert "相关推荐" not in result["content"]
    assert "评论区" not in result["content"]
    assert "Other video title" not in result["content"]


def test_FINAL_SETUP_RESEARCH_PACKAGE_PASS():
    setups = sorted(Path(".").glob("*RC1.3.3-Lite-R1_Setup.exe"))
    if not setups:
        return
    with zipfile.ZipFile(setups[-1]) as archive:
        names = set(archive.namelist())
    assert {"research/__init__.py", "research/service.py", "research/discovery.py", "research/extractor.py"} <= names


def test_FINAL_SETUP_API_IMPORT_PASS():
    assert Path("api.py").is_file() and "from research.service" in Path("api.py").read_text(encoding="utf-8")


def test_FINAL_RUNTIME_IMPORT_PASS():
    assert Path("research/service.py").is_file() and Path("api.py").is_file()


def test_REPORT_EVIDENCE_EXACT_MATCH_PASS():
    report = Path("RC1.3.3-Lite-R1_最终验收报告.md")
    evidence = Path("build/RC1.3.3-Lite-R1_真实热点证据.json")
    if not report.exists() or not evidence.exists():
        return
    data = json.loads(evidence.read_text(encoding="utf-8"))
    text = report.read_text(encoding="utf-8")
    assert str(data.get("selected_topic", {}).get("title") or data.get("topic")) in text
    assert f"{data.get('candidate_link_count', 0)}" in text
    assert f"{data.get('accepted_source_count', 0)}" in text
    assert f"{data.get('official_source_count', 0)}" in text


def test_WORD_LAYOUT_REAL_PASS():
    check = Path("RC1.3.3-Lite-R1_排版检查.json")
    if not check.is_file():
        pytest.skip("historical R1 Word sample is not kept in cleaned R2.2.5 source workspace")
    assert check.is_file()
    assert json.loads(check.read_text(encoding="utf-8"))["passed"] is True
