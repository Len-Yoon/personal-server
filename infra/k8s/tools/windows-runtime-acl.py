#!/usr/bin/env python3
"""Reviewed, bounded Windows DACL transaction; never persists/prints credentials.

Run this exact CI-reviewed source through a trusted ``python3 -I`` stdin launcher.
It imports no repository code. The Windows adapter keeps no-share-delete handles
open for the whole transaction. Plans and journals contain DPAPI CurrentUser
ciphertext only; stdout contains fixed reasons, counts and hashes only.

Native fixture verification is a mandatory external gate, not a claim made by
unit tests. SACL is deliberately not requested: DACL-only SetSecurityInfo cannot
change it. No privilege is enabled, no owner/group/content setter exists.
"""

from __future__ import annotations

import argparse
import base64
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import select
import stat
import subprocess
import sys
import time
import zlib
from contextlib import contextmanager
from datetime import datetime, timezone


SCHEMA = 1
RUNTIME = (".env", "docker-compose.yml", "docker-compose.n100.yml",
           "docker-compose.portal-bridge.yml")
EXPECTED_COUNTS = {"total": 6669, "files": 5813, "directories": 856,
                   "git_unsafe": 18, "tracked_unsafe": 0,
                   "ignored_unsafe": 315, "other_owner": 69}
SAFE_REASON = re.compile(r"^[a-z][a-z0-9_]{0,80}$")
POWERSHELL_BINARY = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"


class Blocked(Exception):
    def __init__(self, reason):
        if not SAFE_REASON.fullmatch(reason):
            reason = "invalid_error_reason"
        self.reason = reason
        super().__init__(reason)


class UnknownWrite(Blocked):
    pass


def require(condition, reason):
    if not condition:
        raise Blocked(reason)


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("ascii")


def digest(value):
    return hashlib.sha256(value).hexdigest()


def object_hash(value):
    return digest(encoded(value))


def relative_path(value):
    require(isinstance(value, str) and value and "\\" not in value
            and "\0" not in value and ":" not in value
            and all(p not in ("", ".", "..") for p in value.split("/")),
            "invalid_relative_path")
    return value


def validate_scope(scope):
    require(set(scope) == {"schema", "root", "tracked_paths", "counts",
                           "source_sha256", "fixture_sha256"}, "invalid_scope")
    require(scope["schema"] == SCHEMA and scope["root"] == "C:\\personal-server",
            "unapproved_root")
    require(scope["counts"] == EXPECTED_COUNTS, "unapproved_counts")
    for key in ("source_sha256", "fixture_sha256"):
        require(isinstance(scope[key], str)
                and re.fullmatch(r"[0-9a-f]{64}", scope[key]), "missing_source_or_fixture_pin")
    paths = scope["tracked_paths"]
    require(isinstance(paths, list) and paths and len(paths) == len(set(paths)),
            "invalid_tracked_inventory")
    for path in paths:
        relative_path(path)
        require(not path.startswith(".git/") and path != ".git", "invalid_tracked_inventory")
    require(len({p.casefold() for p in paths}) == len(paths), "case_alias_inventory")
    return scope


def validate_snapshot(snapshot, scope):
    require(set(snapshot) == {"entries", "counts", "runtime_hashes", "principal",
                             "projection_contract"}, "invalid_snapshot")
    require(snapshot["counts"] == scope["counts"], "scope_counts_changed")
    require(snapshot["projection_contract"] == "win32_private_object_security_v1",
            "unsupported_projection")
    require(re.fullmatch(r"S-1-(?:\d+-)*\d+", snapshot["principal"] or ""),
            "invalid_principal")
    entries = snapshot["entries"]
    require(isinstance(entries, dict) and "" in entries
            and len(entries) == scope["counts"]["total"], "invalid_tree_inventory")
    identities = set()
    for path, entry in entries.items():
        if path:
            relative_path(path)
            require(path.rpartition("/")[0] in entries, "missing_parent")
        require(set(entry) == {"identity", "kind", "attributes", "created", "modified",
                               "size", "links", "owner", "group", "dacl", "explicit",
                               "protected", "sd", "unsafe"}, "invalid_entry")
        require(entry["kind"] in ("file", "directory") and not entry["attributes"] & 1024,
                "reparse_point_forbidden")
        require(entry["identity"] not in identities, "hardlink_or_identity_alias")
        identities.add(entry["identity"])
        require(entry["links"] == 1 or entry["kind"] == "directory", "hardlink_forbidden")
    require(set(snapshot["runtime_hashes"]) == set(RUNTIME), "runtime_inventory_changed")
    for path in RUNTIME:
        require(path in entries and entries[path]["kind"] == "file", "runtime_file_missing")
        require(re.fullmatch(r"[0-9a-f]{64}", snapshot["runtime_hashes"][path] or ""),
                "invalid_runtime_hash")
    require(all(path in entries for path in scope["tracked_paths"]), "tracked_file_missing")
    git_targets = sorted(path for path, entry in entries.items()
                         if (path == ".git" or path.startswith(".git/")) and entry["unsafe"])
    require(len(git_targets) == 18 and ".git" in git_targets
            and all(entries[path]["kind"] == "directory" for path in git_targets),
            "git_directory_inventory_changed")
    targets = [""] + list(RUNTIME) + git_targets
    require(len(targets) == 23 and len(set(targets)) == 23, "target_inventory_changed")
    return targets


def security_projection(snapshot):
    """Compare metadata, identity, owner/group, DACL ACE bytes and protection.

    SD auto-inheritance/defaulted control bits and unreadable SACL bytes are not
    independently compared. DACL-only native flags preserve owner/group/SACL.
    """
    result = copy.deepcopy(snapshot)
    result.pop("counts")  # initial scope classification changes as DACLs change
    for entry in result["entries"].values():
        entry.pop("sd")
    return result


def same_snapshot(actual, expected):
    return security_projection(actual) == security_projection(expected)


def snapshot_hash(snapshot):
    return object_hash(security_projection(snapshot))


def delta(before, after):
    return {path: entry for path, entry in after["entries"].items()
            if before["entries"][path] != entry}


def with_delta(snapshot, changes):
    result = copy.deepcopy(snapshot)
    require(set(changes) <= set(result["entries"]), "delta_inventory_changed")
    result["entries"].update(copy.deepcopy(changes))
    return result


def materialize(plan, completed):
    require(0 <= completed <= len(plan["steps"]), "invalid_completed_count")
    result = copy.deepcopy(plan["initial"])
    for step in plan["steps"][:completed]:
        require(snapshot_hash(result) == step["before_sha256"], "plan_delta_mismatch")
        result = with_delta(result, step["changes"])
        require(snapshot_hash(result) == step["after_sha256"], "plan_delta_mismatch")
    return result


def own_partial(actual, before, after):
    """Whole object ACLs may equal only the durable before/own-after values."""
    if set(actual["entries"]) != set(before["entries"]) or not same_snapshot(
            {**actual, "entries": before["entries"]}, before):
        return False
    strip = lambda row: {key: value for key, value in row.items() if key != "sd"}
    return all(strip(entry) in (strip(before["entries"][path]), strip(after["entries"][path]))
               for path, entry in actual["entries"].items())


def validate_projection(before, after, target):
    require(set(before["entries"]) == set(after["entries"]), "projection_inventory_changed")
    require(before["runtime_hashes"] == after["runtime_hashes"]
            and before["principal"] == after["principal"], "projection_content_changed")
    for path, old in before["entries"].items():
        new = after["entries"][path]
        for field in ("identity", "kind", "attributes", "created", "modified", "size",
                      "links", "owner", "group"):
            require(old[field] == new[field], "projection_metadata_changed")
        if path != target:
            require(old["explicit"] == new["explicit"]
                    and old["protected"] == new["protected"], "unapproved_explicit_change")
    # Counts are baseline classifications, not recomputed after correction.
    require(before["counts"] == after["counts"], "projection_counts_changed")


def private_directory(path):
    """Open each ancestor without following links; reject writable parents."""
    path = Path(path)
    require(path.is_absolute() and ".." not in path.parts, "absolute_private_path_required")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
            info = os.fstat(fd)
            require(info.st_uid in (0, os.getuid()) and not info.st_mode & 0o022,
                    "unsafe_private_ancestor")
        return fd
    except BaseException:
        os.close(fd)
        raise


@contextmanager
def sequence_lock():
    """The existing backup-sequence lock, without importing untrusted repo code."""
    descriptors = []
    try:
        fd = private_directory(Path.home())
        descriptors.append(fd)
        for component in (".local", "state", "personal-server"):
            parent = fd
            try:
                os.mkdir(component, 0o700, dir_fd=parent)
            except FileExistsError:
                pass
            fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            descriptors.append(fd)
            info = os.fstat(fd)
            require(info.st_uid == os.getuid() and not info.st_mode & 0o022,
                    "unsafe_lock_directory")
        lock = os.open("pvc-backup-sequence.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                       0o600, dir_fd=fd)
        descriptors.append(lock)
        info = os.fstat(lock)
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                and info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o600,
                "unsafe_sequence_lock")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Blocked("sequence_lock_busy") from None
        yield lock
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


