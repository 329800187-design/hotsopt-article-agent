from __future__ import annotations

from hot_sources.tophub import TopHubOverviewSource
from hot_sources.dedupe import deduplicate_topics
from modules.models import HotTopic


def test_overview_parser_keeps_independent_board_identity(monkeypatch):
    page = """
    <div class="cc-cd" id="node-1">
      <a href="/n/boardA"><div class="cc-cd-lb"><span>平台甲</span></div></a>
      <span class="cc-cd-sb-st">热榜</span>
      <a href="https://a.example/1"><span class="s h">1</span><span class="t">事件甲</span><span class="e">10万</span></a>
    <div class="cc-cd" id="node-2">
      <a href="/n/boardB"><div class="cc-cd-lb"><span>平台乙</span></div></a>
      <span class="cc-cd-sb-st">新闻榜</span>
      <a href="https://b.example/1"><span class="s h">1</span><span class="t">事件乙</span><span class="e">8万</span></a>
    """

    class Response:
        text = page
        status_code = 200
        headers = {"content-type": "text/html; charset=utf-8"}

        def raise_for_status(self):
            return None

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr("hot_sources.tophub.create_http_client", lambda _settings: Client())
    result = TopHubOverviewSource().fetch_trends()
    assert [item.title for item in result] == ["事件甲", "事件乙"]
    assert {item.source for item in result} == {"tophub:boardA", "tophub:boardB"}
    assert all(item.source_url and item.captured_at for item in result)


def test_live_topic_dedupe_handles_width_rank_and_board_tags():
    topics = [
        HotTopic(id="a", title="１、同一事件进入热搜【爆】", source="a", source_url="https://a.example"),
        HotTopic(id="b", title="同一事件进入热搜 热", source="b", source_url="https://b.example"),
        HotTopic(id="c", title="同一事件后续发布新政策", source="c", source_url="https://c.example"),
    ]
    result = deduplicate_topics(topics)
    assert [item.id for item in result] == ["a", "c"]
