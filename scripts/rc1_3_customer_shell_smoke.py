"""Compatibility entry point for the RC1.3.1 desktop-shell smoke test."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.rc1_3_1_desktop_smoke import main


if __name__ == "__main__":
    raise SystemExit(main())
