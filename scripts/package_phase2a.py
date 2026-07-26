from __future__ import annotations

import sys
import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.package_phase1 as package_phase1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Package a phase 2A release")
    parser.add_argument("--stage", default="phase2a")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args()
    output = args.output or package_phase1.ROOT / f"hotspot-article-agent-{args.stage}-final.zip"
    manifest = args.manifest or package_phase1.ROOT / f"hotspot-article-agent-{args.stage}-final-manifest.json"
    package_phase1.OUTPUT = output if output.is_absolute() else package_phase1.ROOT / output
    package_phase1.MANIFEST = manifest if manifest.is_absolute() else package_phase1.ROOT / manifest
    package_phase1.main()
