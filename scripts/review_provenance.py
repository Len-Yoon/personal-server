"""Bind local review receipts to Git objects and recorded Codex contributors.

This checks consistency of operator-supplied local records, not authenticated
identity or completeness of the runtime's logging. It never executes log text.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import uuid
from pathlib import Path


class EvidenceError(ValueError):
    """An evidence source cannot be safely interpreted."""


def _hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _git(repo: Path, *args: str) -> bytes:
    try:
        result = subprocess.run(["git", "--no-pager", "-c", "core.fsmonitor=false",
                                 "-C", str(repo), *args],
                                capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise EvidenceError("review_git_unavailable") from None
    if result.returncode:
        raise EvidenceError("review_git_command_failed")
    return result.stdout


def _path(value: str) -> str:
    if not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise EvidenceError("invalid_review_path")
    p = Path(value)
    if p.is_absolute() or ".." in p.parts or str(p) != value:
        raise EvidenceError("invalid_review_path")
    return value


def _oid(repo: Path, ref: str) -> str:
    if not isinstance(ref, str) or not ref or len(ref) > 1024 or "\0" in ref:
        raise EvidenceError("invalid_review_ref")
    return _git(repo, "rev-parse", "--verify", "--end-of-options", ref + "^{commit}").decode().strip()


def git_target(repo: Path, base: str, head: str) -> tuple[dict, set[str]]:
    """Use raw full object IDs plus SHA256 blob contents, including file modes."""
    if _git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all"):
        raise EvidenceError("review_worktree_not_clean")
    base_oid, head_oid = _oid(repo, base), _oid(repo, head)
    if _oid(repo, "HEAD") != head_oid:
        raise EvidenceError("review_head_not_checked_out")
    _git(repo, "merge-base", "--is-ancestor", base_oid, head_oid)
    raw = _git(repo, "diff", "--raw", "--no-abbrev", "--no-renames", "--no-ext-diff",
               "--no-textconv", "--ignore-submodules=none", "-z", base_oid, head_oid, "--")
    parts = raw.split(b"\0")
    if parts[-1] != b"" or len(parts[:-1]) % 2:
        raise EvidenceError("invalid_review_diff")
    rows = []
    blobs = {}
    for index in range(0, len(parts) - 1, 2):
        fields = parts[index].decode("ascii").lstrip(":").split()
        if len(fields) != 5:
            raise EvidenceError("invalid_review_diff")
        old_mode, new_mode, old_oid, new_oid, status = fields
        if "160000" in (old_mode, new_mode):
            raise EvidenceError("review_submodule_change_unsupported")
        path = _path(parts[index + 1].decode("utf-8"))
        for oid in (old_oid, new_oid):
            if set(oid) != {"0"} and oid not in blobs:
                blobs[oid] = hashlib.sha256(_git(repo, "cat-file", "blob", oid)).hexdigest()
        rows.append([path, old_mode, new_mode, status,
                     blobs.get(old_oid), blobs.get(new_oid)])
    # Preserve contributors whose changes were subsequently reverted, including
    # merge-resolution changes and both sides of renames (no rename collapsing).
    commits = _git(repo, "rev-list", base_oid + ".." + head_oid).decode().splitlines()
    if len(commits) > 1000:
        raise EvidenceError("review_history_too_large")
    history = set()
    for commit in commits:
        names = _git(repo, "diff-tree", "-m", "--root", "--no-commit-id", "-r",
                     "--no-renames", "--name-only", "-z", commit)
        history.update(_path(p.decode("utf-8")) for p in names.split(b"\0") if p)
    if _oid(repo, "HEAD") != head_oid or _git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all"):
        raise EvidenceError("review_target_changed_during_collection")
    scope = sorted(row[0] for row in rows)
    return {"base_commit": base_oid, "head_commit": head_oid, "scope": scope,
            "diff_sha256": _hash(sorted(rows)), "diff_format": "git-raw-blob-sha256-v1"}, history


def _inside(repo: Path, name: str) -> str | None:
    if not isinstance(name, str) or "\0" in name or not Path(name).is_absolute():
        raise EvidenceError("invalid_activity_path")
    # Do not follow a final symlink: its Git path, not its referent, was edited.
    candidate = Path(os.path.abspath(repo / name))
    full = candidate.parent.resolve() / candidate.name
    try:
        relative = full.relative_to(repo.resolve()).as_posix()
    except ValueError:
        return None
    return _path(relative)


def _read_command(command: str) -> bool:
    """Conservative classification only; never execute recorded commands."""
    if any(c in command for c in ("$", "`", ">", "<", "\\", "\0", "(", ")")):
        return False
    try:
        tokens = shlex.shlex(command, posix=True, punctuation_chars=";&|\n")
        tokens.whitespace = " \t\r"
        words = list(tokens)
    except ValueError:
        return False
    groups, current = [], []
    for word in words + [";"]:
        if word and all(c in ";&|\n" for c in word):
            if current:
                groups.append(current)
                current = []
        else:
            current.append(word)
    if not groups:
        return False
    for argv in groups:
        program = argv[0]
        if program in {"cat", "head", "tail", "wc", "ls", "pwd", "true"}:
            continue
        if program == "rg" and not any(a.startswith(("--pre", "--hostname-bin")) for a in argv[1:]):
            continue
        if (program == "sed" and len(argv) >= 3 and argv[1] == "-n"
                and re.fullmatch(r"[0-9,$p; ]+", argv[2])
                and not any(a.startswith("-") for a in argv[3:])):
            continue
        if (program == "git" and len(argv) >= 2 and argv[1] in
                {"status", "diff", "show", "log", "rev-parse", "ls-files", "diff-tree", "cat-file"}
                and not any(a.startswith(("--output", "--ext-diff", "--textconv", "--filters",
                                           "--show-signature")) for a in argv[2:])):
            continue
        return False
    return True


def collect_activity(repo: Path, sessions: list[Path], history: set[str]) -> dict:
    identities, events, written, contributors = {}, {}, set(), set()
    potential = set()
    def record(principal, event_id, value):
        if not isinstance(event_id, str) or not event_id or len(event_id) > 1024:
            raise EvidenceError("review_event_id_invalid")
        key, digest = (principal, event_id), _hash(value)
        if key in events and events[key] != digest:
            raise EvidenceError("review_event_id_reused")
        events[key] = digest
    if not sessions:
        raise EvidenceError("review_sessions_missing")
    for file in sessions:
        principal = None
        pending = {}
        try:
            stream = file.open(encoding="utf-8")
        except OSError:
            raise EvidenceError("review_session_unreadable") from None
        with stream:
            for number, line in enumerate(stream, 1):
                try:
                    item = json.loads(line)
                    payload = item["payload"]
                    if not isinstance(payload, dict):
                        raise ValueError()
                except (ValueError, KeyError, TypeError):
                    raise EvidenceError("review_session_invalid") from None
                if item.get("type") == "session_meta":
                    try:
                        identity = str(uuid.UUID(payload["id"]))
                    except (ValueError, KeyError, TypeError, AttributeError):
                        raise EvidenceError("review_identity_invalid") from None
                    label = payload.get("agent_path", "root")
                    if principal is not None or identity in identities:
                        raise EvidenceError("review_identity_duplicate")
                    principal = identity
                    identities[identity] = label
                    continue
                if principal is None:
                    raise EvidenceError("review_identity_missing")
                if item.get("type") == "response_item":
                    call_id = payload.get("call_id")
                    if payload.get("type") == "custom_tool_call" and payload.get("name") == "exec":
                        if not isinstance(call_id, str) or not call_id:
                            raise EvidenceError("review_event_id_invalid")
                        pending[call_id] = _hash(payload)
                    elif payload.get("type") == "custom_tool_call_output":
                        if isinstance(call_id, str):
                            pending.pop(call_id, None)
                if payload.get("type") in {"item_started", "item_updated"}:
                    started = payload.get("item")
                    if not isinstance(started, dict) or not isinstance(started.get("id"), str):
                        raise EvidenceError("review_event_invalid")
                    if started.get("type") in {"CommandExecution", "FileChange"}:
                        pending[started["id"]] = _hash(started)
                event = payload.get("item", {}) if payload.get("type") == "item_completed" else {}
                if not isinstance(event, dict) or (payload.get("type") == "item_completed"
                                                   and not isinstance(event.get("type"), str)):
                    raise EvidenceError("review_event_invalid")
                if isinstance(event.get("id"), str):
                    pending.pop(event["id"], None)
                if event.get("type") == "FileChange":
                    if event.get("status") not in {"completed", "success"}:
                        potential.add(principal)
                        record(principal, event.get("id"), ["incomplete_write", event])
                        continue
                    changes = event.get("changes")
                    if not isinstance(changes, dict) or not changes:
                        raise EvidenceError("review_file_change_invalid")
                    relevant = []
                    for name, change in changes.items():
                        if not isinstance(change, dict):
                            raise EvidenceError("review_file_change_invalid")
                        if not isinstance(change.get("type"), str) or change["type"] not in {"add", "update", "delete"}:
                            raise EvidenceError("review_file_change_invalid")
                        move = change.get("move_path")
                        if move is not None and (not isinstance(move, str) or not move):
                            raise EvidenceError("review_file_change_invalid")
                        paths = [name]
                        if change.get("move_path"):
                            paths.append(change["move_path"])
                        for path in paths:
                            relative = _inside(repo, path)
                            if relative in history:
                                relevant.append(relative)
                                written.add(relative)
                    if relevant:
                        contributors.add(principal)
                        record(principal, event.get("id"), ["write", sorted(set(relevant)), changes])
                if event.get("type") == "CommandExecution":
                    command = event.get("command")
                    if (isinstance(command, list) and len(command) == 3
                            and command[0] in {"/bin/zsh", "/bin/bash", "/bin/sh"}
                            and command[1] in {"-lc", "-c"}):
                        source = command[2]
                    else:
                        source = None
                    if not isinstance(source, str) or not _read_command(source):
                        potential.add(principal)
                        record(principal, event.get("id"), ["potential_writer", command])
                # Unsupported execution surfaces cannot establish a read-only
                # reviewer. Normal messages, reasoning and usage are not writes.
                neutral = {"UserMessage", "AgentMessage", "Reasoning", "SubAgentActivity",
                           "CollabAgentToolCall", "ContextCompaction", "FileChange", "CommandExecution"}
                if (event and event.get("type") not in neutral) or (
                    item.get("type") == "response_item"
                    and payload.get("type") == "custom_tool_call"
                    and payload.get("name") != "exec"
                ) or (
                    item.get("type") == "response_item"
                    and payload.get("type") == "function_call"
                    and payload.get("name") not in {"spawn_agent", "followup_task", "send_message",
                        "list_agents", "wait_agent", "interrupt_agent", "sleep", "wait",
                        "request_user_input", "request_user_input_async"}
                ):
                    potential.add(principal)
                    record(principal, event.get("id", payload.get("call_id")), ["unsupported_execution", event or payload])
        if principal is None:
            raise EvidenceError("review_identity_missing")
        for event_id, digest in pending.items():
            potential.add(principal)
            record(principal, event_id, ["unfinished_execution", digest])
    return {"session_ids": sorted(identities),
            "contributors": sorted(contributors | potential),
            "potential_writers": sorted(potential),
            "unattributed_paths": sorted(history - written),
            "activity_sha256": _hash([sorted(identities.items()), sorted(events.items())])}


def review_evidence(repo: Path, paths: list[str], base: str, head: str,
                    sessions: list[Path], receipt: Path | None) -> dict:
    repo = repo.resolve()
    target, history = git_target(repo, base, head)
    activity = collect_activity(repo, sessions, history)
    target["activity_sha256"] = activity["activity_sha256"]
    reasons = []
    if set(paths) != set(target["scope"]):
        reasons.append("review_scope_mismatch")
    if activity["unattributed_paths"]:
        reasons.append("review_unattributed_changes")
    result = {"status": "incomplete", "target": target, "activity": activity, "reasons": reasons}
    if receipt is None:
        reasons.append("independent_review_missing")
        return result
    if receipt.resolve().is_relative_to(repo):
        raise EvidenceError("review_receipt_must_be_outside_repository")
    try:
        data = json.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise EvidenceError("review_receipt_invalid") from None
    if not isinstance(data, dict) or set(data) != {"schema_version", "target", "reviewer_session_id", "decision"} or data.get("schema_version") != 1:
        raise EvidenceError("review_receipt_invalid")
    reviewer = data["reviewer_session_id"]
    if not isinstance(reviewer, str) or reviewer not in activity["session_ids"]:
        reasons.append("reviewer_session_unknown")
    elif reviewer in activity["contributors"]:
        reasons.append("reviewer_contributed_to_change")
    if data["target"] != target:
        reasons.append("review_target_stale")
    if not isinstance(data["decision"], str) or data["decision"] not in {"pass", "changes_requested"}:
        raise EvidenceError("review_decision_invalid")
    if data["decision"] != "pass":
        reasons.append("independent_review_failed")
    result["status"] = "passed" if not reasons else "incomplete"
    return result
