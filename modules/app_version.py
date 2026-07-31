from __future__ import annotations

from pathlib import Path
from typing import Any

from modules.app_metadata import (
    APP_SHORT_NAME,
    APP_VERSION,
    BUILD_COMMIT,
    BUILD_TIME_UTC,
    PRODUCT_NAME,
)


def diagnostic_info(install_path: Path, data_path: Path) -> dict[str, Any]:
    return {
        "product": PRODUCT_NAME,
        "version": APP_VERSION,
        "install_path": str(install_path),
        "data_path": str(data_path),
        "build_commit": BUILD_COMMIT,
        "build_time_utc": BUILD_TIME_UTC,
    }
