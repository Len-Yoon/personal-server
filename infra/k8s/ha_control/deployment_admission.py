"""GitHub-side deployment exclusion for the N100 migration backend."""
from concurrent.futures import ThreadPoolExecutor
import json
import re
import subprocess
import time

from .controller import TransitionBlocked

RUN_PAGE_WORKERS = 3
ADMISSION_READ_SECONDS = 60
TRANSIENT_API_ERROR = re.compile(
    rb"HTTP (?:429|500|502|503|504)\b|no such host|connection reset|tls handshake timeout|i/o timeout",
    re.IGNORECASE,
)


class DeploymentAdmission:
    """Pin one workflow and drain its runs after disabling future admission.

    The enclosing coordinator journals intent and ownership before calling a
    mutation. On an unknown response, reconcile remotely before restoring.
    The host must additionally fence the local runner and any manual launchers.
    """

    def __init__(self, repository, workflow_id, gh, *, runner=subprocess.run):
        if (not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
                or any(part in (".", "..") for part in repository.split("/"))
                or type(workflow_id) is not int or workflow_id < 1
                or type(gh) is not str or not gh.startswith("/")):
            raise TransitionBlocked("deployment_admission_identity_invalid")
        self.workflow_id = workflow_id
        self.endpoint = f"repos/{repository}/actions/workflows/{workflow_id}"
        self.gh = gh
        self.run = runner

    def _api(self, endpoint, *args, retry_read=False, deadline=None):
        command = (self.gh, "api", "--hostname", "github.com", endpoint, *args)
        for may_retry in ((True, False) if retry_read else (False,)):
            timeout = 20 if deadline is None else min(20, deadline - time.monotonic())
            if timeout <= 0:
                raise TransitionBlocked("deployment_admission_read_deadline")
            try:
                response = self.run(command, capture_output=True, check=True, timeout=timeout)
            except (OSError, subprocess.SubprocessError) as error:
                transient = isinstance(error, subprocess.TimeoutExpired) or (
                    isinstance(error, subprocess.CalledProcessError)
                    and type(error.stderr) is bytes and TRANSIENT_API_ERROR.search(error.stderr)
                )
                if not may_retry or not transient:
                    raise TransitionBlocked("deployment_admission_command_unconfirmed") from None
                delay = 1 if deadline is None else min(1, max(0, deadline - time.monotonic()))
                time.sleep(delay)
            else:
                if deadline is not None and time.monotonic() >= deadline:
                    raise TransitionBlocked("deployment_admission_read_deadline")
                return response.stdout

    def state(self, *, deadline=None):
        result = json.loads(self._api(self.endpoint, '-H', 'Cache-Control: no-cache',
                                      retry_read=True, deadline=deadline))
        if (result.get("id") != self.workflow_id or result.get("path") != ".github/workflows/deploy-n100.yml"
                or result.get("state") not in ("active", "disabled_manually")):
            raise TransitionBlocked("deployment_admission_identity_changed")
        return result["state"]

    def close(self):
        if self.state() != "active":
            raise TransitionBlocked("deployment_admission_baseline_changed")
        self._api(self.endpoint + "/disable", "--method", "PUT")
        deadline = time.monotonic() + ADMISSION_READ_SECONDS
        # The write can be acknowledged before the state read reflects it.
        while self.state(deadline=deadline) == "active":
            if time.monotonic() >= deadline:
                raise TransitionBlocked("deployment_admission_disable_unconfirmed")
            time.sleep(1)
        while not self._closed(deadline, acquiring=True):
            if time.monotonic() >= deadline:
                raise TransitionBlocked("deployment_admission_runs_not_settled")
            time.sleep(1)

    def _closed(self, deadline, *, acquiring=False):
        def disabled():
            if self.state(deadline=deadline) == "disabled_manually":
                return True
            if acquiring:
                # Until the first full closure proof, conflicting reads can
                # reflect the acknowledged disable still propagating.
                return False
            raise TransitionBlocked("deployment_admission_reopened")

        if not disabled():
            return False
        first = self._run_page(1, deadline)
        total = first["total_count"]
        page_count = max(1, (total + 99) // 100)
        ids = set()
        def settled(number, page):
            expected = min(100, total - 100 * (number - 1))
            if page["total_count"] != total or len(page["workflow_runs"]) != expected:
                raise TransitionBlocked("deployment_admission_runs_unconfirmed")
            for row in page["workflow_runs"]:
                run = row.get("id") if type(row) is dict else None
                if (type(run) is not int or run < 1 or run in ids
                        or type(row.get("workflow_id")) is not int or row["workflow_id"] != self.workflow_id):
                    raise TransitionBlocked("deployment_admission_runs_unconfirmed")
                ids.add(run)
                if row.get("status") != "completed":
                    return False
            return True

        if not settled(1, first):
            return False
        if page_count > 1:
            with ThreadPoolExecutor(max_workers=RUN_PAGE_WORKERS) as pool:
                for start in range(2, page_count + 1, RUN_PAGE_WORKERS):
                    numbers = range(start, min(start + RUN_PAGE_WORKERS, page_count + 1))
                    pages = pool.map(lambda number: self._run_page(number, deadline), numbers)
                    if not all(settled(number, page) for number, page in zip(numbers, pages)):
                        return False
        if len(ids) != total:
            raise TransitionBlocked("deployment_admission_runs_unconfirmed")
        return disabled()

    def _run_page(self, number, deadline):
        try:
            page = json.loads(self._api(self.endpoint + f"/runs?per_page=100&page={number}",
                                        '-H', 'Cache-Control: no-cache', retry_read=True, deadline=deadline))
        except (TypeError, ValueError):
            raise TransitionBlocked("deployment_admission_runs_unconfirmed") from None
        if (type(page) is not dict or type(page.get("total_count")) is not int
                or not 0 <= page["total_count"] <= 100000 or type(page.get("workflow_runs")) is not list):
            raise TransitionBlocked("deployment_admission_runs_unconfirmed")
        return page

    def assert_closed(self):
        if not self._closed(time.monotonic() + ADMISSION_READ_SECONDS):
            raise TransitionBlocked("deployment_admission_runs_not_settled")

    def restore(self, *, owned_disable):
        if owned_disable is not True or self.state() != "disabled_manually":
            raise TransitionBlocked("deployment_admission_restore_ownership_required")
        self._api(self.endpoint + "/enable", "--method", "PUT")
        if self.state() != "active":
            raise TransitionBlocked("deployment_admission_restore_unconfirmed")
