from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PRODUCT_NAME = "热点图文批量生产工作台"
APP_SHORT_NAME = "热点图文工作台"
APP_VERSION = "RC1.3.3-Lite-P1-HF4.1-R1.2"
BUILD_TIME_UTC = datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def diagnostic_info(install_path: Path, data_path: Path) -> dict[str, Any]:
    return {
        "product": PRODUCT_NAME,
        "version": APP_VERSION,
        "install_path": str(install_path),
        "data_path": str(data_path),
        "build_time_utc": BUILD_TIME_UTC,
    }
