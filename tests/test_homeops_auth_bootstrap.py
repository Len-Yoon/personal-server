"""Execute production entrypoint functions with isolated command boundaries."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tests.test_homeops_auth_supply import KEY, AUTH, resources

ROOT = Path(__file__).resolve().parents[1]


def secret_reader_fixture():
    deployment, secret = resources()
    return ("#!" + sys.executable + "\nimport sys,json\n" +
            f"deployment={deployment!r}\nsecret={secret!r}\n" +
            "print(json.dumps(deployment if 'deployment' in sys.argv else secret))\n")


class AuthEntrypointTests(unittest.TestCase):
    def invoke(self, script, mode, missing=False):
        name = "start_runtime_services" if script == "windows-bootstrap.sh" else "deploy_runtime_services"
        source = (ROOT / "scripts" / script).read_text()
        start = source.index(name + "() {")
        function = source[start:source.index("\n}\n", start) + 3]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            deployment, secret = resources()
            if missing:
                secret["data"].pop(KEY)
            boundary = (
                "#!" + sys.executable + "\nimport os,sys,json,hashlib\n"
                f"deployment={deployment!r}\nsecret={secret!r}\n"
                "if os.path.basename(sys.argv[0]) == 'sudo':\n"
                " print(json.dumps(deployment if 'deployment' in sys.argv else secret))\n"
                "else:\n"
                " with open(os.environ['CALLS'], 'a') as stream:\n"
                "  stream.write(json.dumps({'args':sys.argv[1:], 'auth_matches':"
                f"os.environ.get({KEY!r}) == {AUTH!r}" + "})+'\\n')\n"
            )
            for name in ("sudo", "docker"):
                path = bin_dir / name
                path.write_text(boundary)
                path.chmod(0o755)
            env = dict(os.environ)
            env.pop(KEY, None)
            env.update(PATH=str(bin_dir) + os.pathsep + env["PATH"], CALLS=str(root / "calls"),
                       SCRIPT_DIR=str(ROOT / "scripts"), PORTAL_BRIDGE_COMPOSE_FILE="bridge.yml",
                       PORTAL_RUNTIME_MODE=mode)
            stubs = """
load_portal_runtime_mode() { :; }
validate_docker_bridge_gateway() { :; }
all_crawler_services_compose() { return 1; }
runtime_service_mode() { printf k3s; }
set_cutover_homeops_lists() { :; }
resolve_crawler_worker_caddy_upstream() { :; }
resolve_book_memo_caddy_upstream() { :; }
resolve_youtube_memo_caddy_upstream() { :; }
"""
            result = subprocess.run(["bash", "-c", "set -euo pipefail\n" + stubs + function + "\n" +
                                     ("start_runtime_services" if script == "windows-bootstrap.sh" else "deploy_runtime_services")],
                                    cwd=root, env=env, text=True, capture_output=True)
            self.assertNotIn(AUTH, result.stdout + result.stderr)
            calls = [json.loads(line) for line in (root / "calls").read_text().splitlines()] if (root / "calls").exists() else []
            return result, calls

    def test_supported_entrypoints_supply_auth_before_executor_recreation(self):
        for script in ("windows-bootstrap.sh", "deploy-n100.sh"):
            for mode in ("k3s", "cutover"):
                with self.subTest(script=script, mode=mode):
                    result, calls = self.invoke(script, mode)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    executor = [call for call in calls if "homeops-executor" in call["args"]]
                    self.assertEqual(len(executor), 1)
                    self.assertTrue(executor[0]["auth_matches"])
                    self.assertNotIn("portal-web", executor[0]["args"])
                    self.assertFalse([call for call in calls if "caddy" in call["args"]][0]["auth_matches"])
                    if script == "deploy-n100.sh":
                        self.assertTrue([call for call in calls if "config" in call["args"]][0]["auth_matches"])

    def test_missing_canonical_auth_blocks_every_compose_mutation(self):
        for script in ("windows-bootstrap.sh", "deploy-n100.sh"):
            for mode in ("k3s", "cutover"):
                with self.subTest(script=script, mode=mode):
                    result, calls = self.invoke(script, mode, missing=True)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
