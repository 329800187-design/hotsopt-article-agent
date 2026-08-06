from __future__ import annotations

from generation.content_quality import intra_article_quality, quality_gate


def _article(title: str) -> dict:
    lead = "这是一段用于标题质量测试的导语，说明文章有正常正文结构，并且不会依赖标题复制来源。"
    sections = [
        {"heading": "手机坠落事实如何表述", "body": "来源只说明一部手机从飞机上意外坠落，随后被定位并找回。正文应保持事实边界，不把设备坠落改写成其他事件。"},
        {"heading": "读者需要看哪些边界", "body": "公开资料没有提供更多材料结构、技术机制或实验结论，因此文章只能围绕定位找回和信息核验展开。"},
        {"heading": "为什么标题需要重新拟定", "body": "标题可以概括事件价值，但不能直接复制来源标题，也不能通过删改标点伪装成新标题。"},
    ]
    markdown = "\n\n".join([f"# {title}", lead] + [f"## {item['heading']}\n{item['body']}" for item in sections])
    return {"title": title, "intro": lead, "lead": lead, "sections": sections, "content_markdown": markdown}


def _bundle(source_title: str) -> dict:
    return {
        "accepted_source_count": 1,
        "topic_title": source_title,
        "sources": [
            {
                "title": source_title,
                "content": "来源只说明一部 iPhone 从 1.1 千米高度意外坠落，机主通过 Find My 定位并找回手机。",
                "accepted_for_research": True,
                "fetch_success": True,
                "domain": "example.com",
            }
        ],
    }


def test_title_must_not_copy_source_title():
    source_title = "iPhone 从 1.1 千米高空坠落后几乎完好无损并被找回"
    article = _article(source_title)
    report = intra_article_quality(article, _bundle(source_title))
    assert "COPIED_SOURCE_TITLE" in report["failures"]

    gate = quality_gate(article, _bundle(source_title))
    assert "ARTICLE_QUALITY_BLOCKED:COPIED_SOURCE_TITLE" in gate["hard_errors"]


def test_title_quotes_must_be_balanced():
    article = _article("千米坠落后，手机为何几乎“完好无损")
    report = intra_article_quality(article, _bundle("iPhone 从 1.1 千米高空坠落后被找回"))
    assert "UNBALANCED_TITLE_QUOTE" in report["failures"]
