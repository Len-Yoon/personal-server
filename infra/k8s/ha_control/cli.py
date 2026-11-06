"""Explicit native Windows controller commands; never automatically promote."""
import argparse
import json
from pathlib import Path
import re
import stat
import sys
import time

from .controller import Controller, Journal, TransitionBlocked, exclusive
from .pg import PgBackend
from .prepare import BundleError, verify


def validate_bundle(plan):
    """Bind the currently executing packaged code to the approved manifest pin."""
    try:
        current = Path(__file__).absolute()
        bundle = current.parents[2]
        if (current != bundle / "files/ha_control/cli.py" or current.resolve(strict=True) != current
                or not re.fullmatch(r"[0-9a-f]{64}", plan["controller_bundle_sha256"])):
            raise TransitionBlocked("approved_controller_bundle_required")
        manifest_path = bundle / "source-manifest.json"
        info = manifest_path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o222 or info.st_nlink != 1:
            raise TransitionBlocked("readonly_controller_manifest_required")
        result = verify(bundle, expected_manifest_sha256=plan["controller_bundle_sha256"])
        if (result["source_commit"] != plan["source_commit"]
                or any(result[key] != plan[key] for key in ("native_windows_code_sha256", "controller_code_sha256"))):
            raise TransitionBlocked("approved_controller_source_mismatch")
    except TransitionBlocked:
        raise
    except (BundleError, OSError, ValueError, KeyError, TypeError, IndexError):
        raise TransitionBlocked("approved_controller_bundle_required") from None


def run(args):
    plan = Path(args.plan)
    journal = Path(args.journal) if args.journal else plan.parent / "controller-state.json"
    if (not plan.is_absolute() or not journal.is_absolute() or journal != plan.parent / "controller-state.json"
            or journal.is_symlink()):
        raise TransitionBlocked("plan_bound_journal_required")
    backend = PgBackend(plan, Path(args.approval))
    with backend.guard():
        p = backend.plan
        validate_bundle(p)
        control = Controller(journal, backend)
        if args.command == "initialize":
            control.initialize(cluster=p["cluster"], generation=p["generation"], old=p["node"],
                               target="B" if p["node"] == "A" else "A", timeline=p["timeline"])
            backend.bind(Journal(journal).read())
            return {"status": "PASS", "phase": "fencing", "authority_opened": False}
        backend.bind(Journal(journal).read())
        if args.command == "advance":
            state = control.advance()
            return {"status": "PASS", "phase": state["phase"], "lease_requires_serve": state["phase"] == "ready"}
        # The single-controller lock AND native Windows exclusion guard remain
        # held throughout serving. A failed renewal exits; the short DB lease
        # expires without inventing a new primary or retrying host recovery.
        with exclusive(journal.with_suffix(".lock")):
            state = Journal(journal).read()
            backend.bind(state)
            if state["phase"] != "ready":
                raise TransitionBlocked("ready_transition_required")
            try:
                while True:
                    started = time.monotonic()
                    backend.assert_ready(state)
                    time.sleep(max(0, 1 - (time.monotonic() - started)))
            finally:
                # Best-effort immediate close; guard loss or outage is covered
                # by the nonrenewed 5s database-clock lease.
                try:
                    backend.close(state)
                except Exception:
                    pass


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("initialize", "advance", "serve"))
    parser.add_argument("--plan", required=True)
    parser.add_argument("--approval", required=True)
    parser.add_argument("--journal")
    args = parser.parse_args(argv)
    try:
        result = run(args)
    except KeyboardInterrupt:
        result = {"status": "STOPPED", "reason": "controller_stopped_lease_not_renewed"}
    except TransitionBlocked as error:
        # Only this package's static reason labels may leave the process.
        reason = str(error)
        result = {"status": "BLOCKED", "reason": reason if reason.replace("_", "").isalnum() else "controller_transition_blocked"}
    except Exception:
        result = {"status": "BLOCKED", "reason": "controller_outcome_unknown"}
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] in {"PASS", "STOPPED"} else 1


if __name__ == "__main__":
    sys.exit(main())
