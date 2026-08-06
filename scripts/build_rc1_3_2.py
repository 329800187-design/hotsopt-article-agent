"""Compatibility entry point retained for source rebuild checks."""

from scripts.build_rc1_3_3_lite import main


if __name__ == "__main__":
    raise SystemExit(main())
