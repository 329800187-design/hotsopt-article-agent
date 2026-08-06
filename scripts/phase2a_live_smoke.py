from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import api
from fastapi.testclient import TestClient
from modules.config_store import load_settings
from modules.database import get_store
from modules.generation_store import generation_task_dir
from modules.network import create_http_client, resolve_network_settings
from modules.security import redact_sensitive_text
from providers.image_provider import OpenAIImageProvider
from providers.text_provider import OpenAITextProvider, _headers
from scripts.security_scan import scan_tree


OUTPUT = ROOT / "outputs" / "phase2a_live_smoke.json"


def public_profile(profile: dict) -> dict:
    return {
        "model": profile.get("model"),
        "base_url": profile.get("base_url"),
        "endpoint": profile.get("endpoint"),
        "auth_type": profile.get("auth_type"),
        "api_key_present": bool(profile.get("api_key")),
        "timeout_seconds": profile.get("timeout_seconds"),
    }


def _response_text(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    if isinstance(payload.get("output_text"), str):
        return payload["output_text"].strip()
    choices = payload.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        message = choices[0].get("message")
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str):
                return content.strip()
            if isinstance(content, list):
                return " ".join(str(item.get("text") or "") for item in content if isinstance(item, dict)).strip()
    output = payload.get("output")
    if isinstance(output, list):
        chunks: list[str] = []
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if isinstance(content, list):
                chunks.extend(str(part.get("text") or "") for part in content if isinstance(part, dict))
            elif isinstance(content, str):
                chunks.append(content)
        return " ".join(chunks).strip()
    return ""


def _json_content(text: str) -> object | None:
    try:
        return json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return None


def probe_text_capabilities(profile: dict, network_settings: dict | None) -> dict:
    base_url = str(profile.get("base_url") or "").rstrip("/")
    headers = _headers(profile)
    settings = resolve_network_settings(network_settings, profile)
    probes = {
        "responses": {
            "model": profile.get("model"),
            "input": "Return exactly the word OK.",
            "max_output_tokens": 20,
        },
        "json_schema": {
            "model": profile.get("model"),
            "messages": [{"role": "user", "content": "Return JSON with ok=true."}],
            "max_tokens": 30,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "capability_probe",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {"ok": {"type": "boolean"}},
                        "required": ["ok"],
                        "additionalProperties": False,
                    },
                },
            },
        },
    }
    results: dict = {}
    try:
        with create_http_client({**settings, "timeout_seconds": min(float(profile.get("timeout_seconds") or 120), 60)}) as client:
            for name, payload in probes.items():
                endpoint = "/responses" if name == "responses" else str(profile.get("endpoint") or "/chat/completions")
                try:
                    response = client.post(f"{base_url}/{endpoint.lstrip('/')}", headers=headers, json=payload)
                    content_type = response.headers.get("content-type", "")
                    result = {
                        "tested": True,
                        "http_status": response.status_code,
                        "success": bool(getattr(response, "is_success", 200 <= int(response.status_code) < 400)),
                        "content_type": content_type,
                    }
                    try:
                        body = response.json()
                    except (ValueError, json.JSONDecodeError):
                        body = None
                    text = _response_text(body)
                    if name == "responses":
                        result["content_present"] = bool(text)
                        result["content_matches_probe"] = text == "OK"
                        result["success"] = bool(result["success"] and text == "OK")
                        if not result["success"]:
                            result["error_code"] = "INVALID_RESPONSE" if not text else "RESPONSES_CONTENT_MISMATCH"
                    else:
                        parsed = _json_content(text)
                        result["content_present"] = bool(text)
                        result["json_schema_valid"] = isinstance(parsed, dict) and parsed.get("ok") is True
                        result["success"] = bool(result["success"] and result["json_schema_valid"])
                        if not result["success"]:
                            result["error_code"] = "INVALID_RESPONSE" if not text else "JSON_SCHEMA_NOT_ENFORCED"
                    results[name] = result
                except Exception as exc:
                    results[name] = {
                        "tested": True,
                        "success": False,
                        "error": redact_sensitive_text(str(exc)),
                    }
    except Exception as exc:
        results["client"] = {"tested": False, "success": False, "error": redact_sensitive_text(str(exc))}
    return results


