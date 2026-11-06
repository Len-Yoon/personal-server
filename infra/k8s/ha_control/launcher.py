"""Absolute, isolated entry point for the source-pinned native controller."""
from pathlib import Path
import sys

if __name__ == "__main__":
    if not sys.flags.isolated or not sys.dont_write_bytecode:
        raise SystemExit("isolated_native_launcher_required")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from ha_control.cli import main
    raise SystemExit(main())
