from __future__ import annotations

import json
import os
from pathlib import Path


PRODUCT_NAME = "热点图文批量生产工作台"
APP_SHORT_NAME = "热点图文工作台"
DATA_DIR_NAME = PRODUCT_NAME
LICENSE_ADMIN_EXE_NAME = "热点图文工作台_本地许可证签发工具.exe"
APP_VERSION = "RC1.3.3-Lite-R2.2.19"
_BUILD_FILE = Path(__file__).with_name("build_metadata.json")
try:
    _BUILD_DATA = json.loads(_BUILD_FILE.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    _BUILD_DATA = {}
BUILD_COMMIT = os.environ.get("HOTSPOT_BUILD_COMMIT") or str(_BUILD_DATA.get("build_commit") or "source-checkout")
BUILD_TIME_UTC = os.environ.get("HOTSPOT_BUILD_TIME_UTC") or str(_BUILD_DATA.get("build_time_utc") or "not-recorded")
