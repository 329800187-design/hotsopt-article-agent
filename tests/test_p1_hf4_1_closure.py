from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from export.layout_pipeline import check_article_product
from generation.batch_executor import BatchExecutor
from generation.single_task import _build_local_fallback_article
from modules.database import SQLiteStore
from modules.models import HotTopic


def _topic() -> HotTopic:
    return HotTopic(
        id="hf4-1-topic",
        title="HF4.1 构建前复核专题",
        summary="围绕基础稿中文化、排版门禁与并发限制的专项检查。",
        source="test",
        source_name="测试来源",
        source_url="https://example.com/topic",
    )


def _bundle() -> dict:
    return {
        "research_status": "sufficient",
        "accepted_source_count": 1,
        "sources": [
            {
                "source_name": "测试来源",
                "title": "HF4.1 构建前复核公告",
                "published_at": "2026-07-26",
                "url": "https://example.com/source-1",
                "fetch_success": True,
                "accepted_for_research": True,
            }
        ],
        "verified_facts": [
            {"canonical_fact": "本轮复核聚焦基础稿中文化输出。"},
            {"canonical_fact": "排版门禁需要按自然段数量而非空行数判断。"},
        ],
        "timeline": ["2026-07-26"],
        "background": ["专项复核要求三分钟内给出正文或基础稿。"],
    }


def test_LOCAL_FALLBACK_CHINESE_PASS():
    article = _build_local_fallback_article(_topic(), {"name": "公共价值"}, "解读", "客观", _bundle(), "TIMEOUT")
    assert article["title"].startswith("HF4.1 构建前复核专题")
    assert article["used_local_fallback"] is True
    assert article["body_char_count"] > 0
    assert "\u9239" not in article["content_markdown"]


def test_SECTION_COUNT_NOT_BREAK_COUNT_PASS():
    article = {
        "title": "三节三段排版测试",
        "sections": [
            {"heading": "事件概览", "body": "第一节保留一段完整文字，用于验证自然段计数。"},
            {"heading": "背景信息", "body": "第二节同样只有一段，但应被视为有效正文小节。"},
            {"heading": "后续关注", "body": "第三节继续使用单段正文，整体仍应通过排版检查。"},
        ],
        "content_markdown": "# 三节三段排版测试\n\n## 事件概览\n第一节保留一段完整文字，用于验证自然段计数。\n\n## 背景信息\n第二节同样只有一段，但应被视为有效正文小节。\n\n## 后续关注\n第三节继续使用单段正文，整体仍应通过排版检查。",
    }
    report = check_article_product(article)
    assert report["passed"] is True


def test_WALL_OF_TEXT_BLOCK_PASS():
    wall = "这是一段过长的连续正文，用于验证长墙文本门禁。" * 60
    article = {
        "title": "长墙文本测试",
        "sections": [
            {"heading": "事件概览", "body": wall},
            {"heading": "背景信息", "body": "第二节为普通段落。"},
            {"heading": "后续关注", "body": "第三节为普通段落。"},
        ],
        "content_markdown": "# 长墙文本测试",
    }
    report = check_article_product(article)
    assert report["passed"] is False
    assert any("长墙文本" in reason for reason in report["reasons"])


def test_TEXT_CONCURRENCY_MAX_THREE_PASS(tmp_path: Path):
    executor = BatchExecutor(store=SQLiteStore(tmp_path / "hf4_1.sqlite"), max_workers=9)
    assert executor.max_workers == 3
    assert executor.single_executor.pool._max_workers == 3
