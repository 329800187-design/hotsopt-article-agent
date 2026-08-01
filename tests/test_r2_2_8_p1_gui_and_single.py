from __future__ import annotations

from pathlib import Path

from generation.angle_planner import plan_angles
from generation.batch_executor import BatchExecutor
from generation.content_quality import quality_gate
from modules.database import SQLiteStore
from modules.generation_store import save_generation_task


ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _topic() -> dict:
    return {"id": "topic-1", "title": "菲公务船侵闯黄岩岛海域意欲何为", "summary": "公开资料显示事件正在发展", "source_name": "测试源"}


def test_GUI_NAV_001_NOT_USED_FOR_TOPIC_PAGE_BUSINESS_ERRORS_PASS():
    app = read("app.py")
    ui = read("ui/rc1_app.py")
    assert 'code = "TOPIC-SELECT-001"' in app
    assert 'code = "TASK-CREATE-001"' in app
    assert 'code = "TASK-STATE-001"' in app
    assert 'code = "GUI-NAV-001"' in app
    assert 'action="render_hotspot_tab"' in ui
    assert 'action="render_title_input_tab"' in ui
    assert 'action="render_batch_links_tab"' in ui


def test_THREE_TOPIC_ENTRY_DEFAULTS_INITIALIZED_BEFORE_WIDGETS_PASS():
    ui = read("ui/rc1_app.py")
    defaults = ui.index('st.session_state.setdefault("rc1_batch_links", "")')
    tabs = ui.index('st.tabs(["📡 今日热点", "✏️ 输入标题/话题", "🔗 批量链接"])')
    assert defaults < tabs
    assert 'st.session_state.setdefault("rc1_link_states", {})' in ui
    assert "请输入标题、话题或链接。" in ui
    assert "粘贴 1～5 个网页链接" in ui


def test_SINGLE_TITLE_CREATES_ONE_TOPIC_NOT_ANGLE_DUPLICATES_PASS():
    ui = read("ui/rc1_app.py")
    start = ui.index('if input_mode == "单个话题"')
    section = ui[start:ui.index('st.markdown("可一次输入最多 5 个标题，每行 1 个。")', start)]
    assert "for i in range(article_count)" not in section
    assert '"summary": f' in section and "raw_input" in section
    assert 'st.session_state["rc1_preferred_article_count"] = int(article_count)' in section


def test_SINGLE_ARTICLE_SKIPS_DIFFERENCE_CHECK_PASS(tmp_path, monkeypatch):
    import generation.batch_executor as batch_module
    import modules.generation_store as generation_store

    monkeypatch.setattr(generation_store, "TASKS_ROOT", tmp_path / "tasks")
    store = SQLiteStore(tmp_path / "hotspot.db")
    batch = store.create_batch("single", "single_topic_multi_angle", [_topic()], {"article_count": 1}, 2, plan_angles(1))
    assert batch["quality_status"] == "not_applicable"
    task = batch["items"][0]["task"]
    store.update_task_status(task["task_id"], "completed")
    save_generation_task({"task_id": task["task_id"], "status": "completed", "stage": "completed", "state_version": 0, "article": {"title": "一篇文章", "content_markdown": "正文"}})

    calls = {"count": 0}

    def fake_compare(_articles):
        calls["count"] += 1
        return {"pairs": [], "violating_pairs": []}

    monkeypatch.setattr(batch_module, "compare_batch_report", fake_compare)
    BatchExecutor(store)._maybe_check_similarity(batch["batch_id"])
    refreshed = store.refresh_batch(batch["batch_id"])
    assert calls["count"] == 0
    assert refreshed["status"] == "completed"
    assert refreshed["quality_status"] == "not_applicable"
    assert refreshed["final_ready"] == 1


def test_SINGLE_ARTICLE_NO_SIMILARITY_REWRITE_PASS():
    ui = read("ui/rc1_app.py")
    assert "len(batch_items) <= 1 or total <= 1" in ui
    assert "正在重新检查 {quality_count} 篇文章的差异" in ui


def test_STUCK_ACTION_BUTTONS_REQUEST_REAL_PARENT_ACTION_PASS():
    components = read("ui/components.py")
    ui = read("ui/rc1_app.py")
    assert "rc1_stuck_cancel_request_" in components
    assert "rc1_stuck_retry_request_" in components
    assert "/cancel" in ui and "stuck_cancel" in ui
    assert "/retry" in ui and "stuck_retry" in ui


def test_QUALITY_GATE_WARNINGS_DO_NOT_BLOCK_DRAFT_PASS():
    bundle = {"accepted_source_count": 1, "official_or_reliable_source_count": 1, "sources": [{"source_id": "s1", "fetch_success": True, "accepted_for_research": True, "content": "\u67d0\u516c\u53f8\u53d1\u5e03\u516c\u544a\u3002"}]}

    def filler(seed: int, length: int = 310) -> str:
        return "".join(chr(0x4E00 + ((seed * 3001 + index * (53 + seed * 2)) % 20000)) for index in range(length))

    sections = [
        {"heading": "\u6838\u9a8c\u8def\u5f84", "body": "\u6838\u9a8c\u8def\u5f84\u8981\u5bf9\u7167\u516c\u544a\u6765\u6e90\u548c\u53d1\u5e03\u4e3b\u4f53\u3002" + filler(31)},
        {"heading": "\u4f20\u64ad\u98ce\u9669", "body": "\u4f20\u64ad\u98ce\u9669\u63d0\u9192\u8bfb\u8005\u4e0d\u8981\u628a\u672a\u6838\u5b9e\u89e3\u8bfb\u5199\u6210\u4e8b\u5b9e\u3002" + filler(32)},
        {"heading": "\u80cc\u666f\u89e3\u91ca", "body": "\u80cc\u666f\u89e3\u91ca\u8bf4\u660e\u516c\u5f00\u8d44\u6599\u3001\u5f71\u54cd\u5206\u6790\u548c\u8bfb\u8005\u5224\u65ad\u4e4b\u95f4\u7684\u5173\u7cfb\u3002" + filler(33)},
    ]
    intro = "\u8fd9\u662f\u4e00\u7bc7\u7ed3\u6784\u5b8c\u6574\u4f46\u5b57\u6570\u7565\u4f4e\u7684\u6d4b\u8bd5\u8349\u7a3f\uff0c\u7528\u6765\u786e\u8ba4 warning \u7ea7\u522b\u4e0d\u4f1a\u963b\u65ad\u53ef\u7f16\u8f91\u6210\u54c1\u3002"
    article = {
        "intro": intro,
        "sections": sections,
        "content_markdown": "# \u6d4b\u8bd5\u6587\u7ae0\n\n" + intro + "\n\n" + "\n\n".join(f"## {section['heading']}\n{section['body']}" for section in sections),
        "fact_basis": [],
        "word_count": 1200,
    }
    gate = quality_gate(article, bundle)
    assert gate["status"] in {"passed", "warning"}
    assert gate["passed"] is True


def test_UNSUPPORTED_HARD_FACT_STILL_FAILS_PASS():
    bundle = {"accepted_source_count": 1, "sources": [{"source_id": "s1", "fetch_success": True, "accepted_for_research": True, "content": "某公司发布公告。"}]}
    article = {"content_markdown": "某公司发布公告，并造成5000人入院。", "fact_basis": [], "word_count": 1200}
    gate = quality_gate(article, bundle)
    assert gate["status"] == "failed"
    assert gate["hard_errors"]
