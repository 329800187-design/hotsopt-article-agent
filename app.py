from __future__ import annotations

import logging
import traceback
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

from hot_sources.classifier import CATEGORIES
from hot_sources.service import HotTrendService
from modules.config_store import load_settings, save_settings
from modules.database import get_store
from modules.app_paths import PROJECT_ROOT, data_root
from ui.rc1_app import render_rc1_app


ROOT = PROJECT_ROOT
normal_pages = ["首页", "选择话题", "开始生成", "我的内容", "模型设置", "关于软件"]


def _setup_error_log() -> Path:
    log_dir = data_root() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "error.log"
    logging.basicConfig(
        filename=str(log_path),
        level=logging.ERROR,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return log_path


def _log_error(code: str, exc: Exception, page: str = "", action: str = "", task_id: str = "") -> None:
    tb = traceback.format_exc()
    # Redact API keys from traceback (heuristic: key-like strings)
    import re
    sanitized_tb = re.sub(r'(sk-[a-zA-Z0-9]{20,})', '***REDACTED***', tb)
    sanitized_tb = re.sub(r'(Bearer\s+)[^\s"]{10,}', r'\1***REDACTED***', sanitized_tb)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    try:
        logging.error(
            "code=%s | page=%s | action=%s | task_id=%s | timestamp=%s | type=%s | traceback=%s",
            code, page, action, task_id, timestamp, type(exc).__name__, sanitized_tb
        )
    except Exception:
        pass


settings = load_settings()
store = get_store()
hot_service = HotTrendService(settings, store=store)

st.set_page_config(page_title="热点图文工作台", page_icon=str(ROOT / "ui" / "assets" / "brand.ico"), layout="wide")

# Initialize error log path for downstream use
_log_path = _setup_error_log()
st.session_state["rc1_error_log_path"] = str(_log_path)

try:
    render_rc1_app(settings, save_settings, hot_service, ROOT, CATEGORIES)
except Exception as exc:
    current_page = str(st.session_state.get("rc1_navigation") or "")
    if "选择话题" in current_page:
        code = "TOPIC-SELECT-001"
        message = "选题页面操作未完成，请刷新后重试。"
    elif "开始生成" in current_page:
        code = "TASK-CREATE-001"
        message = "任务创建页面操作未完成，请重新尝试。"
    elif "我的内容" in current_page:
        code = "TASK-STATE-001"
        message = "任务状态读取异常，请稍后重试。"
    elif current_page:
        code = "APP-PAGE-001"
        message = "当前页面加载失败，请重新尝试。"
    else:
        code = "GUI-NAV-001"
        message = "页面导航初始化失败，请重新尝试。"
    _log_error(code, exc, page=current_page or "app_entry", action="render_rc1_app")
    st.error(f"{message}\n错误编号：{code}")
    st.caption("详细错误信息已记录到日志文件。")
    left, right = st.columns(2)
    if left.button("重新加载"):
        st.rerun()
    if right.button("返回首页"):
        st.session_state["rc1_navigation_target"] = "⌂ 首页"
        st.rerun()
