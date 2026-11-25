"""Absolute, isolated entry point for the source-pinned native controller."""
import hashlib
import json
from pathlib import Path
import re
import sys


def _verified_probe_sources(root, plan_path, plan_pin):
    """Authenticate imports with stdlib only, before any bundle code can run."""
    def read_pinned(path, want, limit):
        if (not re.fullmatch(r"[a-f0-9]{64}", want) or path.resolve(strict=True) != path.absolute()
                or not path.is_file() or path.stat().st_size > limit):
            raise ValueError("untrusted_probe_source")
        raw = path.read_bytes()
        if len(raw) > limit or hashlib.sha256(raw).hexdigest() != want:
            raise ValueError("untrusted_probe_source")
        return raw

    plan = json.loads(read_pinned(Path(plan_path), plan_pin, 65536))
    manifest = json.loads(read_pinned(root.parent / "source-manifest.json",
                                     plan["controller_bundle_sha256"], 65536))
    expected = {name.removeprefix("files/"): pin for name, pin in manifest["bundle_files"].items()
                if name.startswith("files/")}
    actual = set()
    for entry in root.rglob("*"):
        if entry.resolve(strict=True) != entry.absolute():
            raise ValueError("untrusted_probe_source")
        if entry.is_dir():
            continue
        name = entry.relative_to(root).as_posix()
        read_pinned(entry, expected[name], 4 * 1024 * 1024)
        actual.add(name)
    if actual != set(expected):
        raise ValueError("untrusted_probe_source")


if __name__ == "__main__":
    if not sys.flags.isolated or not sys.dont_write_bytecode:
        raise SystemExit("isolated_native_launcher_required")
    root = Path(__file__).resolve().parents[1]
    if sys.argv[1:2] == ["--verify-saved-reboot-closed-pair"]:
        if len(sys.argv) != 5 or sys.flags.optimize:
            raise SystemExit("initial_closed_pair_unverified")
        try:
            _verified_probe_sources(root, sys.argv[2], sys.argv[3])
        except (OSError, ValueError, TypeError, KeyError):
            raise SystemExit("initial_closed_pair_unverified") from None
        sys.path.insert(0, str(root))
        from ha_control.initial_closed_pair import saved_reboot_main
        raise SystemExit(saved_reboot_main(sys.argv[2:]))
    sys.path.insert(0, str(root))
    from ha_control.cli import main
    raise SystemExit(main())