class Vault:
    """No plaintext state reaches disk. Atomic replacement retains prior intent."""
    def __init__(self, path, backend):
        self.backend = backend
        self.path = Path(path)
        require(self.path.is_absolute(), "absolute_vault_required")
        require(not str(self.path).startswith(("/mnt/", "/media/")), "linux_private_vault_required")
        parent = private_directory(self.path.parent)
        try:
            try:
                os.mkdir(self.path.name, 0o700, dir_fd=parent)
            except FileExistsError:
                pass
            self.fd = os.open(self.path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                              dir_fd=parent)
        finally:
            os.close(parent)
        info = os.fstat(self.fd)
        require(info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700,
                "unsafe_vault_directory")

    def close(self):
        os.close(self.fd)

    def exists(self, name):
        try:
            os.stat(name, dir_fd=self.fd, follow_symlinks=False)
            return True
        except FileNotFoundError:
            return False

    def read_bytes(self, name):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self.fd)
        try:
            info = os.fstat(fd)
            require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                    and info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o600,
                    "unsafe_vault_file")
            require(info.st_size <= 64 * 1024 * 1024, "vault_size_limit")
            parts = []
            while chunk := os.read(fd, 1024 * 1024):
                parts.append(chunk)
            return b"".join(parts)
        finally:
            os.close(fd)

    def read(self, name):
        return json.loads(zlib.decompress(self.backend.decrypt(self.read_bytes(name))).decode("ascii"))

    def write(self, name, value, exclusive=False):
        ciphertext = self.backend.encrypt(zlib.compress(encoded(value), level=6))
        require(isinstance(ciphertext, bytes) and ciphertext, "encryption_failed")
        if exclusive:
            require(not self.exists(name), "vault_already_prepared")
        temporary = ".pending-" + os.urandom(16).hex()
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=self.fd)
        try:
            view = memoryview(ciphertext)
            while view:
                written = os.write(fd, view)
                require(written > 0, "journal_short_write")
                view = view[written:]
            os.fsync(fd)
        except BaseException:
            os.close(fd)
            os.unlink(temporary, dir_fd=self.fd)
            raise
        else:
            os.close(fd)
        if exclusive:
            # link() is an atomic no-replace publication, even under a hostile race.
            os.link(temporary, name, src_dir_fd=self.fd, dst_dir_fd=self.fd,
                    follow_symlinks=False)
            os.unlink(temporary, dir_fd=self.fd)
        else:
            os.replace(temporary, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
        os.fsync(self.fd)
        return digest(ciphertext)


class Transaction:
    def __init__(self, backend, vault):
        self.backend, self.vault = backend, vault
        self.backend.on_held = self.session_held

    def session_held(self, reason):
        if self.vault.exists("state.dpapi"):
            state = self.vault.read("state.dpapi")
            state.update(phase="held", unknown=True)
            state["failures"][reason] = state["failures"].get(reason, 0) + 1
            self.save(state)

    def prepare(self, scope):
        validate_scope(scope)
        require(self.backend.lock_owned, "sequence_lock_required")
        require(not self.vault.exists("plan.dpapi") and not self.vault.exists("state.dpapi"),
                "vault_already_prepared")
        self.backend.open(scope)
        ancestor_sha = self.backend.ancestor_sha256
        require(re.fullmatch(r"[0-9a-f]{64}", ancestor_sha or ""), "ancestor_guard_missing")
        initial = self.backend.snapshot()
        targets = validate_snapshot(initial, scope)
        steps, current = [], initial
        for target in targets:
            projected = self.backend.project(current, target, None)
            validate_projection(current, projected, target)
            steps.append({"target": target, "before_sha256": snapshot_hash(current),
                          "after_sha256": snapshot_hash(projected), "changes": delta(current, projected)})
            current = projected
        require(same_snapshot(self.backend.snapshot(), initial), "prepare_drift")
        plan = {"schema": SCHEMA, "scope": scope, "initial": initial,
                "ancestor_sha256": ancestor_sha,
                "steps": steps, "final_sha256": snapshot_hash(current)}
        plan_sha = self.vault.write("plan.dpapi", plan, exclusive=True)
        self.vault.write("state.dpapi", {"schema": SCHEMA, "plan_sha256": plan_sha,
                         "phase": "prepared", "completed": 0, "pending": None,
                         "unknown": False, "failures": {}} , exclusive=True)
        return {"reason": "prepared", "plan_sha256": plan_sha, "targets": len(targets),
                "total": scope["counts"]["total"]}

    def load(self, reviewed_sha):
        require(re.fullmatch(r"[0-9a-f]{64}", reviewed_sha or ""), "reviewed_plan_required")
        require(digest(self.vault.read_bytes("plan.dpapi")) == reviewed_sha, "plan_pin_mismatch")
        plan, state = self.vault.read("plan.dpapi"), self.vault.read("state.dpapi")
        require(plan["schema"] == SCHEMA and state["schema"] == SCHEMA
                and state["plan_sha256"] == reviewed_sha, "journal_plan_mismatch")
        validate_scope(plan["scope"])
        targets = validate_snapshot(plan["initial"], plan["scope"])
        require(self.backend.lock_owned, "sequence_lock_required")
        self.backend.recovery_targets = targets
        self.backend.open(plan["scope"])
        require(re.fullmatch(r"[0-9a-f]{64}", plan.get("ancestor_sha256", ""))
                and self.backend.ancestor_sha256 == plan["ancestor_sha256"],
                "ancestor_pin_mismatch")
        return plan, state

    def save(self, state):
        self.vault.write("state.dpapi", state)

    def hold(self, state, reason, unknown=False):
        state["phase"] = "held"
        state["unknown"] = state["unknown"] or unknown
        state["failures"][reason] = state["failures"].get(reason, 0) + 1
        self.save(state)
        raise Blocked(reason)

    def apply(self, reviewed_sha):
        plan, state = self.load(reviewed_sha)
        require(state["phase"] == "prepared" and state["completed"] == 0
                and state["pending"] is None and not state["unknown"], "unfinished_transaction")
        require(self.backend.mutation_authorized, "native_fixture_and_guard_required")
        if not same_snapshot(self.backend.snapshot(), plan["initial"]):
            self.hold(state, "preapply_drift")
        for index, step in enumerate(plan["steps"]):
            before = materialize(plan, index)
            after = with_delta(before, step["changes"])
            require(all(count < 3 for count in state["failures"].values()), "failure_limit")
            state.update(phase="applying", pending={"direction": "apply", "index": index})
            self.save(state)  # encrypted original + durable intent precede first setter
            try:
                require(same_snapshot(self.backend.stable_snapshot(), before),
                        "prewrite_drift")
                self.backend.set_dacl(step["target"], after["entries"][step["target"]])
                state["pending"]["response_complete"] = True
                self.save(state)
                actual = self.backend.stable_snapshot()
            except UnknownWrite:
                self.hold(state, "native_response_unknown", unknown=True)
            except Blocked as exc:
                state["pending"]["response_complete"] = True
                self.hold(state, exc.reason)
            if not same_snapshot(actual, after):
                self.hold(state, "propagation_or_metadata_drift")
            state.update(completed=index + 1, pending=None)
            self.save(state)
        state.update(phase="applied")
        self.save(state)
        return {"reason": "applied", "targets": state["completed"], "plan_sha256": reviewed_sha}

    def status(self, reviewed_sha):
        plan, state = self.load(reviewed_sha)
        actual = self.backend.stable_snapshot()
        matched = "drift"
        if same_snapshot(actual, plan["initial"]):
            matched = "initial"
        for index, step in enumerate(plan["steps"]):
            if snapshot_hash(actual) == step["after_sha256"]:
                matched = "step_" + str(index + 1)
        # Status never clears intent/unknown, even when a readback appears complete.
        return {"reason": state["phase"], "matched": matched,
                "unknown": state["unknown"], "targets": state["completed"],
                "plan_sha256": reviewed_sha}

    def rollback(self, reviewed_sha):
        plan, state = self.load(reviewed_sha)
        require(self.backend.mutation_authorized, "native_fixture_and_guard_required")
        require(not state["unknown"] or self.backend.recovery_quiescent,
                "unknown_native_process_held")
        require(state["phase"] in ("applied", "applying", "held", "rolling_back"),
                "rollback_not_applicable")
        require(all(count < 3 for count in state["failures"].values()), "failure_limit")
        actual = self.backend.stable_snapshot()
        completed = state["completed"]
        recovered_partial = False
        if state["pending"] is not None:
            pending = state["pending"]
            # Missing response after crash is not proof that a Windows setter ended.
            require(pending.get("response_complete") is True or self.backend.recovery_quiescent,
                    "unresolved_intent_held")
            index = pending["index"]
            step = plan["steps"][index]
            before = materialize(plan, index)
            after = with_delta(before, step["changes"])
            if pending["direction"] == "apply":
                if same_snapshot(actual, after):
                    completed = index + 1
                elif same_snapshot(actual, before):
                    completed = index
                elif own_partial(actual, before, after):
                    completed = index + 1
                    recovered_partial = True
                else:
                    self.hold(state, "partial_or_foreign_state")
            else:
                before = materialize(plan, index + 1)
                expected = with_delta(before, pending["changes"])
                if same_snapshot(actual, expected):
                    completed = index
                elif own_partial(actual, before, expected):
                    completed = index + 1
                    recovered_partial = True
                else:
                    self.hold(state, "partial_or_foreign_state")
            # Keep the durable pending provenance until the inverse intent is
            # fsynced below. A mixed state cannot be reconstructed from only
            # `completed` after a controller crash between those checkpoints.
            state.update(completed=completed)
            state["unknown"] = False
        expected_current = materialize(plan, completed)
        if not recovered_partial and not same_snapshot(actual, expected_current):
            self.hold(state, "rollback_ownership_mismatch")
        for index in range(completed - 1, -1, -1):
            step = plan["steps"][index]
            # The original target DACL is restored by handle; inheritance effects
            # are projected against the current parent, not assumed from old bytes.
            expected = self.backend.project(actual, step["target"],
                                             plan["initial"]["entries"][step["target"]])
            validate_projection(actual, expected, step["target"])
            state.update(phase="rolling_back", pending={"direction": "rollback", "index": index,
                                                        "before_sha256": snapshot_hash(actual),
                                                        "after_sha256": snapshot_hash(expected),
                                                        "changes": delta(expected_current, expected)})
            self.save(state)
            try:
                self.backend.set_dacl(step["target"], expected["entries"][step["target"]])
                state["pending"]["response_complete"] = True
                self.save(state)
                actual = self.backend.stable_snapshot()
            except UnknownWrite:
                self.hold(state, "rollback_response_unknown", unknown=True)
            except Blocked as exc:
                state["pending"]["response_complete"] = True
                self.hold(state, exc.reason)
            if not same_snapshot(actual, expected):
                if not own_partial(actual, expected_current, expected):
                    self.hold(state, "rollback_propagation_drift")
                # Known, completed root propagation may leave whole child ACLs
                # in durable before/own-after states. Repair only those objects,
                # with WRITE_DAC handles retained before the first root change.
                for path in sorted(actual["entries"], key=lambda p: (p.count("/"), len(p), p)):
                    if same_snapshot({**actual, "entries": {path: actual["entries"][path]}},
                                     {**expected, "entries": {path: expected["entries"][path]}}):
                        continue
                    state["pending"].update(direction="repair", repair_target=path,
                                              response_complete=False)
                    self.save(state)
                    try:
                        self.backend.set_dacl(path, expected["entries"][path])
                        state["pending"]["response_complete"] = True
                        self.save(state)
                        actual = self.backend.stable_snapshot()
                    except UnknownWrite:
                        self.hold(state, "rollback_response_unknown", unknown=True)
                    except Blocked as exc:
                        state["pending"]["response_complete"] = True
                        self.hold(state, exc.reason)
                    if not own_partial(actual, expected_current, expected):
                        self.hold(state, "rollback_repair_foreign_state")
                if not same_snapshot(actual, expected):
                    self.hold(state, "rollback_repair_incomplete")
            state.update(completed=index, pending=None)
            self.save(state)
            expected_current = materialize(plan, index)
        if not same_snapshot(actual, plan["initial"]):
            self.hold(state, "rollback_not_exact")
        state.update(phase="rolled_back", unknown=False, pending=None)
        self.save(state)
        return {"reason": "rolled_back", "targets": completed, "plan_sha256": reviewed_sha}


# Native implementation follows below. No user-controlled PowerShell is evaluated.
POWERSHELL = r'''
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
Add-Type -AssemblyName System.Security
Add-Type -AssemblyName System.Web.Extensions
Add-Type -ReferencedAssemblies @('System.dll','System.Core.dll','System.Security.dll') -TypeDefinition @'
using System;
using System.IO;
using System.Linq;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Security.Cryptography;
using Microsoft.Win32.SafeHandles;
public static class BoundAcl {
 public static string FixtureStage="setup";
 public static string AncestorSHA {get;private set;}
 static List<string> ancestorPins=new List<string>();
 [StructLayout(LayoutKind.Sequential)] struct Info {
  public uint Attr; public uint CL,CH,AL,AH,ML,MH,Volume,SizeH,SizeL,Links,IndexH,IndexL;
 }
 [StructLayout(LayoutKind.Sequential)] struct Mapping { public uint Read,Write,Execute,All; }
 [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)]
 static extern SafeFileHandle CreateFile(string p,uint access,uint share,IntPtr sa,uint creation,uint flags,IntPtr t);
 [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern bool CreateDirectory(string p,IntPtr security);
 [DllImport("kernel32.dll",SetLastError=true)] static extern bool WriteFile(SafeFileHandle h,byte[] bytes,uint length,out uint written,IntPtr overlapped);
 [DllImport("kernel32.dll",SetLastError=true)] static extern bool FlushFileBuffers(SafeFileHandle h);
 [DllImport("kernel32.dll",SetLastError=true)] static extern bool GetFileInformationByHandle(SafeFileHandle h,out Info i);
 [DllImport("advapi32.dll")] static extern uint GetSecurityInfo(SafeFileHandle h,int type,uint flags,out IntPtr owner,out IntPtr group,out IntPtr dacl,out IntPtr sacl,out IntPtr sd);
 [DllImport("advapi32.dll")] static extern uint SetSecurityInfo(SafeFileHandle h,int type,uint flags,IntPtr owner,IntPtr group,IntPtr dacl,IntPtr sacl);
 [DllImport("advapi32.dll")] static extern uint GetSecurityDescriptorLength(IntPtr p);
 [DllImport("kernel32.dll")] static extern IntPtr LocalFree(IntPtr p);
 [DllImport("kernel32.dll",SetLastError=true)] static extern bool SetFileInformationByHandle(SafeFileHandle h,int kind,ref int data,uint length);
 [DllImport("advapi32.dll",SetLastError=true)] static extern bool CreatePrivateObjectSecurityEx(byte[] parent,byte[] creator,out IntPtr result,IntPtr guid,bool directory,uint flags,IntPtr token,ref Mapping mapping);
 [DllImport("advapi32.dll")] static extern bool DestroyPrivateObjectSecurity(ref IntPtr p);
 static Dictionary<string,SafeFileHandle> handles = new Dictionary<string,SafeFileHandle>(StringComparer.OrdinalIgnoreCase);
 static List<SafeFileHandle> ancestors = new List<SafeFileHandle>();
 static string root, principal;
 static HashSet<string> tracked, runtime;
 public static string Principal {get {return principal;}}
 static void Check(bool value) {if(!value) throw new Exception("native_acl_failure");}
 static string NativePath(string p) {return @"\\?\"+p;}
 static string Full(string relative) {return relative.Length==0 ? root : Path.Combine(root,relative.Replace('/','\\'));}
 static SafeFileHandle Open(string p,bool write) {
  // Explicit rights, never MAXIMUM_ALLOWED. Both read/write sharing permit
  // Windows inheritance propagation; omitting DELETE blocks replacement.
  var h=CreateFile(NativePath(p),0x00020080u | (write?0x00040000u:0),3,IntPtr.Zero,3,0x02200000,IntPtr.Zero);
  Check(!h.IsInvalid); Info i; Check(GetFileInformationByHandle(h,out i));
  Check((i.Attr&1024)==0); return h;
 }
 static string[] Inventory() {
  var all=new List<string>{""}; var pending=new Queue<string>(); pending.Enqueue("");
  while(pending.Count>0) {
   var parent=pending.Dequeue();
   foreach(var p in Directory.EnumerateFileSystemEntries(Full(parent))) {
    var relative=p.Substring(root.Length+1).Replace('\\','/');
    Check(handles.ContainsKey(relative));
    using(var fresh=Open(p,false))Check(Identity(fresh)==Identity(handles[relative]));
    Info info;Check(GetFileInformationByHandle(handles[relative],out info)&&(info.Attr&1024)==0);
    all.Add(relative); if((info.Attr&16)!=0) pending.Enqueue(relative);
   }
  }
  return all.OrderBy(p=>p.Count(c=>c=='/')).ThenBy(p=>p,StringComparer.Ordinal).ToArray();
 }
 static bool AncestorTrusted(string sid) {return Trusted(sid)||sid=="S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464";}
 static string FixtureParent() {
  string path=Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
  Check(!String.IsNullOrEmpty(path)&&Path.IsPathRooted(path));return Path.GetFullPath(path).TrimEnd('\\');
 }
 public static Dictionary<string,object> TempCheck() {return ParentCheck(Path.GetTempPath());}
 public static Dictionary<string,object> FixtureParentCheck() {return ParentCheck(FixtureParent());}
 static Dictionary<string,object> ParentCheck(string parent) {
  principal=WindowsIdentity.GetCurrent().User.Value;
  string temp=Path.GetFullPath(parent).TrimEnd('\\');
  bool canonical=temp.StartsWith(@"C:\",StringComparison.OrdinalIgnoreCase)&&!temp.StartsWith(@"C:\personal-server",StringComparison.OrdinalIgnoreCase);
  var rows=new List<Dictionary<string,object>>();int index=0;string ancestor=temp;
  if(!canonical)return new Dictionary<string,object>{{"reason","native_temp_canonical_blocked"},{"ancestors",rows}};
  while(!String.IsNullOrEmpty(ancestor)) {
   var row=new Dictionary<string,object>{{"index",index},{"opened",false},{"owner_trusted",false},{"acl_supported",false},{"dangerous_allow",0}};
   try {
    using(var h=Open(ancestor,false)) {
     row["opened"]=true;var sd=Descriptor(h);row["owner_trusted"]=AncestorTrusted(sd.Owner.Value);
     bool supported=sd.DiscretionaryAcl!=null;int unsafeCount=0;
     if(supported)foreach(GenericAce ace in sd.DiscretionaryAcl) {
      var common=ace as CommonAce;if(common==null||common.IsCallback){supported=false;continue;}
      if(common.AceQualifier==AceQualifier.AccessAllowed&&(common.AceFlags&AceFlags.InheritOnly)==0&&!AncestorTrusted(common.SecurityIdentifier.Value)&&(common.AccessMask&0xd0040)!=0)unsafeCount++;
     }
     row["acl_supported"]=supported;row["dangerous_allow"]=unsafeCount;
    }
   }catch{rows.Add(row);return new Dictionary<string,object>{{"reason","native_temp_ancestor_blocked"},{"ancestors",rows}};}
   rows.Add(row);index++;string next=Path.GetDirectoryName(ancestor);if(next==ancestor)break;ancestor=next;
  }
  return new Dictionary<string,object>{{"reason","native_temp_checked"},{"ancestors",rows}};
 }
 static string AncestorPin(SafeFileHandle h) {
  var sd=Descriptor(h);Check(AncestorTrusted(sd.Owner.Value)&&sd.DiscretionaryAcl!=null);
  foreach(GenericAce ace in sd.DiscretionaryAcl) {
   var common=ace as CommonAce;Check(common!=null&&!common.IsCallback);
   Check(common.AceQualifier!=AceQualifier.AccessAllowed||(common.AceFlags&AceFlags.InheritOnly)!=0||AncestorTrusted(common.SecurityIdentifier.Value)||(common.AccessMask&0xd0040)==0);
  }
  return Identity(h)+":"+HexHash(Bytes(sd));
 }
 static void VerifyAncestors() {for(int i=0;i<ancestors.Count;i++)Check(AncestorPin(ancestors[i])==ancestorPins[i]);}
 public static void Start(string r,string[] t,string[] rt,string[] writeTargets) {
  Check(handles.Count==0); root=r.TrimEnd('\\'); tracked=new HashSet<string>(t,StringComparer.OrdinalIgnoreCase);
  runtime=new HashSet<string>(rt,StringComparer.OrdinalIgnoreCase);
  principal=WindowsIdentity.GetCurrent().User.Value;
  var parent=Path.GetDirectoryName(root);
  while(!String.IsNullOrEmpty(parent)) {var h=Open(parent,false);ancestors.Add(h);ancestorPins.Add(AncestorPin(h)); var next=Path.GetDirectoryName(parent); if(next==parent)break; parent=next;}
  AncestorSHA=HexHash(System.Text.Encoding.UTF8.GetBytes(String.Join("|",ancestorPins)));
  // Hold every object before classifying targets. WRITE_DAC is requested only
  // for root/runtime/Git explicit unsafe targets, by reopening after identity pin.
  handles.Add("",Open(root,false));
  var pending=new Queue<string>();pending.Enqueue("");
  while(pending.Count>0) {
   string parentPath=pending.Dequeue();
   foreach(var path in Directory.EnumerateFileSystemEntries(Full(parentPath))) {
    string relative=path.Substring(root.Length+1).Replace('\\','/');
    var handle=Open(path,false); // OPEN_REPARSE_POINT and no DELETE before traversal
    handles.Add(relative,handle);Info info;Check(GetFileInformationByHandle(handle,out info));
    if((info.Attr&16)!=0)pending.Enqueue(relative);
   }
  }
  var initial=Rows();
  foreach(var p in handles.Keys.ToArray()) {
   var row=initial[p]; bool target=p=="" || runtime.Contains(p) || writeTargets.Contains(p) || ((p==".git"||p.StartsWith(".git/",StringComparison.OrdinalIgnoreCase))&&(bool)row["unsafe"]);
   if(target || !(bool)row["protected"]) {
    var replacement=Open(Full(p),true); var old=Identity(handles[p]);
    Check(Identity(replacement)==old); handles[p].Dispose(); handles[p]=replacement;
   }
  }
  Check(Inventory().SequenceEqual(handles.Keys.OrderBy(p=>p.Count(c=>c=='/')).ThenBy(p=>p,StringComparer.Ordinal)));
 }
 static string Identity(SafeFileHandle h) {Info i;Check(GetFileInformationByHandle(h,out i));return i.Volume.ToString("x8")+":"+i.IndexH.ToString("x8")+i.IndexL.ToString("x8");}
 static byte[] Bytes(GenericAcl a) {var b=new byte[a.BinaryLength];a.GetBinaryForm(b,0);return b;}
 static byte[] Bytes(RawSecurityDescriptor a) {var b=new byte[a.BinaryLength];a.GetBinaryForm(b,0);return b;}
 static RawSecurityDescriptor Descriptor(SafeFileHandle h) {
  IntPtr o,g,d,s,p; Check(GetSecurityInfo(h,1,7,out o,out g,out d,out s,out p)==0);
  try {var b=new byte[GetSecurityDescriptorLength(p)];Marshal.Copy(p,b,0,b.Length);return new RawSecurityDescriptor(b,0);}
  finally {LocalFree(p);}
 }
 static bool Trusted(string sid) {return sid==principal||sid=="S-1-5-18"||sid=="S-1-5-32-544"||sid=="S-1-3-0";}
 static string[] Explicit(RawAcl acl) {
  return acl.Cast<GenericAce>().Where(a=>(a.AceFlags&AceFlags.Inherited)==0).Select(a=>{var b=new byte[a.BinaryLength];a.GetBinaryForm(b,0);return Convert.ToBase64String(b);}).ToArray();
 }
 static bool Unsafe(RawAcl acl) {
  Check(acl!=null);bool unsafeEntry=false;
  foreach(GenericAce a in acl) {
   // Reject object/callback/conditional ACLs instead of guessing inheritance.
   var c=a as CommonAce;Check(c!=null&&!c.IsCallback);
   if(c.AceQualifier==AceQualifier.AccessAllowed&&(c.AceFlags&(AceFlags.Inherited|AceFlags.InheritOnly))==0
       && !Trusted(c.SecurityIdentifier.Value) && (c.AccessMask&852310)!=0) unsafeEntry=true;
  }
  return unsafeEntry;
 }
 static Dictionary<string,object> Row(string p,SafeFileHandle h) {
  Info i;Check(GetFileInformationByHandle(h,out i));Check((i.Attr&1024)==0);
  var sd=Descriptor(h);Check(sd.DiscretionaryAcl!=null); bool bad=Unsafe(sd.DiscretionaryAcl);
  return new Dictionary<string,object>{{"identity",Identity(h)},{"kind",(i.Attr&16)!=0?"directory":"file"},
   {"attributes",i.Attr},{"created",((ulong)i.CH<<32|i.CL).ToString()}, {"modified",((ulong)i.MH<<32|i.ML).ToString()},
   {"size",((ulong)i.SizeH<<32|i.SizeL).ToString()},{"links",i.Links},
   {"owner",sd.Owner.Value},{"group",sd.Group.Value},{"dacl",Convert.ToBase64String(Bytes(sd.DiscretionaryAcl))},
   {"explicit",Explicit(sd.DiscretionaryAcl)}, {"protected",(sd.ControlFlags&ControlFlags.DiscretionaryAclProtected)!=0},
   {"sd",Convert.ToBase64String(Bytes(sd))},{"unsafe",bad}};
 }
 public static Dictionary<string,Dictionary<string,object>> Rows() {
  VerifyAncestors();
  Check(Inventory().SequenceEqual(handles.Keys.OrderBy(p=>p.Count(c=>c=='/')).ThenBy(p=>p,StringComparer.Ordinal)));
  var rows=new Dictionary<string,Dictionary<string,object>>(StringComparer.Ordinal);
  foreach(var item in handles) {
   // A fresh path handle must resolve to the pinned object, even on restart.
   using(var fresh=Open(Full(item.Key),false)) Check(Identity(fresh)==Identity(item.Value));
   rows.Add(item.Key,Row(item.Key,item.Value));
  }
  return rows;
 }
 public static Dictionary<string,int> Counts(Dictionary<string,Dictionary<string,object>> rows) {
  int files=0,dirs=0,git=0,ignored=0,source=0,owners=0;
  foreach(var item in rows) {
   var p=item.Key;var r=item.Value;if((string)r["kind"]=="file")files++;else dirs++;
   if(!Trusted((string)r["owner"]))owners++;
   if((bool)r["unsafe"]) {
    if(p==".git"||p.StartsWith(".git/",StringComparison.OrdinalIgnoreCase))git++;
    else if(tracked.Contains(p))source++;
    else if(p!="")ignored++; // original runtime-or-untracked classification
   }
  }
  return new Dictionary<string,int>{{"total",rows.Count},{"files",files},{"directories",dirs},
   {"git_unsafe",git},{"tracked_unsafe",source},{"ignored_unsafe",ignored},{"other_owner",owners}};
 }
 public static Dictionary<string,string> Hashes() {
  var result=new Dictionary<string,string>();
  foreach(var p in runtime) {
   using(var h=Open(Full(p),false)) Check(Identity(h)==Identity(handles[p]));
   // Hash only the four runtime files. No content is copied to Python or disk.
   using(var stream=new FileStream(Full(p),FileMode.Open,FileAccess.Read,FileShare.ReadWrite))
   using(var sha=System.Security.Cryptography.SHA256.Create()) result[p]=BitConverter.ToString(sha.ComputeHash(stream)).Replace("-","").ToLowerInvariant();
  }
  return result;
 }
 static RawSecurityDescriptor From(string value) {return new RawSecurityDescriptor(Convert.FromBase64String(value),0);}
 static RawSecurityDescriptor Restrict(RawSecurityDescriptor original,bool dir) {
  var acl=new RawAcl(2,3); var flags=dir?AceFlags.ContainerInherit|AceFlags.ObjectInherit:AceFlags.None;
  foreach(var sid in new[]{principal,"S-1-5-18","S-1-5-32-544"}) acl.InsertAce(acl.Count,new CommonAce(flags,AceQualifier.AccessAllowed,0x1f01ff,new SecurityIdentifier(sid),false,null));
  return new RawSecurityDescriptor(ControlFlags.DiscretionaryAclPresent|ControlFlags.DiscretionaryAclProtected|ControlFlags.SelfRelative,original.Owner,original.Group,null,acl);
 }
 static RawSecurityDescriptor Inherit(RawSecurityDescriptor parent,RawSecurityDescriptor original,bool dir) {
  if((original.ControlFlags&ControlFlags.DiscretionaryAclProtected)!=0)return original;
  var explicitAcl=new RawAcl(original.DiscretionaryAcl.Revision,original.DiscretionaryAcl.Count);
  foreach(GenericAce a in original.DiscretionaryAcl) if((a.AceFlags&AceFlags.Inherited)==0) explicitAcl.InsertAce(explicitAcl.Count,a.Copy());
  var creator=new RawSecurityDescriptor(ControlFlags.DiscretionaryAclPresent|ControlFlags.SelfRelative,original.Owner,original.Group,null,explicitAcl);
  var mapping=new Mapping{Read=0x120089,Write=0x120116,Execute=0x1200a0,All=0x1f01ff};IntPtr result;
  Check(CreatePrivateObjectSecurityEx(Bytes(parent),Bytes(creator),out result,IntPtr.Zero,dir,0x19,IntPtr.Zero,ref mapping));
  try {var b=new byte[GetSecurityDescriptorLength(result)];Marshal.Copy(result,b,0,b.Length);return new RawSecurityDescriptor(b,0);}
  finally {DestroyPrivateObjectSecurity(ref result);}
 }
 public static Dictionary<string,string> Project(Dictionary<string,string> descriptors,string target,string original) {
  var result=new Dictionary<string,string>(descriptors,StringComparer.Ordinal);
  Check(handles.ContainsKey(target)); var sd=From(original??descriptors[target]);
  bool dir=Directory.Exists(Full(target));
  if(original==null)sd=Restrict(sd,dir);
  else if(target.Length>0) {int slash=target.LastIndexOf('/');string parent=slash<0?"":target.Substring(0,slash);sd=Inherit(From(result[parent]),sd,dir);}
  result[target]=Convert.ToBase64String(Bytes(sd));
  var propagating=new HashSet<string>(StringComparer.Ordinal){target};
  foreach(var p in descriptors.Keys.OrderBy(p=>p.Count(c=>c=='/')).ThenBy(p=>p,StringComparer.Ordinal)) {
   if(p==target||p.Length==0||!(target.Length==0||p.StartsWith(target+"/",StringComparison.Ordinal)))continue;
   int slash=p.LastIndexOf('/');string parent=slash<0?"":p.Substring(0,slash);
   if(!propagating.Contains(parent))continue;
   result[p]=Convert.ToBase64String(Bytes(Inherit(From(result[parent]),From(descriptors[p]),Directory.Exists(Full(p)))));
   if((From(descriptors[p]).ControlFlags&ControlFlags.DiscretionaryAclProtected)==0)propagating.Add(p);
  }
  return result;
 }
 public static Dictionary<string,object> Security(string descriptor) {
  var sd=From(descriptor);return new Dictionary<string,object>{{"sd",descriptor},{"dacl",Convert.ToBase64String(Bytes(sd.DiscretionaryAcl))},
   {"explicit",Explicit(sd.DiscretionaryAcl)},{"protected",(sd.ControlFlags&ControlFlags.DiscretionaryAclProtected)!=0},{"unsafe",Unsafe(sd.DiscretionaryAcl)}};
 }
 public static void Set(string p,string expectedIdentity,string descriptor) {
  VerifyAncestors();
  Check(handles.ContainsKey(p)&&Identity(handles[p])==expectedIdentity);
  using(var fresh=Open(Full(p),false))Check(Identity(fresh)==expectedIdentity);
  SetHandle(handles[p],From(descriptor));
 }
 static void SetHandle(SafeFileHandle handle,RawSecurityDescriptor sd) {
  SetHandleFlags(handle,sd,4u|((sd.ControlFlags&ControlFlags.DiscretionaryAclProtected)!=0?0x80000000u:0x20000000u));
 }
 static void SetHandleFlags(SafeFileHandle handle,RawSecurityDescriptor sd,uint flags) {
  var bytes=Bytes(sd.DiscretionaryAcl);var pin=GCHandle.Alloc(bytes,GCHandleType.Pinned);
  try {
   // Owner, group and SACL pointers and information bits are always zero.
   Check(SetSecurityInfo(handle,1,flags,IntPtr.Zero,IntPtr.Zero,pin.AddrOfPinnedObject(),IntPtr.Zero)==0);
  } finally {pin.Free();}
 }
 static Dictionary<string,string> Descriptors(Dictionary<string,Dictionary<string,object>> rows) {
  return rows.ToDictionary(x=>x.Key,x=>(string)x.Value["sd"],StringComparer.Ordinal);
 }
 static void Verify(Dictionary<string,Dictionary<string,object>> baseline,Dictionary<string,string> expected) {
  var actual=Rows();Check(actual.Count==baseline.Count);
  foreach(var item in baseline) {
   Check(actual.ContainsKey(item.Key));var fresh=actual[item.Key];
   foreach(var key in new[]{"identity","kind","attributes","created","modified","size","links","owner","group"}) Check(Object.Equals(item.Value[key],fresh[key]));
   var security=Security(expected[item.Key]);
   Check((string)fresh["dacl"]==(string)security["dacl"]&&(bool)fresh["protected"]==(bool)security["protected"]);
  }
 }
 static void VerifyMix(Dictionary<string,Dictionary<string,object>> baseline,Dictionary<string,string> first,Dictionary<string,string> second) {
  var actual=Rows();Check(actual.Count==baseline.Count);
  foreach(var item in baseline) {
   var fresh=actual[item.Key];
   foreach(var key in new[]{"identity","kind","attributes","created","modified","size","links","owner","group"})Check(Object.Equals(item.Value[key],fresh[key]));
   var a=Security(first[item.Key]);var b=Security(second[item.Key]);
   Check(((string)fresh["dacl"]==(string)a["dacl"]&&(bool)fresh["protected"]==(bool)a["protected"])||((string)fresh["dacl"]==(string)b["dacl"]&&(bool)fresh["protected"]==(bool)b["protected"]));
  }
 }
 static string HexHash(byte[] bytes) {using(var sha=SHA256.Create())return BitConverter.ToString(sha.ComputeHash(bytes)).Replace("-","").ToLowerInvariant();}
 static void Cleanup(string fixture,Dictionary<string,string> identities) {
  // Only the just-created UUID tree is eligible. Delete by a freshly verified
  // exclusive handle; never a path-based recursive delete or foreign object.
  Check(Path.GetFileName(fixture).StartsWith("personal-server-acl-fixture-",StringComparison.Ordinal));
  foreach(var p in identities.Keys.OrderByDescending(x=>x.Count(c=>c=='/')).ThenByDescending(x=>x.Length)) {
   string path=p==""?fixture:Path.Combine(fixture,p.Replace('/','\\'));
   using(var h=CreateFile(NativePath(path),0x00030080,0,IntPtr.Zero,3,0x02200000,IntPtr.Zero)) {
    Check(!h.IsInvalid&&Identity(h)==identities[p]);Info info;Check(GetFileInformationByHandle(h,out info)&&(info.Attr&1024)==0);
    int disposition=1;Check(SetFileInformationByHandle(h,4,ref disposition,4));
   }
  }
  Check(!Directory.Exists(fixture));
 }
 public static Dictionary<string,object> Fixture(string nonce,Func<Dictionary<string,string>,string,object,Dictionary<string,string>> project) {
  Check(handles.Count==0&&project!=null);FixtureStage="parent_canonical";principal=WindowsIdentity.GetCurrent().User.Value;
  // The system TEMP had untrusted effective DELETE permissions. Choose the
  // existing per-user KnownFolder explicitly; do not change TEMP or any parent
  // ACL, and apply the identical ancestor guard before creating an owned UUID.
  string temp=FixtureParent();
  Check(temp.StartsWith(@"C:\",StringComparison.OrdinalIgnoreCase)&&!temp.StartsWith(@"C:\personal-server",StringComparison.OrdinalIgnoreCase));
  var fixtureParents=new List<SafeFileHandle>();
  var setupHandles=new List<SafeFileHandle>();
  string fixture=Path.Combine(temp,"personal-server-acl-fixture-"+Guid.NewGuid().ToString("N"));
  Check(!fixture.StartsWith(@"C:\personal-server",StringComparison.OrdinalIgnoreCase));
  var identities=new Dictionary<string,string>(StringComparer.Ordinal);bool cleanup=false;int count=0;string binding=null;
  Action<string> createDir=p=>{
   FixtureStage="create_dir";
   string full=p==""?fixture:Path.Combine(fixture,p.Replace('/','\\'));
   Check(CreateDirectory(NativePath(full),IntPtr.Zero));var h=Open(full,true);
   setupHandles.Add(h);identities.Add(p,Identity(h));
  };
  Action<string,string> createFile=(p,text)=>{
   FixtureStage="create_file";
   string full=Path.Combine(fixture,p.Replace('/','\\'));
   var h=CreateFile(NativePath(full),0x40060080,3,IntPtr.Zero,1,0x02200000,IntPtr.Zero);
   Check(!h.IsInvalid);setupHandles.Add(h);identities.Add(p,Identity(h));
   byte[] bytes=System.Text.Encoding.UTF8.GetBytes(text);uint written;
   Check(WriteFile(h,bytes,(uint)bytes.Length,out written,IntPtr.Zero)&&written==bytes.Length&&FlushFileBuffers(h));
  };
  try {
   string ancestor=temp;
   while(!String.IsNullOrEmpty(ancestor)) {
    FixtureStage="ancestor_open";var h=Open(ancestor,false);fixtureParents.Add(h);FixtureStage="ancestor_acl";AncestorPin(h);
    string next=Path.GetDirectoryName(ancestor);if(next==ancestor)break;ancestor=next;
   }
   createDir("");
   FixtureStage="initial_acl";
   using(var h=Open(fixture,true)) {
    var original=Descriptor(h);var descriptor=Restrict(original,true);
    descriptor.DiscretionaryAcl.InsertAce(3,new CommonAce(AceFlags.ObjectInherit|AceFlags.ContainerInherit,AceQualifier.AccessAllowed,0x120089,new SecurityIdentifier("S-1-5-21-0-0-0-1001"),false,null));
    SetHandle(h,descriptor);
   }
   var rt=new[]{".env","docker-compose.yml","docker-compose.n100.yml","docker-compose.portal-bridge.yml"};
   foreach(var p in rt)createFile(p,"nonsecret-fixture\n");
   var variants=new[]{AceFlags.ObjectInherit,AceFlags.ContainerInherit,AceFlags.ObjectInherit|AceFlags.ContainerInherit|AceFlags.NoPropagateInherit,AceFlags.ObjectInherit|AceFlags.InheritOnly};
   for(int i=0;i<variants.Length;i++) {
    string name="case"+i;string p=Path.Combine(fixture,name);createDir(name);
    FixtureStage="variants";
    using(var h=Open(p,true)) {
     var original=Descriptor(h);var acl=new RawAcl(2,original.DiscretionaryAcl.Count+2);
     acl.InsertAce(0,new CommonAce(AceFlags.ContainerInherit,AceQualifier.AccessDenied,0x10000,new SecurityIdentifier("S-1-5-21-0-0-0-1002"),false,null));
     acl.InsertAce(1,new CommonAce(variants[i],AceQualifier.AccessAllowed,0x120089,new SecurityIdentifier("S-1-5-21-0-0-0-1001"),false,null));
     foreach(GenericAce ace in original.DiscretionaryAcl)acl.InsertAce(acl.Count,ace.Copy());
     SetHandle(h,new RawSecurityDescriptor(ControlFlags.DiscretionaryAclPresent|ControlFlags.SelfRelative,original.Owner,original.Group,null,acl));
    }
    createDir(name+"/child");createFile(name+"/child/value","fixture");createFile(name+"/value","fixture");
   }
   createDir("protected");
   using(var h=Open(Path.Combine(fixture,"protected"),true))SetHandle(h,Restrict(Descriptor(h),true));
   createFile("protected/value","fixture");
   FixtureStage="start";Start(fixture,new string[0],rt,new string[0]);var baseline=Rows();var originalDescriptors=Descriptors(baseline);
   Check(baseline.Count==identities.Count&&baseline.All(x=>identities[x.Key]==(string)x.Value["identity"]));
   var hashes=Hashes();count=baseline.Count;binding=HexHash(System.Text.Encoding.UTF8.GetBytes(principal+"|"+identities[""]));
   FixtureStage="project";var expected=project(originalDescriptors,"",null);Set("",identities[""],expected[""]);FixtureStage="verify";Verify(baseline,expected);
   foreach(var p in rt){expected=project(expected,p,null);Set(p,identities[p],expected[p]);Verify(baseline,expected);}
   FixtureStage="rollback";foreach(var p in rt.Reverse()){expected=project(expected,p,originalDescriptors[p]);Set(p,identities[p],expected[p]);Verify(baseline,expected);}
   var restrictedDescriptors=new Dictionary<string,string>(expected,StringComparer.Ordinal);
   expected=project(expected,"",originalDescriptors[""]);Set("",identities[""],expected[""]);Verify(baseline,originalDescriptors);
   FixtureStage="partialmix";
   foreach(var p in new[]{"case0/value","case1/value"})SetHandleFlags(handles[p],From(restrictedDescriptors[p]),4);
   VerifyMix(baseline,originalDescriptors,restrictedDescriptors);
   foreach(var p in new[]{"case0/value","case1/value"})Set(p,identities[p],originalDescriptors[p]);
   Verify(baseline,originalDescriptors);
   Check(Hashes().All(x=>hashes[x.Key]==x.Value));
   FixtureStage="dpapi";byte[] plain=System.Text.Encoding.UTF8.GetBytes("nonsecret-dpapi-fixture"), entropy=System.Text.Encoding.ASCII.GetBytes("personal-server-windows-acl-v1");
   var cipher=ProtectedData.Protect(plain,entropy,DataProtectionScope.CurrentUser);
   Check(!plain.SequenceEqual(cipher)&&plain.SequenceEqual(ProtectedData.Unprotect(cipher,entropy,DataProtectionScope.CurrentUser)));
  } finally {
   Close();foreach(var h in setupHandles)h.Dispose();
   string stage=FixtureStage;
   try {if(identities.Count>0){FixtureStage="cleanup";Cleanup(fixture,identities);cleanup=true;FixtureStage=stage;}}
   finally {foreach(var h in fixtureParents)h.Dispose();}
  }
  return new Dictionary<string,object>{{"nonce",nonce},{"binding_sha256",binding},{"objects",count},{"projection",true},{"rollback",true},{"partialmix",true},{"content_hashes",true},{"dpapi",true},{"cleanup",cleanup},{"sacl_contract","dacl_only_no_privilege_change"},{"utc",DateTimeOffset.UtcNow.ToString("o")}};
 }
 public static void Close() {foreach(var h in handles.Values)h.Dispose();handles.Clear();foreach(var h in ancestors)h.Dispose();ancestors.Clear();ancestorPins.Clear();}
}
'@
$baselineCounts=$null
$json=New-Object System.Web.Script.Serialization.JavaScriptSerializer
$json.MaxJsonLength=67108864
$json.RecursionLimit=100
function Invoke-DescriptorProjection($Descriptors,$Target,$Original) {
 # PowerShell binds ordinary $null to String.Empty for a .NET string argument.
 # Only JSON null means forward restriction; empty/missing restore SD is invalid.
 $value=[System.Management.Automation.Language.NullString]::Value
 if($null -ne $Original) {
  if($Original -isnot [System.Collections.IDictionary] -or $Original.sd -isnot [string] -or [string]::IsNullOrEmpty($Original.sd)){throw 'invalid_project_original'}
  $value=[string]$Original.sd
 }
 return [BoundAcl]::Project($Descriptors,[string]$Target,$value)
}
$fixtureProject=[System.Func[System.Collections.Generic.Dictionary[string,string],string,object,System.Collections.Generic.Dictionary[string,string]]]{
 param($descriptors,$target,$original)
 # Exercise the same JSON decode and PS-to-.NET call as production, on the
 # owned fixture only. Non-null rollback SD bytes are never normalized.
 $wireOriginal=$null;if($null -ne $original){$wireOriginal=@{sd=[string]$original}}
 $wire=$json.DeserializeObject($json.Serialize(@{original=$wireOriginal}))
 Invoke-DescriptorProjection $descriptors $target $wire.original
}
try {
 while($null -ne ($line=[Console]::In.ReadLine())) {
  try {
   $q=$json.DeserializeObject($line)
   switch($q.op) {
    'temp_check' {$r=[BoundAcl]::TempCheck()}
    'fixture_parent_check' {$r=[BoundAcl]::FixtureParentCheck()}
    'fixture' {
     [BoundAcl]::FixtureStage='original_boundary'
     $emptyDescriptors=New-Object 'System.Collections.Generic.Dictionary[string,string]'
     foreach($bad in @(@{sd=''},@{})) {
      $rejected=$false
      try {$unused=Invoke-DescriptorProjection $emptyDescriptors '' $bad} catch {if($_.Exception.Message -ne 'invalid_project_original'){throw};$rejected=$true}
      if(!$rejected){throw 'invalid_project_original_boundary'}
     }
     $r=[BoundAcl]::Fixture([string]$q.nonce,$fixtureProject);$r['ps_original_boundary']=$true
    }
    'open' {[BoundAcl]::Start($q.scope.root,[string[]]$q.scope.tracked_paths,[string[]]$q.runtime,[string[]]$q.targets);$baselineCounts=[BoundAcl]::Counts([BoundAcl]::Rows());$r=@{ok=$true;ancestor_sha256=[BoundAcl]::AncestorSHA}}
    'snapshot' {$r=@{entries=[BoundAcl]::Rows();counts=$baselineCounts;runtime_hashes=[BoundAcl]::Hashes();principal=[BoundAcl]::Principal;projection_contract='win32_private_object_security_v1'}}
    'project' {
     $descriptors=New-Object 'System.Collections.Generic.Dictionary[string,string]'
     foreach($p in $q.snapshot.entries.GetEnumerator()){$descriptors.Add($p.Key,[string]$p.Value.sd)}
     $projected=Invoke-DescriptorProjection $descriptors $q.target $q.original
     foreach($p in $q.snapshot.entries.GetEnumerator()){$s=[BoundAcl]::Security($projected[$p.Key]);foreach($k in $s.Keys){$p.Value[$k]=$s[$k]}}
     $r=$q.snapshot
    }
    'set' {[BoundAcl]::Set([string]$q.target,[string]$q.entry.identity,[string]$q.entry.sd);$r=@{ok=$true}}
    'encrypt' {$b=[Convert]::FromBase64String($q.data);$c=[Security.Cryptography.ProtectedData]::Protect($b,[Text.Encoding]::ASCII.GetBytes('personal-server-windows-acl-v1'),[Security.Cryptography.DataProtectionScope]::CurrentUser);$r=@{data=[Convert]::ToBase64String($c)}}
    'decrypt' {$b=[Convert]::FromBase64String($q.data);$c=[Security.Cryptography.ProtectedData]::Unprotect($b,[Text.Encoding]::ASCII.GetBytes('personal-server-windows-acl-v1'),[Security.Cryptography.DataProtectionScope]::CurrentUser);$r=@{data=[Convert]::ToBase64String($c)}}
    'close' {[BoundAcl]::Close();$r=@{ok=$true}}
    default {throw 'invalid_native_operation'}
   }
   [Console]::Out.WriteLine($json.Serialize($r));[Console]::Out.Flush()
  } catch {
   $reason='native_acl_failure';if($q.op -eq 'fixture'){$reason='native_fixture_'+[BoundAcl]::FixtureStage}
   [Console]::Out.WriteLine(('{"error":"'+$reason+'"}'));[Console]::Out.Flush()
  }
 }
} finally {[BoundAcl]::Close()}
'''


class NativeBackend:
    """One Windows adapter session retains all handles until the controller ends.

    ``guard`` and ``fixture_pin`` come from the trusted reviewed launcher. No CLI
    flag claims that a Windows fixture passed. An unknown adapter process blocks
    every further request and all recovery writes across restart.
    """
    def __init__(self, lock_fd, *, guard=None, timeout=180):
        info = os.fstat(lock_fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                and info.st_nlink == 1 and stat.S_IMODE(info.st_mode) == 0o600,
                "unsafe_sequence_lock")
        # The caller must retain the context manager; taking the same flock FD
        # does not prove ownership, so native mutations additionally require guard.
        self.lock_owned = True
        self.mutation_authorized = False
        self.recovery_quiescent = False
        self.guard, self.timeout = guard, timeout
        self.receipt = None
        self.process = None
        self.unknown = False
        self.opened = False
        self.crypto = None
        self.script = POWERSHELL

    def request(self, request, mutation=False):
        require(not self.unknown, "native_process_held")
        if self.process is None:
            bootstrap = ("[Console]::InputEncoding=New-Object Text.UTF8Encoding($false);"
                         "[Console]::OutputEncoding=New-Object Text.UTF8Encoding($false);"
                         "$s=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String([Console]::In.ReadLine()));"
                         "&([ScriptBlock]::Create($s))")
            script = base64.b64encode(bootstrap.encode("utf-16-le")).decode("ascii")
            self.process = subprocess.Popen([POWERSHELL_BINARY, "-NoLogo", "-NoProfile",
                                             "-NonInteractive", "-EncodedCommand", script],
                                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.DEVNULL, start_new_session=True,
                                            env={k: v for k, v in os.environ.items()
                                                 if not k.upper().startswith(("PSMODULE", "POWERSHELL"))})
            self.startup = base64.b64encode(self.script.encode("utf-8")) + b"\n"
        try:
            data = self.startup + encoded(request) + b"\n"
            self.startup = b""
            # Large tree requests must not block forever on a full stdin pipe.
            os.set_blocking(self.process.stdin.fileno(), False)
            os.set_blocking(self.process.stdout.fileno(), False)
            deadline = time.monotonic() + self.timeout
            sent, output = 0, bytearray()
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError
                reads, writes, _ = select.select([self.process.stdout],
                                                 [self.process.stdin] if sent < len(data) else [], [], left)
                if writes:
                    sent += os.write(self.process.stdin.fileno(), data[sent:sent + 65536])
                if reads:
                    chunk = os.read(self.process.stdout.fileno(), 65536)
                    if not chunk:
                        raise EOFError
                    output.extend(chunk)
                    require(len(output) <= 64 * 1024 * 1024, "native_output_limit")
                    if b"\n" in output:
                        require(sent == len(data) and output.count(b"\n") == 1,
                                "invalid_native_frame")
                        result = json.loads(output)
                        if "error" in result:
                            allowed = {"native_acl_failure", "native_crypto_failure"} | {
                                "native_fixture_" + stage for stage in
                                ("setup", "original_boundary", "temp_canonical", "parent_canonical", "ancestor_open", "ancestor_acl", "create_dir",
                                 "initial_acl", "create_file", "variants", "start", "project", "verify",
                                 "rollback", "partialmix", "dpapi", "cleanup")}
                            raise Blocked(result["error"] if result["error"] in allowed else "native_acl_failure")
                        return result
        except Blocked as exc:
            if mutation and exc.reason in ("native_output_limit", "invalid_native_frame"):
                self.unknown = True
                raise UnknownWrite("native_response_unknown") from None
            raise
        except (OSError, ValueError, TimeoutError, EOFError):
            self.unknown = True
            if mutation:
                raise UnknownWrite("native_response_unknown") from None
            raise Blocked("native_adapter_unavailable") from None

    def open(self, scope):
        if not self.opened:
            require(scope["fixture_sha256"] == digest(POWERSHELL.encode("utf-8")),
                    "fixture_adapter_pin_mismatch")
            if self.guard:
                self.check_guard(scope)
            self.native_self_test()
            opened = self.request({"op": "open", "scope": scope, "runtime": list(RUNTIME),
                                   "targets": getattr(self, "recovery_targets", [])})
            require(re.fullmatch(r"[0-9a-f]{64}", opened.get("ancestor_sha256", "")),
                    "ancestor_guard_missing")
            self.ancestor_sha256 = opened["ancestor_sha256"]
            self.opened = True
            self.mutation_authorized = self.guard is not None and self.receipt is not None

    def check_guard(self, scope):
        proof = self.guard(scope)
        require(isinstance(proof, dict) and all(proof.get(key) is True for key in
                ("source_pin", "ci_verified", "sequence_lock", "writers_idle")),
                "trusted_operational_guard_required")

    def native_self_test(self):
        require(not self.opened, "fixture_before_root_only")
        nonce = os.urandom(32).hex()
        receipt = self.request({"op": "fixture", "nonce": nonce}, mutation=True)
        require(receipt.get("nonce") == nonce and all(receipt.get(key) is True for key in
                ("projection", "rollback", "partialmix", "content_hashes", "dpapi", "cleanup", "ps_original_boundary"))
                and re.fullmatch(r"[0-9a-f]{64}", receipt.get("binding_sha256", ""))
                and receipt.get("sacl_contract") == "dacl_only_no_privilege_change",
                "native_fixture_failed")
        timestamp = datetime.fromisoformat(receipt["utc"].replace("Z", "+00:00"))
        require(timestamp.tzinfo is not None
                and abs((datetime.now(timezone.utc) - timestamp).total_seconds()) < 300,
                "stale_native_fixture")
        receipt["adapter_sha256"] = digest(POWERSHELL.encode("utf-8"))
        self.receipt = receipt
        return {"reason": "native_fixture_passed", "objects": receipt["objects"],
                "adapter_sha256": receipt["adapter_sha256"],
                "receipt_sha256": object_hash(receipt)}

    def native_temp_check(self):
        return self.native_parent_check("temp_check")

    def native_fixture_parent_check(self):
        return self.native_parent_check("fixture_parent_check")

    def native_parent_check(self, operation):
        require(not self.opened, "temp_check_before_root_only")
        require(operation in ("temp_check", "fixture_parent_check"), "invalid_temp_check")
        result = self.request({"op": operation})
        require(set(result) == {"reason", "ancestors"} and result["reason"] in
                ("native_temp_checked", "native_temp_canonical_blocked", "native_temp_ancestor_blocked"),
                "invalid_temp_check")
        for row in result["ancestors"]:
            require(set(row) == {"index", "opened", "owner_trusted", "acl_supported", "dangerous_allow"}
                    and isinstance(row["index"], int) and isinstance(row["dangerous_allow"], int)
                    and all(type(row[key]) is bool for key in ("opened", "owner_trusted", "acl_supported")),
                    "invalid_temp_check")
        return result

    def snapshot(self):
        return self.request({"op": "snapshot"})

    def stable_snapshot(self):
        last = self.snapshot()
        for _ in range(2):
            current = self.snapshot()
            require(same_snapshot(last, current), "unstable_tree")
            last = current
        return last

    def project(self, snapshot, target, original):
        if original is not None:
            require(isinstance(original, dict) and isinstance(original.get("sd"), str)
                    and bool(original["sd"]), "invalid_project_original")
        return self.request({"op": "project", "snapshot": snapshot,
                             "target": target, "original": original})

    def set_dacl(self, target, entry):
        require(self.mutation_authorized, "native_fixture_and_guard_required")
        self.check_guard(None)
        self.request({"op": "set", "target": target, "entry": entry}, mutation=True)

    def encrypt(self, plaintext):
        result = self.crypto_request({"op": "encrypt", "data": base64.b64encode(plaintext).decode("ascii")})
        return base64.b64decode(result["data"], validate=True)

    def decrypt(self, ciphertext):
        result = self.crypto_request({"op": "decrypt", "data": base64.b64encode(ciphertext).decode("ascii")})
        return base64.b64decode(result["data"], validate=True)

    def crypto_request(self, request):
        # Independent, non-filesystem DPAPI session remains available when the
        # mutation session loses its response, so HELD can still be fsynced.
        if self.crypto is None:
            self.crypto = object.__new__(NativeBackend)
            self.crypto.process, self.crypto.unknown = None, False
            self.crypto.timeout, self.crypto.crypto = self.timeout, None
            self.crypto.script = CRYPTO_POWERSHELL
        return self.crypto.request(request)

    def close(self):
        try:
            self.close_process()
        except Blocked as exc:
            if not getattr(self, "held_notified", False):
                callback = getattr(self, "on_held", None)
                if callback:
                    callback(exc.reason)
            raise
        finally:
            if self.crypto:
                self.crypto.close()

    def close_process(self):
        if self.process is not None:
            # Closing stdin asks the Windows session to release its own handles.
            # WSL process-group termination cannot prove Windows child termination.
            try:
                self.process.stdin.close()
            except OSError:
                pass
            try:
                returncode = self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.unknown = True
                callback = getattr(self, "on_held", None)
                self.held_notified = True
                # Broken stdout or a failed journal fsync must never bypass the
                # lease-retaining wait. Intent was durable before the setter.
                try:
                    if callback:
                        callback("native_process_termination_unknown")
                except BaseException:
                    pass
                try:
                    print('{"reason":"native_process_held"}', flush=True)
                except BaseException:
                    pass
                # Keep this process and its caller's sequence lease alive. Never
                # equate killing a WSL process group with Windows termination.
                while self.process.poll() is None:
                    try:
                        self.process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        continue
                raise Blocked("native_process_termination_unknown") from None
            require(returncode == 0, "native_process_failed")


CRYPTO_POWERSHELL = r'''
$ErrorActionPreference='Stop'
Add-Type -AssemblyName System.Security
while($null -ne ($line=[Console]::In.ReadLine())) {
 try {
  $q=$line|ConvertFrom-Json;$b=[Convert]::FromBase64String($q.data)
  $entropy=[Text.Encoding]::ASCII.GetBytes('personal-server-windows-acl-v1')
  if($q.op -eq 'encrypt'){$r=[Security.Cryptography.ProtectedData]::Protect($b,$entropy,[Security.Cryptography.DataProtectionScope]::CurrentUser)}
  elseif($q.op -eq 'decrypt'){$r=[Security.Cryptography.ProtectedData]::Unprotect($b,$entropy,[Security.Cryptography.DataProtectionScope]::CurrentUser)}
  else{throw 'invalid_crypto_operation'}
  [Console]::Out.WriteLine(('{"data":"'+[Convert]::ToBase64String($r)+'"}'));[Console]::Out.Flush()
 }catch{[Console]::Out.WriteLine('{"error":"native_crypto_failure"}');[Console]::Out.Flush()}
}
'''


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--prepare", action="store_true")
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--rollback", action="store_true")
    mode.add_argument("--status", action="store_true")
    mode.add_argument("--native-self-test", action="store_true")
    mode.add_argument("--native-temp-check", action="store_true")
    mode.add_argument("--native-fixture-parent-check", action="store_true")
    parser.add_argument("--vault")
    parser.add_argument("--reviewed-sha256")
    args = parser.parse_args(argv)
    backend = vault = None
    try:
        with sequence_lock() as lock:
            backend = NativeBackend(lock)
            try:
                if args.native_self_test:
                    result = backend.native_self_test()
                elif args.native_temp_check:
                    result = backend.native_temp_check()
                elif args.native_fixture_parent_check:
                    result = backend.native_fixture_parent_check()
                else:
                    require(args.vault is not None, "vault_required")
                    vault = Vault(args.vault, backend)
                    transaction = Transaction(backend, vault)
                    if args.prepare:
                        result = transaction.prepare(json.load(sys.stdin))
                    elif args.apply:
                        result = transaction.apply(args.reviewed_sha256)
                    elif args.rollback:
                        result = transaction.rollback(args.reviewed_sha256)
                    else:
                        result = transaction.status(args.reviewed_sha256)
            finally:
                # Close while the sequence lease is still held, before success.
                backend.close()
            print(json.dumps(result, sort_keys=True))
    except (Blocked, OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"reason": exc.reason if isinstance(exc, Blocked) else "acl_operation_blocked"}))
        return 2
    finally:
        if vault:
            vault.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
