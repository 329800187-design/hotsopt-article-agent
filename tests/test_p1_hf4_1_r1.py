from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from generation.source_overlap import _ngrams
from hot_sources.tophub import TopHubToutiaoSource
from scripts.hf4_1_r1_acceptance import _build_article
from modules.models import HotTopic


def test_TOPHUB_HTML_FALLBACK_PROVIDER_PASS():
    html = """
    <tbody>
      <tr><td align="center">1.</td><td><a href="https://example.com/a" target="_blank">甘肃省委书记和省长赶赴山洪现场</a></td><td class="ws">1702.9万</td><td></td></tr>
      <tr><td align="center">2.</td><td><a href="https://example.com/b" target="_blank">10艘万吨大驱齐聚亮相</a></td><td class="ws">1032.9万</td><td></td></tr>
    </tbody>
    """
    source = TopHubToutiaoSource()
    topics = []
    for index, match in enumerate(source.ROW_RE.finditer(html), start=1):
        topic = source.normalize_item(match.groupdict(), index, "2026-07-26T00:00:00+00:00")
        if topic:
            topics.append(topic)
    assert len(topics) == 2
    assert topics[0].title == "甘肃省委书记和省长赶赴山洪现场"
    assert topics[0].hot_value == "1702.9万"
    assert topics[0].source_name == "今日热榜 TopHub"


def test_LIMITED_INFO_DRAFT_PARAGRAPHS_ARE_NOT_DUPLICATED_PASS():
    topic = HotTopic(
        id="r1-topic",
        title="甘肃省委书记和省长赶赴山洪现场",
        summary="TopHub 今日头条热榜第 1 位。",
        source_name="今日热榜 TopHub",
        source_url="https://example.com/hot",
    )
    article = _build_article(
        topic,
        {"research_status": "limited", "sources": [{"source_name": "今日热榜 TopHub", "title": topic.title, "published_at": "2026-07-26", "url": topic.source_url, "accepted_for_research": True}]},
        "event",
        "事件经过",
        ("事件概览", "已知信息与缺口", "为什么受到关注", "后续关注"),
    )
    paragraphs = [section["body"] for section in article["sections"]]
    assert len(paragraphs) == 4
    assert len(set(paragraphs)) == 4
    for left_index, left in enumerate(paragraphs):
        left_grams = _ngrams(left, 5)
        for right in paragraphs[left_index + 1 :]:
            right_grams = _ngrams(right, 5)
            overlap = len(left_grams & right_grams) / max(1, len(left_grams | right_grams))
            assert overlap < 0.5
