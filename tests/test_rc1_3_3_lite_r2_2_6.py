from __future__ import annotations

from pathlib import Path

import httpx
from fastapi.testclient import TestClient

import api
from generation.content_quality import quality_gate
from providers.text_provider import OpenAITextProvider


ROOT = Path(__file__).resolve().parents[1]


def read_text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_UNINSTALLER_DOES_NOT_KILL_ITSELF_PASS():
    source = read_text("packaging/setup_bootstrapper.cs")
    assert "StopProductProcesses(string installRoot, int excludePid)" in source
    assert "Process.GetCurrentProcess().Id" in source
    assert "if (excludePid > 0 && process.Id == excludePid) continue;" in source
    assert "if (excludePid > 0 && pid == excludePid) continue;" in source
    uninstall_body = source[source.index("private static void Uninstall"):source.index("private static void CleanupAfterUninstall")]
    assert uninstall_body.index("File.Copy(Application.ExecutablePath, cleaner, true)") < uninstall_body.index("Process.Start")
    assert "StopProductProcesses(installRoot);" not in uninstall_body


def test_WINDOWS_SETTINGS_UNINSTALL_REAL_PASS():
    source = read_text("packaging/setup_bootstrapper.cs")
    assert 'key.SetValue("UninstallString", "\\""' in source
    assert '"--uninstall"' in source
    assert "CleanupAfterUninstall" in source


def test_INSTALL_DIRECTORY_REMOVED_REAL_PASS():
    source = read_text("packaging/setup_bootstrapper.cs")
    assert "TryDeleteTree(installRoot)" in source
    assert "bool installGone = !Directory.Exists(installRoot)" in source


def test_UNINSTALL_REGISTRY_REMOVED_REAL_PASS():
    source = read_text("packaging/setup_bootstrapper.cs")
    assert "DeleteSubKeyTree" in source
    assert "bool registryGone" in source


def test_R227_TOPIC_BASKET_ALLOWS_FIVE_PASS():
    ui = read_text("ui/rc1_app.py")
    assert "{len(basket)}/5" in ui
    assert "len(basket) >= 5" in ui


def test_R227_SINGLE_TOPIC_ONE_TO_FIVE_ARTICLES_PASS():
    ui = read_text("ui/rc1_app.py")
    assert "single_topic_multi_angle" in ui
    assert "st.slider" in ui
    assert "min_value=1" in ui
    assert "max_value=5" in ui
    assert "concurrency = min(3, count)" in ui


def test_R227_MULTI_ANGLE_IS_ACTIVE_PASS():
    ui = read_text("ui/rc1_app.py")
    assert "angle_id" in ui
    assert '"angles": angles' in ui
    assert "single_topic_multi_angle" in ui


def test_R227_BATCH_QUEUE_ALLOWED_WITH_LIMIT_PASS(monkeypatch):
    monkeypatch.setattr(api, "_license_gate", lambda feature=None: None)
    client = TestClient(api.app)
    body = client.post("/api/batches", json={"batch_name": "x", "mode": "single_topic_multi_angle", "topics": [{"id": "t1", "title": "a"}], "article_count": 5, "concurrency": 1}).json()
    assert body["success"] is True, body
    body = client.post("/api/batches", json={"batch_name": "x", "mode": "multi_topic", "topics": [{"id": f"t{i}", "title": f"topic {i}"} for i in range(1, 7)], "article_count": 1, "concurrency": 1}).json()
    assert body["success"] is False
    assert body["error"]["code"] in {"TOTAL_ARTICLE_LIMIT", "VALIDATION_ERROR"}


