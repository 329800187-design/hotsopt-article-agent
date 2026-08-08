from __future__ import annotations

import json
import zipfile
from pathlib import Path

from PIL import Image

import api
import scripts.build_rc1_3_3_lite_r2_2_7 as release_build
from export.docx_exporter import export_article
from export.zip_exporter import export_article_bundle
from generation.workflow import (
    begin_image_generation,
    complete_image_delivery,
    confirm_article,
    confirm_final_draft,
    confirm_images,
    finish_image_generation,
    image_workflow_gate,
    initialize_workflow,
    prepare_fusion,
    require_export_ready,
)
import modules.generation_store as generation_store
from modules.database import SQLiteStore
from modules.generation_store import save_generation_task
from modules.models import HotTopic


def _article() -> dict:
    body = "这是用于客户交付验证的完整正文，包含事实说明、背景信息和清晰结论。" * 18
    return {
        "title": "R2.2.20 图文交付验证",
        "lead": "完整图文稿验证",
        "body_markdown": body,
        "body_char_count": len(body),
        "sections": [
            {"heading": "事件背景", "body": body[: len(body) // 3]},
            {"heading": "当前进展", "body": body[len(body) // 3 : 2 * len(body) // 3]},
            {"heading": "后续影响", "body": body[2 * len(body) // 3 :]},
        ],
        "quality_gate": {"status": "passed", "hard_error_count": 0},
    }


def _completed_state(tmp_path: Path, task_id: str = "r220-e2e") -> dict:
    image_root = tmp_path / "images"
    image_root.mkdir(parents=True)
    Image.new("RGB", (80, 60), (30, 90, 160)).save(image_root / "cover.png")
    Image.new("RGB", (80, 60), (160, 90, 30)).save(image_root / "section-1.png")
    state = {
        "task_id": task_id,
        "status": "completed",
        "quality_gate": {"status": "passed", "hard_error_count": 0},
        "article": _article(),
        "cover": {"role": "cover", "image_id": "cover", "path": "images/cover.png", "status": "completed"},
        "inline_images": [
            {"role": "inline", "image_id": "section-1", "paragraph_ref": "section-1", "path": "images/section-1.png", "status": "completed"}
        ],
    }
    initialize_workflow(state)
    return state


def test_completed_article_has_single_article_image_entry_and_count_selector():
    source = Path("ui/rc1_app.py").read_text(encoding="utf-8")
    state = {"status": "completed", "quality_gate": {"status": "passed"}, "article": _article()}
    initialize_workflow(state)
    assert image_workflow_gate(state)["reasons"] == ["请先确认文章内容"]
    assert "确认并生成图片" in source
    assert 'st.selectbox("图片数量", [1, 2, 3]' in source
    assert "快捷批量模式" not in source
    assert "确认所选文章并生成图片" not in source


def test_article_confirmation_enables_image_count_selection():
    state = {"status": "completed", "quality_gate": {"status": "passed"}, "article": _article()}
    initialize_workflow(state)
    confirm_article(state)
    assert state["workflow_state"] == "article_confirmed"
    assert image_workflow_gate(state) == {"ready": True, "workflow_state": "article_confirmed", "reasons": []}


def test_two_local_images_create_and_persist_final_document(tmp_path):
    state = _completed_state(tmp_path)
    confirm_article(state)
    begin_image_generation(state)
    finish_image_generation(state)
    confirm_images(state, ["cover", "section-1"])
    prepare_fusion(state)
    assert state["workflow_state"] == "final_draft_pending_preview"
    assert state["final_document"]["document_kind"] == "final_document"
    assert {item["path"] for item in state["final_document"]["images"]} == {"images/cover.png", "images/section-1.png"}
    confirm_final_draft(state)
    require_export_ready(state)


def test_completed_images_auto_create_export_ready_document(tmp_path):
    state = _completed_state(tmp_path, "r220-auto")
    confirm_article(state)
    begin_image_generation(state)
    finish_image_generation(state)
    complete_image_delivery(state)
    assert state["workflow_state"] == "final_draft_confirmed"
    assert state["final_document"]["document_kind"] == "final_document"
    require_export_ready(state)


def test_partial_image_success_auto_creates_document_from_available_images(tmp_path):
    state = _completed_state(tmp_path, "r220-partial")
    state["inline_images"].append({"role": "inline", "image_id": "section-2", "path": "images/section-2.png", "status": "failed"})
    initialize_workflow(state)
    confirm_article(state)
    begin_image_generation(state)
    finish_image_generation(state)
    complete_image_delivery(state)
    assert state["status"] == "completed"
    assert state["workflow_state"] == "final_draft_confirmed"
    assert {item["image_id"] for item in state["final_document"]["images"]} == {"cover", "section-1"}


def test_word_contains_body_two_images_and_relationships(tmp_path):
    state = _completed_state(tmp_path)
    confirm_article(state); begin_image_generation(state); finish_image_generation(state)
    confirm_images(state); prepare_fusion(state); confirm_final_draft(state)
    output = export_article(state["final_document"], tmp_path / "result.docx", tmp_path)
    with zipfile.ZipFile(output) as archive:
        document = archive.read("word/document.xml").decode("utf-8")
        relations = archive.read("word/_rels/document.xml.rels").decode("utf-8")
        media = [name for name in archive.namelist() if name.startswith("word/media/")]
    assert "事件背景" in document and "后续影响" in document
    assert len(media) == 2
    assert relations.count("relationships/image") == 2


def test_three_successful_images_are_all_persisted_and_embedded_when_sections_are_short(tmp_path):
    state = _completed_state(tmp_path, "r220-three")
    Image.new("RGB", (80, 60), (20, 140, 60)).save(tmp_path / "images" / "section-2.png")
    state["inline_images"].append({"role": "inline", "image_id": "section-2", "slot_id": "section-2", "paragraph_ref": "section-2", "path": "images/section-2.png", "status": "completed", "order": 2})
    state["article"]["sections"] = state["article"]["sections"][:1]
    initialize_workflow(state)
    confirm_article(state); begin_image_generation(state); finish_image_generation(state)
    confirm_images(state, ["cover", "section-1", "section-2"]); prepare_fusion(state); confirm_final_draft(state)
    assert len(state["final_document"]["images"]) == 3
    output = export_article(state["final_document"], tmp_path / "three.docx", tmp_path)
    with zipfile.ZipFile(output) as archive:
        assert len([name for name in archive.namelist() if name.startswith("word/media/")]) == 3
        assert archive.read("word/_rels/document.xml.rels").decode("utf-8").count("relationships/image") == 3


def test_batch_image_controls_are_hidden_from_customer_ui():
    source = Path("ui/rc1_app.py").read_text(encoding="utf-8")
    for marker in ("快捷批量模式", "确认所选文章并生成图片", "统一图片数量", "生成图文稿并导出 Word", "导出本次创作 Word"):
        assert marker not in source
    assert '"image_plan_mode": "none"' in source


def test_zip_contains_complete_word_and_images(tmp_path):
    state = _completed_state(tmp_path)
    confirm_article(state); begin_image_generation(state); finish_image_generation(state)
    confirm_images(state); prepare_fusion(state); confirm_final_draft(state)
    output = export_article_bundle(state["final_document"], tmp_path, tmp_path / "result.zip")
    with zipfile.ZipFile(output) as archive:
        names = archive.namelist()
        docx_name = next(name for name in names if name.endswith(".docx"))
        with archive.open(docx_name) as nested:
            docx_bytes = nested.read()
    assert docx_bytes.startswith(b"PK")
    assert len([name for name in names if name.endswith(".png")]) == 2


def test_repeated_export_never_calls_model(tmp_path):
    state = _completed_state(tmp_path)
    confirm_article(state); begin_image_generation(state); finish_image_generation(state)
    confirm_images(state); prepare_fusion(state); confirm_final_draft(state)
    export_article(state["final_document"], tmp_path / "first.docx", tmp_path)
    export_article(state["final_document"], tmp_path / "second.docx", tmp_path)
    assert state["fusion_status"]["model_calls"] == 0


def test_missing_images_has_explicit_gate_reason():
    state = {"status": "completed", "quality_gate": {"status": "passed"}, "article": _article(), "workflow_state": "images_pending_confirmation"}
    gate = image_workflow_gate(state)
    assert gate["ready"] is False
    assert "没有可用的已生成图片" in gate["reasons"]


def test_export_exception_writes_safe_log_and_returns_log_id(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "logs_root", lambda: tmp_path / "logs")
    response = api._export_failure("word", "测试文章", RuntimeError("disk unavailable sk-testsecret123456789"), "WORD_EXPORT_FAILED")
    payload = json.loads(response.body)
    detail = payload["error"]["detail"]
    assert detail["stage"] == "word"
    assert detail["article_title"] == "测试文章"
    assert detail["log_id"].startswith("EXP-")
    log_path = tmp_path / "logs" / "exports" / f"{detail['log_id']}.json"
    assert log_path.is_file()
    assert "disk unavailable" in log_path.read_text(encoding="utf-8")
    assert "api_key" not in log_path.read_text(encoding="utf-8").lower()
    assert "sk-testsecret123456789" not in log_path.read_text(encoding="utf-8")


def test_installed_package_includes_latest_ui_workflow_component():
    source = Path("scripts/package_phase1.py").read_text(encoding="utf-8")
    ui = Path("ui/rc1_app.py").read_text(encoding="utf-8")
    assert '"ui"' in source
    assert "确认并生成图片" in ui


def test_export_uses_final_document_instead_of_draft_article():
    source = Path("api.py").read_text(encoding="utf-8")
    assert 'state.get("final_document") if state.get("workflow_state")' in source


def test_batch_export_is_hidden_from_customer_ui():
    source = Path("ui/rc1_app.py").read_text(encoding="utf-8")
    assert "batch_export_ready" not in source
    assert "导出本次创作 Word" not in source


def test_export_ui_shows_stage_title_reason_retry_and_log_id():
    source = Path("ui/rc1_app.py").read_text(encoding="utf-8")
    for marker in ("导出失败阶段", "文章：", "原因：", "重试导出", "日志编号"):
        assert marker in source


def test_api_exports_persisted_final_document_to_word_and_zip(tmp_path, monkeypatch):
    monkeypatch.setattr(generation_store, "TASKS_ROOT", tmp_path / "tasks")
    monkeypatch.setattr(api, "exports_root", lambda: tmp_path / "exports")
    store = SQLiteStore(tmp_path / "state.sqlite")
    topic = HotTopic(id="r220-topic", title="R2.2.20", source_name="test")
    store.save_topics([topic], record_observation=False)
    task = store.create_task("R2.2.20", "multi_topic", [topic.to_dict()], 1)
    monkeypatch.setattr(api, "store", store)
    root = generation_store.generation_task_dir(task["task_id"])
    state = _completed_state(root, task["task_id"])
    confirm_article(state); begin_image_generation(state); finish_image_generation(state)
    confirm_images(state); prepare_fusion(state); confirm_final_draft(state)
    state["state_version"] = 1
    save_generation_task(state)
    word = api._article_export(task["task_id"], "word")
    zipped = api._article_export(task["task_id"], "zip")
    assert Path(word.path).is_file()
    assert Path(zipped.path).is_file()
    assert (generation_store.load_generation_task(task["task_id"]) or {}).get("final_document")


def test_dotnet_gate_selects_repository_sdk_when_default_machine_has_dotnet10(monkeypatch, capsys):
    responses = iter(["10.0.302 [C:/dotnet/sdk]\n8.0.423 [C:/dotnet/sdk]\n", "8.0.423\n"])
    monkeypatch.setattr(release_build.subprocess, "check_output", lambda *_args, **_kwargs: next(responses))
    monkeypatch.setattr(release_build.shutil, "which", lambda _name: "C:/dotnet/dotnet.exe")
    assert release_build.require_dotnet_sdk() == "8.0.423"
    output = capsys.readouterr().out
    assert '"resolved_sdk": "8.0.423"' in output
    assert "C:/dotnet/dotnet.exe" in output


def test_ci_reports_package_gate_separately_from_inno_compilation():
    workflow = Path(".github/workflows/windows-delivery-ci.yml").read_text(encoding="utf-8")
    assert "Verify repository .NET 8 SDK selection" in workflow
    assert 'package_build = "${{ steps.package_build.outcome }}"' in workflow
    assert 'inno_build = "${{ steps.inno_build.outcome }}"' not in workflow
