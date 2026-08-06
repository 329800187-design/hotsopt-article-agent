"""Compatibility development entry point using the same desktop lifecycle."""
from __future__ import annotations

import os
from pathlib import Path


ROOT = Path(__file__).resolve().parent
os.environ.setdefault("HOTSPOT_LAUNCH_MODE", "source")
os.environ.setdefault("HOTSPOT_DESKTOP", "1")
os.environ.setdefault("HOTSPOT_NO_BROWSER", "1")


if __name__ == "__main__":
    from desktop_host import main

    raise SystemExit(main())
