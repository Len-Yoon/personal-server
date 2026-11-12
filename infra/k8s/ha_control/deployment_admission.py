"""GitHub-side deployment exclusion for the N100 migration backend."""
import json
import re
import subprocess
import time

from .controller import TransitionBlocked


def _pages(raw):
    """Accept the concatenated JSON objects emitted by gh api --paginate."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    decoder = json.JSONDecoder()
    pages = []
    offset = 0
    whitespace = re.compile(r"\s*")
    while offset < len(raw):
        offset = whitespace.match(raw, offset).end()
        if offset == len(raw):
            break
        page, offset = decoder.raw_decode(raw, offset)
        if type(page) is not dict or len(pages) >= 1000:
            raise ValueError("invalid_workflow_run_page")
        pages.append(page)
    return pages


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

    def _api(self, endpoint, *args):
        try:
            return self.run((self.gh, "api", "--hostname", "github.com", endpoint, *args),
                            capture_output=True, check=True, timeout=20).stdout
        except (OSError, subprocess.SubprocessError):
            raise TransitionBlocked("deployment_admission_command_unconfirmed") from None

    def state(self):
        result = json.loads(self._api(self.endpoint))
        if (result.get("id") != self.workflow_id or result.get("path") != ".github/workflows/deploy-n100.yml"
                or result.get("state") not in ("active", "disabled_manually")):
            raise TransitionBlocked("deployment_admission_identity_changed")
        return result["state"]

    def close(self):
        if self.state() != "active":
            raise TransitionBlocked("deployment_admission_baseline_changed")
        self._api(self.endpoint + "/disable", "--method", "PUT")
        deadline = time.monotonic() + 60
        while not self._closed():
            if time.monotonic() >= deadline:
                raise TransitionBlocked("deployment_admission_runs_not_settled")
            time.sleep(1)

    def _closed(self):
        if self.state() != "disabled_manually":
            raise TransitionBlocked("deployment_admission_reopened")
        try:
            pages = _pages(self._api(self.endpoint + "/runs?per_page=100", "--paginate"))
        except (TypeError, ValueError):
            raise TransitionBlocked("deployment_admission_runs_unconfirmed") from None
        if not pages or type(pages[0].get("total_count")) is not int or pages[0]["total_count"] < 0:
            raise TransitionBlocked("deployment_admission_runs_unconfirmed")
        ids = set()
        for page in pages:
            if page.get("total_count") != pages[0]["total_count"] or type(page.get("workflow_runs")) is not list:
                raise TransitionBlocked("deployment_admission_runs_unconfirmed")
            for row in page["workflow_runs"]:
                run = row.get("id") if type(row) is dict else None
                if (type(run) is not int or run < 1 or run in ids
                        or type(row.get("workflow_id")) is not int or row["workflow_id"] != self.workflow_id):
                    raise TransitionBlocked("deployment_admission_runs_unconfirmed")
                ids.add(run)
                if row.get("status") != "completed":
                    return False
        if len(ids) != pages[0]["total_count"]:
            raise TransitionBlocked("deployment_admission_runs_unconfirmed")
        if self.state() != "disabled_manually":
            raise TransitionBlocked("deployment_admission_reopened")
        return True

    def assert_closed(self):
        if not self._closed():
            raise TransitionBlocked("deployment_admission_runs_not_settled")

    def restore(self, *, owned_disable):
        if owned_disable is not True or self.state() != "disabled_manually":
            raise TransitionBlocked("deployment_admission_restore_ownership_required")
        self._api(self.endpoint + "/enable", "--method", "PUT")
        if self.state() != "active":
            raise TransitionBlocked("deployment_admission_restore_unconfirmed")
