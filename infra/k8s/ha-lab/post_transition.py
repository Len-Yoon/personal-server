"""Actual app/store calls after promotion or a new-system restore."""
import argparse
import json
import os


def run(stage, service, node):
    import ha_runtime
    from ha_runtime.config import WEB
    from app.services import ha_backend
    if stage == "inactive":
        try:
            with ha_runtime.state_transaction(service, "transition_fixture", {}) as state:
                state["inactive_write"] = True
        except ha_runtime.DatabaseUnavailable as error:
            expected = "not_serving_owner" if service in WEB else "not_worker_owner"
            if str(error) != expected:
                raise
            return {"status": "PASS", "stage": stage, "service": service, "node": node, "write_denied": True}
        raise AssertionError("inactive_write_admitted")
    if service in {"book-memo", "youtube-memo"}:
        ha_backend.require_schema()
    key = "transition_fixture"
    with ha_runtime.state_transaction(service, key, {}) as state:
        previous = {"restored": "promoted", "returned": "failed_over"}.get(stage)
        if previous and state.get(previous) is not True:
            raise AssertionError("committed_business_state_missing")
        if stage in {"failed_over", "returned"} and (
            state.get("before") is not True or "inactive_write" in state
        ):
            raise AssertionError("roundtrip_business_state_mismatch")
        state[stage] = True
    if service == "book-memo":
        from app.services import book_service
        books = book_service.list_books()
        if not books:
            raise AssertionError("restored_book_missing")
    if service == "portal-web":
        from app.services import file_store
        path = file_store.get_download_path("documents/preserved.txt")
        try:
            if path.read_bytes() != b"preserved attachment":
                raise AssertionError("restored_file_mismatch")
        finally:
            path.close()
    return {"status": "PASS", "stage": stage, "service": service, "node": node,
            "actual_app_import": True, "protected_write": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", choices=("before", "promoted", "restored", "failed_over", "returned", "inactive"), required=True)
    parser.add_argument("--service", required=True)
    parser.add_argument("--node", choices=("A", "B"), default="B")
    args = parser.parse_args()
    try:
        print(json.dumps(run(args.stage, args.service, args.node), sort_keys=True))
    except Exception as error:
        from ha_runtime.config import DatabaseUnavailable
        import traceback
        from pathlib import Path
        result = {"status": "FAIL", "error_class": type(error).__name__, "service": args.service,
                  "frames": [{"file": Path(f.filename).name, "function": f.name, "line": f.lineno} for f in traceback.extract_tb(error.__traceback__)]}
        if isinstance(error, DatabaseUnavailable):
            result["runtime_reason"] = str(error)
        print(json.dumps(result))
        raise SystemExit(1) from None
