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
    assert "请输入标题或话题后再加入选题篮" in ui
    assert "请粘贴 1～5 个公开网页链接" in ui


def test_SINGLE_TITLE_CREATES_ONE_TOPIC_NOT_ANGLE_DUPLICATES_PASS():
    ui = read("ui/rc1_app.py")
    section = ui[ui.index('if input_mode == "单个话题（可多篇不同角度）"'):ui.index('else:', ui.index('if input_mode == "单个话题（可多篇不同角度）"'))]
    assert "for i in range(article_count)" not in section
    assert '"summary": f"用户输入话题：{title.strip()}"' in section
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
    bundle = {"accepted_source_count": 1, "official_or_reliable_source_count": 1, "sources": [{"source_id": "s1", "fetch_success": True, "accepted_for_research": True, "content": "某公司发布公告。"}]}
    article = {"content_markdown": "根据现有公开资料，某公司发布公告。", "fact_basis": [], "word_count": 800}
    gate = quality_gate(article, bundle)
    assert gate["status"] in {"passed", "warning"}
    assert gate["passed"] is True


def test_UNSUPPORTED_HARD_FACT_STILL_FAILS_PASS():
    bundle = {"accepted_source_count": 1, "sources": [{"source_id": "s1", "fetch_success": True, "accepted_for_research": True, "content": "某公司发布公告。"}]}
    article = {"content_markdown": "某公司发布公告，并造成5000人入院。", "fact_basis": [], "word_count": 800}
    gate = quality_gate(article, bundle)
    assert gate["status"] == "failed"
    assert any("5000人入院" in reason for reason in gate["hard_errors"])
