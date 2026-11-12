"""Compile a private seven-app capture plan from closed, staged source files."""
import os
from pathlib import Path

from ha_runtime import seven_capture as converter
from ha_runtime.config import SERVICES
from ha_runtime.seven_migration import DATABASES, REQUIRED_DATABASES

from .controller import TransitionBlocked

STATES = {
    "portal-web": {"auth_sessions", "auth_failures", "portfolio"},
    "book-memo": {"auth-rate-limit"},
    "youtube-memo": {"auth-rate-limit"},
    "crawler-worker": {"archive", "collection_status"},
}
EMPTY_STATES = {"portal-web.portfolio", "book-memo.auth-rate-limit", "youtube-memo.auth-rate-limit"}
EVIDENCE = {"runtime", "writer_graph", "writer_closure"}


# Role-ordered PVC roots are fixed by pvc_capture.CLAIMS. Compose paths are
# rooted at the exact stopped container mounts captured by compose_capture.
N100_SOURCES = {
    "portal-web": {
        "databases": {"homeops": "pvc-snapshot/portal-web/v1/homeops.sqlite3",
                      "bookmarks": "pvc-snapshot/portal-web/v1/news-bookmarks.sqlite3"},
        "states": {"auth_sessions": "pvc-snapshot/portal-web/v1/auth/session-state.sqlite3",
                   "auth_failures": "pvc-snapshot/portal-web/v1/auth-rate-limit-state.json",
                   "portfolio": "pvc-snapshot/portal-web/v0/.portfolio/portfolio.md"},
        "files": "pvc-snapshot/portal-web/v0",
    },
    "book-memo": {
        "databases": {"main": "pvc-snapshot/book-memo/v0/book_memo.sqlite3"},
        "states": {"auth-rate-limit": "pvc-snapshot/book-memo/v0/auth-rate-limit-state.json"},
    },
    "youtube-memo": {
        "databases": {"main": "pvc-snapshot/youtube-memo/v0/youtube_memo.sqlite3"},
        "states": {"auth-rate-limit": "pvc-snapshot/youtube-memo/v0/auth-rate-limit-state.json"},
    },
    "crawler-worker": {
        "databases": {"main": "pvc-snapshot/crawler-worker/v0/news_summaries.sqlite3"},
        "states": {"archive": "pvc-snapshot/crawler-worker/v0/news_archive.json",
                   "collection_status": "pvc-snapshot/crawler-worker/v0/news_collection_status.json"},
    },
    "car-care-worker": {
        "databases": {"main": "compose-snapshot/car-care-worker/car-care/car-care.sqlite3"},
        "states": {},
        "private_tokens": "compose-snapshot/car-care-worker/hyundai-token.json",
    },
    "homeops-executor": {
        "databases": {"main": "compose-snapshot/homeops-executor/homeops-executor/restarts.sqlite3"},
        "states": {},
    },
    "system-agent": {"databases": {}, "states": {}},
}


def _require(value, reason):
    if not value:
        raise TransitionBlocked(reason)


def _path(root, relative):
    _require(type(relative) is str and relative and not any(
        part in ("", ".", "..") for part in relative.split("/")) and "\\" not in relative,
        "frozen_source_path_invalid")
    path = root / relative
    _require(path.is_relative_to(root) and all(not parent.is_symlink() for parent in (path, *path.parents)),
             "frozen_source_path_invalid")
    return path


def _file(path, sqlite=False):
    result = converter.descriptor(path)
    if sqlite:
        result["sidecars"] = {
            suffix: converter.descriptor(Path(str(path) + suffix))
            for suffix in sorted(converter.SIDECARS) if os.path.lexists(str(path) + suffix)
        }
    return result


