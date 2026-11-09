#!/usr/bin/env python3
"""Actual web applications' isolated SQLite-to-PG migration and smoke checks.

This fixture creates synthetic data only. It never contacts a provider or a
production endpoint, and it prints only check names, booleans, counts and hashes.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import importlib
import io
import json
import mimetypes
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import sys
import traceback
from types import SimpleNamespace
from unittest.mock import patch
import zipfile
import uuid

RUN_TAG=os.getenv("HA_SMOKE_RUN_TAG","")
if RUN_TAG not in {"","recovered"}:raise ValueError("invalid_synthetic_run_tag")

def tagged(value):return value+("-"+RUN_TAG if RUN_TAG else "")

def request_tag(value):return str(uuid.uuid5(uuid.NAMESPACE_URL,"synthetic-fixture:"+RUN_TAG+":"+value)) if RUN_TAG else value

SERVICES = ("portal-web", "book-memo", "youtube-memo")
SOURCE = Path(__file__).resolve().parents[3]
PASSWORD = "isolated-fixture-password"
CREATE_REQUEST = "00000000-0000-0000-0000-000000000011"
EDIT_REQUEST = "00000000-0000-0000-0000-000000000012"
SMOKE_REQUEST = "00000000-0000-0000-0000-000000000021"
HIGH_WATER = 9000


def check(condition, label):
    if not condition:
        error = AssertionError()
        error.safe_check = label
        raise error


def _scratch(service):
    root = Path(os.environ.get("HA_LAB_SCRATCH", "/fixture/results"))
    check(root.is_absolute() and not root.is_symlink(), "private_fixture_scratch")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    check(not root.stat().st_mode & 0o077, "private_fixture_scratch_mode")
    return root / (service + "-web-fixture.json")


def _write_fixture(service, payload):
    path = _scratch(service)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as target:
        json.dump(payload, target)
        target.flush()
        os.fsync(target.fileno())


def _read_fixture(service):
    path = _scratch(service)
    check(not path.is_symlink() and not path.stat().st_mode & 0o077, "private_fixture_result")
    return json.loads(path.read_text())


def _clear_app():
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]


def _environment(service):
    os.environ.pop("HA_PG_CONFIG", None)
    root = Path("/tmp/ha-seed") / service
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.environ.update({
        "APP_ENV": "test", "HA_LOCAL_RUNTIME": "1", "DELETE_PASSWORD": PASSWORD,
        "FILE_MANAGER_ACCESS_PASSWORD": PASSWORD, "FILE_MANAGER_AUTH_REQUIRED": "true",
        "SECURITY_LOG_PATH": str(root / "security.log"),
        "AUTH_RATE_LIMIT_STATE_PATH": str(root / "auth-failures.json"),
        "AUTH_SESSION_STATE_PATH": str(root / "sessions.sqlite3"),
        "NEWS_BOOKMARK_DB_PATH": str(root / "bookmarks.sqlite3"),
        "PORTFOLIO_CONTENT_PATH": str(root / "portfolio.md"),
        "FILE_STORAGE_PATH": str(root / "files"),
        "BOOK_MEMO_DB_PATH": str(root / "book.sqlite3"),
        "YOUTUBE_MEMO_DB_PATH": str(root / "youtube.sqlite3"),
    })
    os.chdir(SOURCE / service)
    sys.path.insert(0, str(SOURCE / service))
    sys.path.insert(0, str(SOURCE / "infra/k8s"))
    _clear_app()
    return root


def _activate(service, node):
    os.environ["HA_PG_CONFIG"] = "/fixture/config/" + service + "-" + node + ".json"
    _clear_app()


@contextmanager
def _admin():
    import psycopg
    path = Path("/fixture/admin.json")
    check(not path.is_symlink() and stat.S_ISREG(path.stat().st_mode) and not path.stat().st_mode & 0o077,
          "private_admin_configuration")
    arguments = json.loads(path.read_text())
    check(set(arguments) <= {"host", "port", "dbname", "user", "sslmode", "sslrootcert", "passfile"}
          and arguments.get("host") == "ha-a" and arguments.get("port", 5432) == 5432
          and arguments.get("sslmode") == "verify-full"
          and arguments.get("sslrootcert") == "/fixture/ca.crt"
          and arguments.get("passfile") == "/fixture/postgres.pgpass", "authenticated_local_admin_only")
    with psycopg.connect(**arguments) as connection:
        yield connection


def _grants(admin, service, components):
    from ha_runtime.migrate import grant_namespace
    roles = [service.replace("-", "_") + suffix for suffix in ("_a", "_b")]
    for component in components:
        grant_namespace(admin, "s_" + service.replace("-", "_") + "_" + component, roles)


def _memo_api(service):
    return importlib.import_module("app.services." + ("book_service" if service == "book-memo" else "memo_service"))


def _create_parent(service, repository, *, new=False):
    if service == "book-memo":
        return repository.create_or_get_book({"isbn": tagged("fixture-new") if new else "fixture-preserved", "title": "Fixture Book", "authors": "Fixture"})
    video = ("AAAAAAAAAAC" if RUN_TAG else "AAAAAAAAAAB") if new else "AAAAAAAAAAA"
    return repository.create_or_get_video("https://www.youtube.com/watch?v=" + video,
                                          title_fetcher=lambda *_: "Fixture Video")


def _create_memo(service, repository, parent, request=CREATE_REQUEST, title="seed", content="original"):
    if service == "book-memo":
        repository.create_memo(parent, None, title, content, 2, "fixture", request)
        return next(row for row in repository.list_memos(parent) if row["title"] == title)
    return repository.create_memo(parent, title, content, "fixture", request)


def _edit_memo(service, repository, memo, content, request=EDIT_REQUEST, version=None):
    version = memo["version"] if version is None else version
    if service == "book-memo":
        return repository.update_memo(memo["id"], memo["title"], content, 2, None,
                                      expected_version=version, request_id=request)
    return repository.update_memo(memo["id"], memo["title"], content,
                                  expected_version=version, request_id=request)


def seed_memo(service, root, node):
    repository = _memo_api(service)
    repository.init_db()
    parent = _create_parent(service, repository)
    live = _create_memo(service, repository, parent["id"])
    _edit_memo(service, repository, live, "edited")
    deleted = _create_memo(service, repository, parent["id"],
                           request="00000000-0000-0000-0000-000000000013", title="deleted")
    repository.delete_memo(deleted["id"], expected_version=deleted["version"],
                           request_id="00000000-0000-0000-0000-000000000014")
    with repository._connect() as connection:
        for table in (("books", "book_chapters", "book_memos", "memo_revisions") if service == "book-memo" else ("videos", "memos", "memo_revisions")):
            connection.execute("UPDATE sqlite_sequence SET seq=? WHERE name=?", (HIGH_WATER, table))
    main = importlib.import_module("app.main")
    token = main._create_write_session()
    main._record_auth_failure("preserved-fixture-client")
    failures = json.loads(main.AUTH_RATE_LIMIT_STATE_PATH.read_text())
    # Legacy memory holds bearer tokens; only hashes enter persistent PG state.
    sessions = {hashlib.sha256(key.encode()).hexdigest(): value.isoformat()
                for key, value in main._WRITE_SESSIONS.items()}
    from ha_runtime.migrate import copy_sqlite, create_namespace, seed_state
    with _admin() as admin:
        manifest = copy_sqlite(admin, service, "main", repository.DB_PATH)
        create_namespace(admin, service, "state")
        seed_state(admin, service, "auth-rate-limit", failures)
        seed_state(admin, service, "write-sessions", sessions)
        _grants(admin, service, ["main", "state"])
    fixture = {"parent_id": parent["id"], "memo_id": live["id"], "deleted_id": deleted["id"],
               "token": token, "manifest": manifest, "high_water": HIGH_WATER}
    _write_fixture(service, fixture)
    return {"sqlite_rows_preserved": True, "json_state_preserved": True,
            "tables": len(manifest["tables"])}


def seed_portal(root, node):
    from app.services import file_store, portfolio_store, security, news_bookmarks
    from app.services.homeops import HomeOpsService
    security.AUTH_SESSION_STORE.start()
    token = security.create_auth_session("file_access", 3600)
    confirmation = security.AUTH_SESSION_STORE.create_confirmation("fixture", "payload", 3600)
    security.record_auth_failure("file_access", "preserved-fixture-client")
    failures = json.loads(security.AUTH_RATE_LIMIT_STATE_PATH.read_text())
    portfolio_store.save_portfolio_content("# Fixture portfolio", expected_revision=portfolio_store._revision(""))
    news_bookmarks.save("https://example.invalid/preserved", "Fixture saved article", "private note")
    homeops = HomeOpsService(root / "homeops.sqlite3", SimpleNamespace())
    operation = homeops._create_operation({"healthy": [], "unhealthy": [], "fixture": True})
    file_store.create_directory("", "documents")
    upload = SimpleNamespace(filename="preserved.txt", content_type="text/plain", file=io.BytesIO(b"preserved attachment"))
    file_store.save_upload("documents", upload)
    ordinary = file_store.get_download_path("documents/preserved.txt")
    expected_hash = hashlib.sha256(ordinary.read_bytes()).hexdigest()
    session_path = security.AUTH_SESSION_STORE.path
    security.AUTH_SESSION_STORE.close()
    with sqlite3.connect(session_path) as source:
        source.row_factory = sqlite3.Row
        session_state = {table: {row["token_hash"]: {key: row[key] for key in ("scope", "payload_hash", "expires_at")}
                                 for row in source.execute("SELECT * FROM " + table)}
                         for table in ("sessions", "confirmations")}
    from ha_runtime.migrate import copy_sqlite, create_namespace, seed_state
    with _admin() as admin:
        manifests = [copy_sqlite(admin, "portal-web", "homeops", root / "homeops.sqlite3"),
                     copy_sqlite(admin, "portal-web", "bookmarks", root / "bookmarks.sqlite3")]
        create_namespace(admin, "portal-web", "state")
        seed_state(admin, "portal-web", "auth_sessions", session_state)
        seed_state(admin, "portal-web", "auth_failures", failures)
        seed_state(admin, "portal-web", "portfolio", {"content": portfolio_store.load_portfolio_content()})
        _grants(admin, "portal-web", ["homeops", "bookmarks", "state"])
    _activate("portal-web", node)
    from app.services import file_store as pg_files, ha_files
    pg_files.create_directory("", "documents")
    with ordinary.open("rb") as source:
        pg_files.save_upload("documents", SimpleNamespace(filename="preserved.txt", content_type="text/plain", file=source))
    namespace = ha_files.backup_manifest()
    check(namespace["entries"]["documents/preserved.txt"]["sha256"] == expected_hash, "attachment_hash_preserved")
    _write_fixture("portal-web", {"token": token, "confirmation": confirmation, "operation_id": operation,
                                 "attachment_hash": expected_hash, "manifests": manifests})
    return {"sqlite_components_preserved": 2, "hashed_sessions_preserved": True,
            "json_state_preserved": True, "ordinary_file_hash_preserved": True}


def _memo_session(main, token):
    main._WRITE_SESSIONS.clear()
    return main._has_write_session(SimpleNamespace(cookies={main.WRITE_AUTH_COOKIE: token}))


def smoke_memo(service, node):
    fixture = _read_fixture(service)
    repository = _memo_api(service)
    main = importlib.import_module("app.main")
    repository.init_db()
    parent = fixture["parent_id"]
    memo = next(row for row in repository.list_memos(parent) if row["id"] == fixture["memo_id"])
    check(memo["version"] == 2 and memo["content"] == "edited", "legacy_memo_version_preserved")
    check(_memo_session(main, fixture["token"]), "legacy_session_shared")
    main._AUTH_FAILURES.clear()
    with main._auth_rate_limit_lock():
        main._reload_auth_failures()
        check(len(main._AUTH_FAILURES.get("preserved-fixture-client", [])) == 1, "legacy_auth_failure_preserved")
    recovered = repository.memo_recovery_state(parent)[1]
    check(any(row["id"] == fixture["deleted_id"] for row in recovered), "deleted_memo_preserved")
    if node == "A":
        if RUN_TAG:
            old_parent=fixture["smoke_parent_id"]
            old_rows=repository.list_memos(old_parent)
            old=_create_memo(service,repository,old_parent,SMOKE_REQUEST,"smoke","protected")
            check(old["id"]==fixture["smoke_memo_id"] and repository.list_memos(old_parent)==old_rows,"restored_memo_receipt_replays_without_mutation")
        child = _create_parent(service, repository, new=True)
        check(child["id"] > fixture["high_water"], "deleted_identity_highwater_preserved")
        created = _create_memo(service, repository, child["id"], request_tag(SMOKE_REQUEST), "smoke", "protected")
        _create_memo(service, repository, child["id"], request_tag(SMOKE_REQUEST), "smoke", "protected")
        check(len(repository.list_memos(child["id"])) == 1, "same_request_retry_no_duplicate")
        _edit_memo(service, repository, created, "changed", request=request_tag("00000000-0000-0000-0000-000000000022"))
        from app.services.write_safety import WriteConflict
        try:
            _edit_memo(service, repository, created, "stale", request=request_tag("00000000-0000-0000-0000-000000000023"))
        except WriteConflict:
            pass
        else:
            raise AssertionError("stale_version_must_conflict")
        fixture["smoke_parent_id"], fixture["smoke_memo_id"] = child["id"], created["id"]
        fixture["shared_token"] = main._create_write_session()
        main._record_auth_failure(tagged("cross-node-client"))
        _write_fixture(service, fixture)
    else:
        check(_memo_session(main, fixture["shared_token"]), "new_session_survives_node_change")
        records = repository.list_memos(fixture["smoke_parent_id"])
        check(len(records) == 1 and records[0]["content"] == "changed", "protected_commit_survives_node_change")
        main._AUTH_FAILURES.clear()
        with main._auth_rate_limit_lock():
            main._reload_auth_failures()
            check(len(main._AUTH_FAILURES.get(tagged("cross-node-client"), [])) == 1, "auth_failure_shared_across_nodes")
    search = repository.search_books_and_memos if service == "book-memo" else repository.search_videos_and_memos
    check(bool(search("fixture")), "native_pg_search")
    check(repository.memo_trash_summary(parent)["total_count"] == 1, "native_pg_json_byte_summary")
    from fastapi.testclient import TestClient
    with TestClient(main.app, headers={"Origin": "http://testserver"}) as client:
        check(client.get("/ready").status_code == 200, "actual_http_ready")
        check(client.get("/").status_code == 200, "actual_http_list")
        if node == "B":
            check(_memo_session(main, fixture["http_token"]), "http_login_session_shared")
        response = client.post("/auth/login", data={"password": PASSWORD}, follow_redirects=False)
        check(response.status_code == 303 and bool(client.cookies.get(main.WRITE_AUTH_COOKIE)), "actual_http_login")
        if node == "A":
            fixture["http_token"] = client.cookies.get(main.WRITE_AUTH_COOKIE)
            _write_fixture(service, fixture)
    return {"migration_verified": True, "retry_verified": True, "session_shared": True, "native_search": True,
            "node": node}


def smoke_portal(root, node):
    fixture = _read_fixture("portal-web")
    from app.services import file_store, ha_files, portfolio_store, security, news_bookmarks
    from app.services.homeops import HomeOpsService
    check(security.has_auth_session("file_access", fixture["token"]), "legacy_hashed_session_valid")
    security._AUTH_FAILURES.clear()
    with security._auth_rate_limit_lock():
        security._reload_auth_failures()
        check(len(security._AUTH_FAILURES.get(("file_access", "preserved-fixture-client"), [])) == 1,
              "legacy_auth_failures_preserved")
    check(news_bookmarks.listing()[0]["title"] == "Fixture saved article", "bookmark_preserved")
    homeops = HomeOpsService(root / "unused.sqlite3", SimpleNamespace())
    check(any(row["operation_id"] == fixture["operation_id"] for row in homeops.operation_history()), "homeops_journal_preserved")
    content, revision = portfolio_store.load_portfolio_snapshot()
    check(content == ("# Fixture portfolio" if node == "A" and not RUN_TAG else ("# Fixture changed" if node == "A" else tagged("# Fixture changed"))), "portfolio_json_preserved")
    attachment = file_store.get_download_path("documents/preserved.txt")
    check(hashlib.sha256(attachment.read_bytes()).hexdigest() == fixture["attachment_hash"], "attachment_hash_preserved")
    if node == "A":
        if RUN_TAG:
            check(not security.AUTH_SESSION_STORE.consume_confirmation("fixture","payload",fixture["confirmation"]),"restored_confirmation_stays_consumed")
            fixture["confirmation"]=security.AUTH_SESSION_STORE.create_confirmation("fixture","payload",3600)
        check(security.AUTH_SESSION_STORE.consume_confirmation("fixture", "payload", fixture["confirmation"]), "legacy_confirmation_consumed")
        check(not security.AUTH_SESSION_STORE.consume_confirmation("fixture", "payload", fixture["confirmation"]), "confirmation_one_time")
        changed_revision = portfolio_store.save_portfolio_content(tagged("# Fixture changed"), expected_revision=revision)
        check(portfolio_store.save_portfolio_content(tagged("# Fixture changed"), expected_revision=revision) == changed_revision,
              "portfolio_lost_response_retry")
        try:
            portfolio_store.save_portfolio_content("# stale", expected_revision=revision)
        except portfolio_store.PortfolioConflict:
            pass
        else:
            raise AssertionError("portfolio_stale_version_conflict")
        file_store.save_upload("documents", SimpleNamespace(filename=tagged("new")+".txt", content_type="text/plain", file=io.BytesIO(b"new protected blob"+RUN_TAG.encode())))
        from app.services import file_management
        token = file_management.trash("documents/"+tagged("new")+".txt")
        check(file_management.restore(token) == "documents/"+tagged("new")+".txt", "trash_restore_roundtrip")
        token = file_management.trash("documents/"+tagged("new")+".txt")
        preview = file_management.preview_purge(item_id=token)
        check(file_management.confirm_purge(preview["confirmation"])["ok"], "trash_purge_metadata_commit")
        try:
            file_management.confirm_purge(preview["confirmation"])
        except ValueError:
            pass
        else:
            raise AssertionError("purge_confirmation_one_time")
        fixture["shared_token"] = security.create_auth_session("file_access", 3600)
        _write_fixture("portal-web", fixture)
    else:
        check(portfolio_store.load_portfolio_content() == tagged("# Fixture changed"), "portfolio_commit_survives_node_change")
        check(security.has_auth_session("file_access", fixture["shared_token"]), "new_session_survives_node_change")
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        file_store.write_download_archive(archive, ["documents"])
    with zipfile.ZipFile(stream) as archive:
        check(archive.read("documents/preserved.txt") == b"preserved attachment", "shared_blob_zip_read")
    namespace = ha_files.backup_manifest()
    check(namespace["blob_gc"] == "disabled-until-backup-pins", "no_unpinned_blob_gc")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.routers import files
    application = FastAPI()
    application.include_router(files.router)
    with TestClient(application) as client:
        client.cookies.set(files.FILE_ACCESS_COOKIE, fixture["token"])
        response = client.get("/files/download", params={"path": "documents/preserved.txt"})
        check(response.status_code == 200 and response.content == b"preserved attachment", "actual_http_shared_download")
        check("preserved.txt" in response.headers["content-disposition"], "original_download_filename")
        if node == "B":
            check(security.has_auth_session("file_access", fixture["http_token"]), "http_login_session_shared")
        response = client.post("/files/login", data={"password": PASSWORD}, follow_redirects=False)
        check(response.status_code == 303, "actual_http_file_login")
        if node == "A":
            # Remove the manually supplied old cookie before reading the new
            # domain-scoped response cookie, avoiding ambiguous cookie names.
            issued = response.cookies.get(files.FILE_ACCESS_COOKIE)
            check(bool(issued), "http_login_cookie_issued")
            fixture["http_token"] = issued
            _write_fixture("portal-web", fixture)
    return {"sqlite_components_verified": 2, "sessions_shared": True, "files_shared": True, "namespace_version": namespace["version"], "node": node}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--service", choices=SERVICES, required=True)
    parser.add_argument("--phase", choices=("seed", "smoke"), required=True)
    parser.add_argument("--node", choices=("A", "B"), default="A")
    args = parser.parse_args()
    try:
        root = _environment(args.service)
        if args.phase == "seed":
            result = seed_portal(root, args.node) if args.service == "portal-web" else seed_memo(args.service, root, args.node)
        else:
            _activate(args.service, args.node)
            result = smoke_portal(root, args.node) if args.service == "portal-web" else smoke_memo(args.service, args.node)
    except Exception as error:
        # Exception messages, source lines and locals can expose SQL, bound
        # values, credentials and session tokens. Emit locations only.
        result = {"status": "FAIL", "service": args.service, "phase": args.phase,
                  "error_class": type(error).__name__,
                  "frames": [{"file": Path(frame.filename).name, "function": frame.name, "line": frame.lineno}
                             for frame in traceback.extract_tb(error.__traceback__)]}
        from ha_runtime.config import DatabaseUnavailable
        if isinstance(error, DatabaseUnavailable):
            # Runtime reasons are static labels, never database exceptions.
            result["runtime_reason"] = str(error)
        if isinstance(error, AssertionError):
            label = getattr(error, "safe_check", None)
            if label is None and error.args and error.args[0] in {
                    "stale_version_must_conflict", "portfolio_stale_version_conflict", "purge_confirmation_one_time"}:
                label = error.args[0]
            # check() receives source-code constants, never an exception message.
            if label is not None:
                result["check"] = label
        print(json.dumps(result, sort_keys=True))
        return 1
    print(json.dumps({"status": "PASS", "service": args.service, "phase": args.phase, "checks": result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
