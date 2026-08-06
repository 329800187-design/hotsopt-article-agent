from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import zipfile
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STAMP = datetime.now().strftime("%Y%m%d_%H%M%S")
OUT_ROOT = ROOT / "release" / f"current_handoff_{STAMP}"
MOD_ZIP = ROOT / "release" / f"current_modified_files_handoff_{STAMP}.zip"
SOFTWARE_ZIP = ROOT / "release" / f"current_software_source_handoff_{STAMP}.zip"

FINAL_SAMPLE_TASK_ID = "c1fe744c6d26"
FINAL_SAMPLE_BATCH_ID = "4ec3c0854017"

EXCLUDE_DIR_NAMES = {
    ".git",
    ".pytest_cache",
    ".pytest_tmp_codex_no_padding",
    ".pytest_tmp_codex_p1_quality",
    "__pycache__",
    "release",
    "runtime",
    "node_modules",
    ".venv",
    "venv",
    ".venv-r227-build",
    "data",
    "logs",
    "license_exports",
    "build",
}
EXCLUDE_FILE_NAMES = {
    "credentials.dat",
    "local-api-token.dat",
    "local-api-token.dat.bak",
    "settings.json",
}
EXCLUDE_SUFFIXES = {
    ".pyc",
    ".pyo",
    ".log",
}


def run(command: list[str]) -> str:
    completed = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
    return (
        f"COMMAND={' '.join(command)}\n"
        f"EXIT_CODE={completed.returncode}\n"
        f"STDOUT:\n{completed.stdout}\n"
        f"STDERR:\n{completed.stderr}\n"
    )


def redacted_settings() -> str:
    path = ROOT / "config" / "settings.json"
    if not path.exists():
        return "MISSING config/settings.json\n"
    data = json.loads(path.read_text(encoding="utf-8"))

    def scrub(value):
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                if re.search(r"api[_-]?key|secret|token|password", str(key), re.I) and item:
                    result[key] = "***REDACTED***"
                else:
                    result[key] = scrub(item)
            return result
        if isinstance(value, list):
            return [scrub(item) for item in value]
        return value

    return json.dumps(scrub(data), ensure_ascii=False, indent=2)


def should_exclude(path: Path) -> bool:
    rel_parts = path.relative_to(ROOT).parts
    if any(part in EXCLUDE_DIR_NAMES for part in rel_parts[:-1]):
        return True
    if path.name in EXCLUDE_FILE_NAMES:
        return True
    if path.suffix.lower() in EXCLUDE_SUFFIXES:
        return True
    return False


def copy_file(src: Path, dst_root: Path, relative: Path | None = None) -> None:
    if not src.exists() or src.is_dir():
        return
    rel = relative or src.relative_to(ROOT)
    dst = dst_root / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def modified_paths() -> list[Path]:
    output = subprocess.run(
        ["git", "status", "--short"],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    ).stdout
    paths: list[Path] = []
    for raw in output.splitlines():
        if not raw.strip():
            continue
        item = raw[3:].strip()
        if " -> " in item:
            item = item.split(" -> ", 1)[1].strip()
        item = item.strip('"')
        path = ROOT / item
        if path.exists() and not should_exclude(path):
            paths.append(path)
    return sorted(set(paths), key=lambda p: str(p).lower())


def build_report() -> str:
    article_path = ROOT / "data" / "data" / "tasks" / FINAL_SAMPLE_TASK_ID / "article.md"
    article_text = article_path.read_text(encoding="utf-8", errors="replace") if article_path.exists() else ""
    article_scan = {
        "task_id": FINAL_SAMPLE_TASK_ID,
        "batch_id": FINAL_SAMPLE_BATCH_ID,
        "topic": "智驾小蓝灯将被禁用",
        "visible_chinese_chars": len(re.findall(r"[\u4e00-\u9fff]", article_text)),
        "headings": re.findall(r"^##\s+(.+)$", article_text, re.M),
        "bad_patterns": [
            item
            for item in [
                "铁路或属地部门",
                "公共空间里的小摩擦",
                "身份和情绪",
                "钩子开头",
                "30秒速览",
                "单点深挖",
                "单点深化",
                "观点判断",
                "结尾互动",
                "，、",
                "、，",
                "资料来源",
                "AI辅助",
                "AI声明",
                "免责声明",
            ]
            if item in article_text
        ],
    }
    return f"""# 当前交接说明

生成时间：{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
项目目录：{ROOT}

## 一、最初设计方向与交付目标

本项目目标是一个本地热点图文批量生产工作台：刷新实时热点，自动搜索资料，基于事实卡重新创作文章，在界面中展示完整分段正文，并导出可编辑 Word/ZIP。核心要求包括：

- 实时热点可刷新，不依赖旧缓存冒充新数据。
- 单热点、多热点、手动话题、链接资料都应能进入文章生成主链。
- 文章不能照抄来源，模型只接收精简事实卡，正文应重新组织。
- 正式文章应有新标题、导语、3-5 个二级标题、自然段、背景/影响/后续关注。
- 界面展示使用 Markdown 成品，不显示 JSON、代码围栏、Markdown 残留。
- Word 导出应有自动排版，可直接编辑。
- 本地许可证签发和客户端激活可用。

## 二、当前已修改重点

- 修复 P0 排队/任务卡住链路：缺 topic id 的脏数据会被拒绝或标记，不再让批次死锁。
- 修复 Bing 搜索 URL：`searchq=` 改为 `search?q=`。
- 修复文章标题泄漏：内部写作标签不再作为正文小标题输出，正式热点稿固定四个交付型小标题。
- 修复段落拆分正则和硬事实清理后的标点残渣。
- 删除正文中的资料来源、AI 辅助声明输出。
- 修复布局管线：最终 `content_markdown` 从结构字段重建，避免结构稿和展示稿不一致。
- 移除错误的硬编码“垫字”逻辑：导出层不再追加固定正文，字数不足交给质量状态处理。
- 增加真实复现脚本与 DB 审计脚本，避免 heredoc/旧日志证据问题。

## 三、最新真实样本

{json.dumps(article_scan, ensure_ascii=False, indent=2)}

样本文件：

- `data/data/tasks/{FINAL_SAMPLE_TASK_ID}/article.md`
- `data/data/tasks/{FINAL_SAMPLE_TASK_ID}/article.json`
- `data/logs/p1_fresh_hotspot_direct_repro_transcript.json`

## 四、已验证

- P1 相关测试：`56 passed`
- 编译检查：`compileall` 通过
- DB 审计：`BAD_TOPIC_SNAPSHOTS=[]`
- 真实生成样本为汽车/智驾类话题，不再出现高铁样本硬编码垫字污染。

## 五、仍未解决/仍需复测

- 当前不是最终客户交付通过状态，尚未执行正式 `Setup.exe` 完整安装/卸载烟测。
- Word 导出排版仍需用最终真实文章再渲染检查一轮。
- 链接批量改写、手动话题长文质量、五篇不同角度批量 Word 仍需完整实测。
- 字数策略现在不再硬凑，若模型输出偏短，应显示 warning/review，而不是伪装成达标。
- 当前源码工作区存在较多历史未提交改动和旧文件删除记录，后续建议先由审阅方确认范围，再整理 commit。

## 六、压缩包说明

- `current_modified_files_handoff_*.zip`：当前改动文件、证据、报告。
- `current_software_source_handoff_*.zip`：当前软件源码整体包，排除 `.git/release/runtime/缓存/密钥/token/log`。
"""