def compile_plan(*, sources_root, private_root, identity, sources, evidence, absences,
                 expected_evidence_pins=None):
    """Bind native closure evidence and exact file descriptors to the converter.

    The capture backend supplies all staged file locations and independently
    established absence observations. This function does not infer closure or
    treat omission from the staging bundle as proof of an original absence.
    """
    source_root = Path(sources_root)
    root = Path(private_root)
    converter.trusted(source_root, directory=True)
    converter.trusted(root, directory=True)
    _require(set(identity) == {"source_commit", "cluster", "plan_sha256", "target_system_id"}
             and set(sources) == set(SERVICES) and set(evidence) == EVIDENCE,
             "frozen_source_scope_invalid")
    _require(type(absences) is dict, "frozen_source_absences_invalid")
    reader = converter.Reader()
    evidence_files = {name: converter.descriptor(Path(path)) for name, path in evidence.items()}
    if expected_evidence_pins is not None:
        _require({name: item["sha256"] for name, item in evidence_files.items()} == expected_evidence_pins,
                 "frozen_source_evidence_pin_changed")
    raw = {name: reader.read(item) for name, item in evidence_files.items()}
    runtime_hash = converter.sha(raw["runtime"])
    graph_hash = converter.sha(raw["writer_graph"])
    closure = converter.parse(raw["writer_closure"])
    _require(type(closure) is dict and closure.get("source_runtime_sha256") == runtime_hash
             and closure.get("writer_graph_sha256") == graph_hash
             and all(closure.get(key) == identity[key] for key in ("cluster", "plan_sha256", "target_system_id")),
             "frozen_source_closure_unbound")

    def descriptor(identifier, relative, *, sqlite=False, optional=False):
        path = _path(source_root, relative)
        if os.path.lexists(path):
            _require(identifier not in absences, "frozen_source_absence_conflict")
            return _file(path, sqlite)
        _require(optional and identifier in absences, "frozen_source_file_missing")
        observation = absences[identifier]
        _require(type(observation) is dict and set(observation) == {
            "source_path_sha256", "inventory_sha256", "checked_at"}
            and observation["source_path_sha256"] == converter.sha(str(path).encode())
            and observation["inventory_sha256"] == runtime_hash
            and converter.utc(closure["writers_closed_at"]) <= converter.utc(observation["checked_at"]) <= converter.utc(converter.now()),
            "frozen_source_absence_unbound")
        _require(not sqlite or not any(os.path.lexists(str(path) + suffix) for suffix in converter.SIDECARS),
                 "frozen_source_absence_conflict")
        used.add(identifier)
        return {"path": str(path), "absence": identifier}

    used = set()
    rows = {}
    for service in sorted(SERVICES):
        row = sources[service]
        extras = {"files"} if service == "portal-web" else {"private_tokens"} if service == "car-care-worker" else set()
        _require(type(row) is dict and set(row) == {"databases", "states"} | extras
                 and set(row["databases"]) == DATABASES[service] and set(row["states"]) == STATES.get(service, set()),
                 "frozen_source_service_invalid")
        result = {
            "databases": {name: descriptor(service + "." + name, relative, sqlite=True,
                                            optional=name not in REQUIRED_DATABASES[service])
                          for name, relative in row["databases"].items()},
            "states": {name: descriptor(service + "." + name, relative,
                                        sqlite=(service, name) == ("portal-web", "auth_sessions"),
                                        optional=service + "." + name in EMPTY_STATES)
                       for name, relative in row["states"].items()},
        }
        if "files" in extras:
            path = _path(source_root, row["files"])
            result["files"] = {"path": str(path), "tree_sha256": converter.inspect_tree(path)}
        if "private_tokens" in extras:
            result["private_tokens"] = descriptor("car-care-worker.private_tokens", row["private_tokens"])
        rows[service] = result
    _require(used == set(absences), "frozen_source_absence_not_used")
    reader.unchanged()
    dispositions = dict(schema_version=1, source_runtime_sha256=runtime_hash, services=sorted(SERVICES),
                        session_dispositions=dict.fromkeys(("book-memo", "youtube-memo"),
                                                           "invalidate_and_require_relogin"),
                        stateless_services=["system-agent"], absences=absences)
    disposition_path = root / "capture-dispositions.json"
    converter._write(disposition_path, converter.encoded(dispositions))
    evidence_files["dispositions"] = converter.descriptor(disposition_path)
    plan = dict(identity, schema_version=1, services=rows, evidence=evidence_files)
    # Reuse converter validation before publishing the plan consumed in capture.
    converter._evidence(converter.Reader(), plan, converter.now())
    target = root / "capture-plan.json"
    receipt = converter._write(target, converter.encoded(plan))
    return target, receipt["sha256"]


def capture_closed_sources(*, pvc, compose, sources_root, private_root, output,
                           identity, evidence, assert_writers_closed, expected_evidence_pins=None):
    """Collect both native storage providers and normalize under one closed writer state."""
    source_root = Path(sources_root)
    pvc_roots = pvc.collect(source_root, assert_writers_closed)
    compose_roots = compose.collect(source_root, assert_writers_closed)
    captured = pvc_roots | compose_roots
    _require(set(captured) == set(SERVICES) - {"system-agent"}, "frozen_source_mounts_unconfirmed")
    assert_writers_closed()
    runtime = converter.descriptor(Path(evidence["runtime"]))
    absences = {}
    for service, row in N100_SOURCES.items():
        selections = []
        for name, relative in row["databases"].items():
            selections.append((name, relative, name not in REQUIRED_DATABASES[service], True))
        for name, relative in row["states"].items():
            selections.append((name, relative, service + "." + name in EMPTY_STATES, False))
        for key in ("files", "private_tokens"):
            if key in row:
                selections.append((key, row[key], False, False))
        for name, relative, optional, sqlite in selections:
            path = _path(source_root, relative)
            _require(service in captured and path.is_relative_to(captured[service]),
                     "frozen_source_outside_native_capture")
            if optional and not os.path.lexists(path):
                _require(not sqlite or not any(os.path.lexists(str(path) + suffix) for suffix in converter.SIDECARS),
                         "frozen_source_absence_conflict")
                absences[service + "." + name] = {
                    "source_path_sha256": converter.sha(str(path).encode()),
                    "inventory_sha256": runtime["sha256"],
                    "checked_at": converter.now(),
                }
    plan, digest = compile_plan(sources_root=source_root, private_root=private_root, identity=identity,
                                sources=N100_SOURCES, evidence=evidence, absences=absences,
                                expected_evidence_pins=expected_evidence_pins)
    result = converter.capture(plan, digest, output)
    pvc.assert_settled()
    assert_writers_closed()
    return result
