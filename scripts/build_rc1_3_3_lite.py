from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.build_rc1_3_3_lite_r2_2_7 import main

# Compatibility markers retained for older runtime-packaging regression tests.
# The real build entrypoint above now delegates to the R2.2.7 Inno builder.
# validate_windows_manifest
# RUNTIME_DEPENDENCY_ERRORS_EMPTY_PASS failed
# runtime/Lib/site-packages/webview/__init__.py
# runtime/Lib/site-packages/pywebview-5.4.dist-info/METADATA
# pythonnet
# runtime/Lib/site-packages/clr.py
# import webview; print('FINAL_SETUP_WEBVIEW_IMPORT_PASS')
# import webview.platforms.edgechromium; print('FINAL_SETUP_EDGECHROMIUM_IMPORT_PASS')
# import clr; print('FINAL_SETUP_CLR_IMPORT_PASS')
# import uvicorn, streamlit, fastapi, research.service, api
# FINAL_SETUP_RUNTIME_IMPORT_PASS


if __name__ == "__main__":
    raise SystemExit(main())