def summarize_generation_result(task_id: str, result: dict | None) -> dict | None:
    if not result:
        return None
    summary = {
        key: result.get(key)
        for key in (
            "task_id", "status", "stage", "progress", "retry_count", "attempt",
            "failed_step", "error_code", "retryable", "completed_at",
        )
    }
    article = result.get("article") if isinstance(result.get("article"), dict) else {}
    cover = result.get("cover") if isinstance(result.get("cover"), dict) else {}
    metadata = cover.get("metadata") if isinstance(cover.get("metadata"), dict) else {}
    summary["article"] = {
        "title": article.get("title"),
        "status": article.get("status"),
        "demo_mode": article.get("demo_mode"),
        "section_count": len(article.get("sections", [])) if isinstance(article.get("sections"), list) else 0,
    }
    summary["cover"] = {
        "status": cover.get("status"),
        "path": cover.get("path"),
        "provider_response_type": cover.get("provider_response_type"),
        "metadata": {
            key: metadata.get(key)
            for key in ("mime_type", "width", "height", "bytes", "sha256")
        },
    }
    task_dir = generation_task_dir(task_id)
    summary["files"] = {
        name: {
            "exists": path.exists(),
            "sha256": __import__("hashlib").sha256(path.read_bytes()).hexdigest() if path.exists() else "",
        }
        for name, path in {
            "article_json": task_dir / "article.json",
            "article_markdown": task_dir / "article.md",
            "cover": task_dir / "images" / "cover.png",
        }.items()
    }
    return summary


def main() -> int:
    settings = load_settings()
    text_profile = dict(settings.get("text_profile") or {})
    image_profile = dict(settings.get("image_profile") or {})
    report: dict = {"text_profile": public_profile(text_profile), "image_profile": public_profile(image_profile), "api_key_hits": 0}
    if not text_profile.get("api_key") or not image_profile.get("api_key"):
        report.update({"status": "REAL_GATEWAY_PENDING", "message": "框架完成，真实网关待联调；config/settings.json 缺少文本或图片 API Key"})
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    text_check = OpenAITextProvider(text_profile, network_settings=settings.get("network")).test_connection()
    with __import__("tempfile").TemporaryDirectory(prefix="phase2a-live-") as directory:
        image_check = OpenAIImageProvider(image_profile, network_settings=settings.get("network")).test_connection(Path(directory) / "connect.png")
    report["text_connection"] = text_check.to_dict()
    report["image_connection"] = image_check.to_dict()
    report["capabilities"] = {
        "chat_completions": bool(text_check.success),
        "structured_json": bool(text_check.supports_json),
        "image_generation": bool(image_check.success),
        "image_response_type": image_check.image_response_type or None,
    }
    report["capability_probes"] = probe_text_capabilities(text_profile, settings.get("network")) if text_check.success else {}
    report["security_scan"] = scan_tree(ROOT, [str(text_profile.get("api_key") or ""), str(image_profile.get("api_key") or "")])
    report["api_key_hits"] = len(report["security_scan"]["forbidden_hits"])
    if not text_check.success or not image_check.success:
        image_blocked = image_check.error_code == "IMAGE_GENERATION_NOT_SUPPORTED"
        report.update({
            "status": "IMAGE_GENERATION_NOT_SUPPORTED" if image_blocked else "REAL_GATEWAY_FAILED",
            "message": "MiMo 文档与实测仅支持图片理解，未提供图片生成端点" if image_blocked else "真实网关连接测试未全部成功",
        })
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1

    store = get_store()
    topics = store.list_topics(limit=1)
    if not topics:
        report.update({"status": "REAL_GATEWAY_BLOCKED", "message": "没有可用于 live 验收的热点"})
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1

    options = {"article_type": "热点资讯", "style": "客观通俗", "image_style": "动漫化新闻插画", "word_count": 800}
    client = TestClient(api.app)
    created = client.post("/api/tasks", json={"task_name": "2A live smoke", "mode": "multi_topic", "topic_ids": [topics[0].id], "article_count": 1, "generation_options": options})
    if created.status_code != 201:
        report.update({"status": "REAL_GATEWAY_BLOCKED", "create_response": created.json()})
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1
    task = created.json()["data"]
    started = client.post(f"/api/tasks/{task['task_id']}/run")
    report["run_status_code"] = started.status_code
    deadline = time.monotonic() + 600
    result = None
    while time.monotonic() < deadline:
        response = client.get(f"/api/tasks/{task['task_id']}/result")
        if response.is_success:
            result = response.json().get("data")
            if result and result.get("status") in {"completed", "failed", "partial_success", "cancelled"}:
                break
        time.sleep(2)
    report["result"] = summarize_generation_result(task["task_id"], result)
    schema_probe = report.get("capability_probes", {}).get("json_schema", {})
    probes_ok = bool(schema_probe.get("success") or schema_probe.get("http_status") in {404, 405})
    report["status"] = "REAL_GATEWAY_PASS" if result and result.get("status") == "completed" and probes_ok and not report["security_scan"]["forbidden_hits"] else "REAL_GATEWAY_FAILED"
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "REAL_GATEWAY_PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