def test_TEXT_ARTICLE_CAPABILITY_TEST_PASS(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        payload = __import__("json").loads(request.content.decode("utf-8"))
        assert request.url.path.endswith("/chat/completions")
        assert payload["response_format"] == {"type": "json_object"}
        assert payload["max_tokens"] == 500
        content = {
            "title": "模型能力测试",
            "intro": "这是一段模型能力测试。",
            "sections": [
                {"heading": "一", "body": "模型能力测试正文一。", "image_brief": "无文字"},
                {"heading": "二", "body": "模型能力测试正文二。", "image_brief": "无文字"},
                {"heading": "三", "body": "模型能力测试正文三。", "image_brief": "无文字"},
            ],
            "content_markdown": "# 模型能力测试\n\n正文",
            "fact_basis": [{"fact_id": "test-1", "fact": "模型能力测试", "source_ids": ["mock"]}],
        }
        return httpx.Response(200, json={"choices": [{"message": {"content": __import__("json").dumps(content, ensure_ascii=False)}}]})

    monkeypatch.setattr("providers.text_provider.create_http_client", lambda *_args, **_kwargs: httpx.Client(transport=httpx.MockTransport(handler), base_url="https://mock.local"))
    result = OpenAITextProvider({"api_key": "key", "base_url": "https://mock.local/v1", "endpoint": "/chat/completions", "model": "text-model"}).article_capability_test()
    assert result.success
    assert result.details["structure"] == "normal"


def test_TEXT_TIMEOUT_ACTIONABLE_ERROR_PASS():
    assert "正文生成在 {limit} 秒内未返回结果" in read_text("generation/single_task.py")
    assert "retry_article" in read_text("generation/single_task.py")
    assert "已等待" in read_text("ui/components.py")
    assert "超时上限" in read_text("ui/components.py")


def test_RETRY_ARTICLE_REUSES_RESEARCH_PASS():
    source = read_text("generation/single_task.py")
    assert "load_research_bundle" in source
    assert "return bundle" in source[source.index("def _auto_collect_research"):source.index("def _failure")]


def test_TIMEOUT_NO_AUTOMATIC_RETRY_PASS():
    source = read_text("generation/executor.py")
    assert 'code == "TIMEOUT" and result.get("failed_step") == "generating_article"' in source


def test_NO_INFINITE_PROGRESS_PASS():
    source = read_text("ui/components.py")
    assert "正在生成正文…" in source
    assert "已等待" in source
    assert "当前模型" in source


def test_IMAGE_TEST_BUTTON_VISIBLE_PASS():
    ui = read_text("ui/rc1_app.py")
    assert "图片接口状态" in ui
    assert "真实测试图片模型" in ui
    assert "use_container_width=True, type=\"primary\"" in ui


def test_IMAGE_TEST_ONE_CALL_PASS():
    ui = read_text("ui/rc1_app.py")
    assert "本次将调用图片模型1次" in ui
    assert "自动重试0次" in ui


def test_IMAGE_TEST_PREVIEW_VISIBLE_PASS():
    ui = read_text("ui/rc1_app.py")
    assert "图片模型测试预览" in ui
    assert "st.image(artifact.content" in ui


def test_IMAGE_TEST_SAVED_KEY_PASS():
    api_source = read_text("api.py")
    assert "profile = _current_test_profile(dict(settings.get(\"image_profile\") or {}), payload)" in api_source


def test_IMAGE_TEST_RESPONSIVE_LAYOUT_PASS():
    ui = read_text("ui/rc1_app.py")
    assert "image_save, image_discover = st.columns(2)" in ui
    assert "image_save, image_discover, image_check, image_paid = st.columns(4)" not in ui


def _bundle() -> dict:
    facts = [
        {"fact_id": "f1", "canonical_fact": "公司于2026年7月21日发布公告。", "supporting_source_ids": ["s1"], "verification_type": "official_single_source"},
        {"fact_id": "f2", "canonical_fact": "公司负责人未辞职。", "supporting_source_ids": ["s1"], "verification_type": "official_single_source"},
        {"fact_id": "f3", "canonical_fact": "公司正在正常经营。", "supporting_source_ids": ["s1"], "verification_type": "official_single_source"},
    ]
    content = "".join(item["canonical_fact"] for item in facts)
    return {"accepted_source_count": 1, "usable_fact_count": 3, "official_or_reliable_source_count": 1, "key_organizations": ["公司"], "sources": [{"source_id": "s1", "source_name": "公司公告", "publisher_id": "company.example", "domain": "company.example", "source_level": "official", "fetch_success": True, "accepted_for_research": True, "content": content, "summary": content}], "verified_facts": facts, "usable_facts": facts}


def _long_sections(content: str) -> list[dict]:
    article_content = content.split("\u8d44\u6599\u6765\u6e90", 1)[0].split("\u53c2\u8003\u8d44\u6599", 1)[0]

    def filler(seed: int, length: int = 170) -> str:
        return "".join(chr(0x4E00 + ((seed * 3001 + index * (47 + seed * 2)) % 20000)) for index in range(length))

    return [
        {"heading": "\u4e8b\u4ef6\u53d1\u751f\u4e86\u4ec0\u4e48", "body": article_content + " \u4e8b\u5b9e\u68b3\u7406\u4fdd\u7559\u6765\u6e90\u5f52\u5c5e\u548c\u53ef\u6838\u9a8c\u8fb9\u754c\u3002" + filler(21) + "\n\n" + filler(121)},
        {"heading": "\u4e3a\u4ec0\u4e48\u53d7\u5230\u5173\u6ce8", "body": "\u80cc\u666f\u89e3\u91ca\u8981\u628a\u4e8b\u4ef6\u653e\u5728\u516c\u5f00\u8d44\u6599\u3001\u53d1\u5e03\u4e3b\u4f53\u548c\u8bfb\u8005\u5173\u5207\u4e2d\u7406\u89e3\u3002" + filler(22) + "\n\n" + filler(122)},
        {"heading": "\u53ef\u80fd\u5e26\u6765\u54ea\u4e9b\u5f71\u54cd", "body": "\u5f71\u54cd\u5206\u6790\u53ea\u63d0\u51fa\u89c2\u5bdf\u65b9\u5411\uff0c\u4e0d\u8865\u5199\u6765\u6e90\u6ca1\u6709\u652f\u6301\u7684\u91d1\u989d\u3001\u4eba\u6570\u6216\u5904\u7f6e\u7ed3\u8bba\u3002" + filler(23) + "\n\n" + filler(123)},
        {"heading": "\u540e\u7eed\u503c\u5f97\u5173\u6ce8\u4ec0\u4e48", "body": "\u6838\u9a8c\u8def\u5f84\u548c\u8bfb\u8005\u542f\u793a\u63d0\u9192\u7ee7\u7eed\u67e5\u770b\u539f\u59cb\u94fe\u63a5\u3001\u53d1\u5e03\u65f6\u95f4\u548c\u6743\u5a01\u56de\u5e94\u3002" + filler(24) + "\n\n" + filler(124)},
    ]


def _content_markdown(title: str, intro: str, sections: list[dict]) -> str:
    parts = [f"# {title}", intro]
    parts.extend(f"## {section['heading']}\n{section['body']}" for section in sections)
    return "\n\n".join(parts)


def _article(extra: str) -> dict:
    base = "据公司公告，公司于2026年7月21日发布公告。公司负责人未辞职。公司正在正常经营。"
    content = base + extra
    intro = "这是一篇用于验证硬事实与分析观点边界的完整文章导语。"
    sections = _long_sections(base)
    title = "公司公告发布后的事实边界"
    markdown = _content_markdown(title, intro + extra, sections)
    return {"title": title, "content_markdown": markdown, "intro": intro + extra, "sections": sections, "word_count": 1200, "fact_basis": [{"fact_id": "f1", "fact": "公司于2026年7月21日发布公告。", "source_ids": ["s1"]}, {"fact_id": "f2", "fact": "公司负责人未辞职。", "source_ids": ["s1"]}, {"fact_id": "f3", "fact": "公司正在正常经营。", "source_ids": ["s1"]}]}


def test_HARD_FACT_ERROR_BLOCK_PASS():
    result = quality_gate(_article("随后，负责人辞职，并投入300亿元。"), _bundle())
    assert result["status"] == "failed"
    assert result["hard_error_count"] > 0


def test_SOFT_ANALYSIS_WARNING_ALLOW_EXPORT_PASS():
    result = quality_gate(_article("从现有资料看，这意味着后续经营趋势值得关注。"), _bundle())
    assert result["status"] == "passed"
    assert result["passed"] is True
    assert result["hard_error_count"] == 0


def test_WARNING_ARTICLE_CAN_GENERATE_IMAGES_PASS():
    api_source = read_text("api.py")
    assert 'gate.get("status") == "failed" or int(gate.get("hard_error_count") or 0) > 0' in api_source
    assert "QUALITY_GATE_FAILED" in api_source


def test_QUALITY_REASON_CLAUSE_VISIBLE_PASS():
    result = quality_gate(_article("随后，负责人辞职，并投入300亿元。"), _bundle())
    reason_text = "；".join(result["reasons"])
    assert "负责人辞职" in reason_text
    assert "投入300亿元" in reason_text
