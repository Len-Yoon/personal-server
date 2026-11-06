"""Durable transition journal; adapters must perform authenticated live checks.

No environment variable or HTTP liveness result authorizes promotion. The host
guard must exclude legacy recovery for the entire transition. A failed phase is
retained across restart; ambiguous actions are reconciled before another call.
"""
from contextlib import contextmanager
import ctypes
import json
import os
from pathlib import Path
import stat
import sys
import tempfile


class TransitionBlocked(RuntimeError):
    pass


@contextmanager
def exclusive(path):
    """One controller; never unlink the lock file while contenders may hold it."""
    path = Path(path)
    if path.is_symlink() or not path.parent.is_dir():
        raise TransitionBlocked("invalid_controller_lock")
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    mutex = None
    try:
        if sys.platform == "win32":
            api = ctypes.windll.kernel32
            api.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
            api.CreateMutexW.restype = ctypes.c_void_p
            api.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            api.ReleaseMutex.argtypes = [ctypes.c_void_p]
            api.CloseHandle.argtypes = [ctypes.c_void_p]
            mutex = api.CreateMutexW(None, False, "Global\\PersonalServerHAController")
            if not mutex or api.WaitForSingleObject(mutex, 0) != 0:
                raise TransitionBlocked("controller_lock_unavailable")
        else:
            import fcntl
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise TransitionBlocked("controller_lock_unavailable") from None
        yield
    finally:
        if mutex:
            ctypes.windll.kernel32.ReleaseMutex(mutex)
            ctypes.windll.kernel32.CloseHandle(mutex)
        os.close(fd)


class Journal:
    def __init__(self, path):
        self.path = Path(path)

    def read(self):
        try:
            info = self.path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or (sys.platform != "win32" and (info.st_uid != os.geteuid() or info.st_mode & 0o077))):
                raise ValueError()
            value = json.loads(self.path.read_text())
            if (set(value) != {"cluster", "generation", "old", "target", "phase", "attempts", "timeline"}
                    or value["old"] not in {"A", "B"} or value["target"] not in {"A", "B"}
                    or value["old"] == value["target"] or not isinstance(value["cluster"], str)
                    or not value["cluster"] or type(value["generation"]) is not int or value["generation"] < 1
                    or type(value["attempts"]) is not int or not 0 <= value["attempts"] <= 3
                    or type(value["timeline"]) is not int or value["timeline"] < 1
                    or value["phase"] not in {"fencing", "promoting", "rejoining", "validating", "ready"}):
                raise ValueError()
            return value
        except (OSError, ValueError, TypeError):
            raise TransitionBlocked("controller_journal_invalid") from None

    def save(self, value):
        if self.path.is_symlink() or not self.path.parent.is_dir():
            raise TransitionBlocked("controller_journal_invalid")
        temporary = None
        try:
            fd, temporary = tempfile.mkstemp(prefix=".ha-journal-", dir=self.path.parent)
            with os.fdopen(fd, "w") as stream:
                json.dump(value, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            if sys.platform == "win32":
                # MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH.
                move = ctypes.windll.kernel32.MoveFileExW
                move.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
                if not move(str(temporary), str(self.path), 9):
                    raise OSError()
            else:
                os.replace(temporary, self.path)
                directory = os.open(self.path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        except (OSError, TypeError, ValueError):
            raise TransitionBlocked("controller_journal_not_durable") from None
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)


class Controller:
    """`backend.guard` must stay live and exclude all restart paths throughout.

    Rejoin creates a NEW standby resource; it must never restart the fenced old
    primary. `validate` performs fresh synchronous WAL proof before `open` may
    publish a new authority. Adapter responses alone are not durable receipts.
    """
    def __init__(self, journal, backend):
        self.journal, self.backend = Journal(journal), backend

    def initialize(self, *, cluster, generation, old, target, timeline):
        with exclusive(self.journal.path.with_suffix(".lock")):
            if self.journal.path.exists():
                raise TransitionBlocked("controller_already_initialized")
            state = dict(cluster=cluster, generation=generation, old=old, target=target,
                         timeline=timeline, phase="fencing", attempts=0)
            # Validate the same shape before an adapter can see initialization.
            self.journal.save(state)
            self.journal.read()

    def advance(self):
        with exclusive(self.journal.path.with_suffix(".lock")), self.backend.guard():
            state = self.journal.read()
            if state["phase"] == "ready":
                self.backend.assert_ready(state)
                return state
            if state["attempts"] >= 3:
                raise TransitionBlocked("transition_attempts_exhausted")
            state["attempts"] += 1
            self.journal.save(state)  # Persist BEFORE the first mutating action.
            try:
                self.backend.close(state)
                if state["phase"] == "fencing":
                    if not self.backend.fenced(state):
                        self.backend.fence(state)
                    if not self.backend.fenced(state):
                        raise TransitionBlocked("positive_fence_required")
                    state["phase"] = "promoting"
                    self.journal.save(state)
                if state["phase"] == "promoting":
                    if not self.backend.fenced(state):
                        raise TransitionBlocked("fence_lost_before_promotion")
                    if not self.backend.promoted(state):
                        self.backend.promote(state)
                    if not self.backend.promoted(state):
                        raise TransitionBlocked("promotion_unconfirmed")
                    state["phase"] = "rejoining"
                    self.journal.save(state)
                if state["phase"] == "rejoining":
                    if not self.backend.fenced(state):
                        raise TransitionBlocked("old_primary_restarted")
                    if not self.backend.rejoined(state):
                        self.backend.rejoin(state)
                    if not self.backend.rejoined(state):
                        raise TransitionBlocked("rejoin_unconfirmed")
                    state["phase"] = "validating"
                    self.journal.save(state)
                if state["phase"] == "validating":
                    if not self.backend.fenced(state):
                        raise TransitionBlocked("old_primary_restarted")
                    self.backend.validate(state)
                    self.backend.open(state)
                    self.backend.assert_ready(state)
                    state["phase"] = "ready"
                    self.journal.save(state)
                return state
            except TransitionBlocked:
                raise
            except Exception:
                # Do not serialize adapter errors, credentials or command data.
                raise TransitionBlocked("transition_outcome_unknown") from None
