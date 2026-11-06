"""Real psycopg + real Book repository cancellation/retry regression."""
import argparse
import json
import os
import sys
import traceback

REQUEST = "00000000-0000-0000-0000-000000009991"


def run(phase):
    import ha_runtime
    from app.services import book_service
    books = book_service.list_books()
    if not books:
        raise AssertionError("real_book_fixture_required")
    book_id = min(book["id"] for book in books)
    acknowledged = False
    try:
        book_service.create_memo(book_id, None, "driver-cancel", "isolated synchronous cancellation", 1, request_id=REQUEST)
        acknowledged = True
    except ha_runtime.DatabaseUnavailable:
        pass
    with ha_runtime.connect("book-memo") as connection:
        ledger = connection.execute("SELECT result_id FROM write_requests WHERE request_id=?", (REQUEST.replace("-", ""),)).fetchone()
        count = connection.execute("SELECT count(*) FROM book_memos WHERE title=?", ("driver-cancel",)).fetchone()[0]
    if ledger is None or count != 1:
        raise AssertionError("cancelled_commit_must_exist_once")
    if acknowledged != (phase == "reprotected"):
        raise AssertionError("unprotected_ledger_must_not_acknowledge")
    return {"status": "PASS", "phase": phase, "acknowledged": acknowledged,
            "local_ledger_exists": True, "record_count": count}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("cancelled", "retry_without_peer", "reprotected"), required=True)
    arguments = parser.parse_args()
    try:
        print(json.dumps(run(arguments.phase), sort_keys=True))
    except Exception as error:
        print(json.dumps({"status": "FAIL", "phase": arguments.phase, "error_class": type(error).__name__,
                          "frames": [{"file": os.path.basename(f.filename), "function": f.name, "line": f.lineno} for f in traceback.extract_tb(error.__traceback__)]}))
        raise SystemExit(1) from None