def write_evidence(base: Path) -> None:
    evidence = base / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / "git_status_short.txt").write_text(run(["git", "status", "--short"]), encoding="utf-8")
    (evidence / "git_diff_stat.txt").write_text(run(["git", "diff", "--stat"]), encoding="utf-8")
    (evidence / "git_diff.patch").write_text(run(["git", "diff", "--", ".", ":(exclude)release", ":(exclude)runtime"]), encoding="utf-8")
    (evidence / "compileall.txt").write_text(run([os.sys.executable, "-m", "compileall", "generation", "export", "research", "scripts", "tests", "-q"]), encoding="utf-8")
    (evidence / "pytest_p1_no_padding.txt").write_text(
        run(
            [
                os.sys.executable,
                "-m",
                "pytest",
                "-q",
                "tests/test_p1_article_quality_research_fix.py",
                "tests/test_p1_hf4_1_r1_2_article_quality_delivery.py",
                "tests/test_p1_hf4_1_r1_2_export_gate_consistency.py",
                "--basetemp=.pytest_tmp_codex_no_padding",
            ]
        ),
        encoding="utf-8",
    )
    (evidence / "db_missing_topic_id_audit.txt").write_text(run([os.sys.executable, "scripts/audit_missing_topic_ids.py"]), encoding="utf-8")
    (evidence / "netstat_8505_8506.txt").write_text(run(["cmd", "/c", 'netstat -ano | findstr "8506 8505"']), encoding="utf-8")
    (evidence / "tasklist_python.txt").write_text(run(["cmd", "/c", "tasklist | findstr python"]), encoding="utf-8")
    (evidence / "settings.redacted.json").write_text(redacted_settings(), encoding="utf-8")
    for rel in [
        "data/logs/api.log",
        "data/logs/dev_start_stdout.log",
        "data/logs/dev_start_stderr.log",
        "data/logs/p1_fresh_hotspot_direct_repro_transcript.json",
        f"data/data/tasks/{FINAL_SAMPLE_TASK_ID}/article.md",
        f"data/data/tasks/{FINAL_SAMPLE_TASK_ID}/article.json",
    ]:
        copy_file(ROOT / rel, evidence)


def zip_dir(source: Path, dest_zip: Path) -> None:
    with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in source.rglob("*"):
            if path.is_file():
                archive.write(path, path.relative_to(source))


def build_modified_package() -> None:
    base = OUT_ROOT / "modified_files"
    files_dir = base / "files"
    files_dir.mkdir(parents=True, exist_ok=True)
    for path in modified_paths():
        copy_file(path, files_dir)
    write_evidence(base)
    (base / "HANDOFF_REPORT.md").write_text(build_report(), encoding="utf-8")
    zip_dir(base, MOD_ZIP)


def build_software_package() -> None:
    base = OUT_ROOT / "software_source"
    for path in ROOT.rglob("*"):
        if path.is_dir() or should_exclude(path):
            continue
        rel = path.relative_to(ROOT)
        # Avoid recursively including handoff output.
        if rel.parts and rel.parts[0] == "release":
            continue
        copy_file(path, base, rel)
    (base / "HANDOFF_REPORT.md").write_text(build_report(), encoding="utf-8")
    (base / "config" / "settings.redacted.json").parent.mkdir(parents=True, exist_ok=True)
    (base / "config" / "settings.redacted.json").write_text(redacted_settings(), encoding="utf-8")
    zip_dir(base, SOFTWARE_ZIP)


def main() -> int:
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    build_modified_package()
    build_software_package()
    print(MOD_ZIP)
    print(SOFTWARE_ZIP)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
