#!/usr/bin/env python3
"""Mac-only, private-network TLS/SCRAM PG integration for all real applications.

Creates only UUID-labelled local fixtures. No published ports, real credentials,
production data, V2 objects, Windows control or N100 access. Image build context
is an explicit source allowlist and excludes .git/.env/data/host credentials.
"""
import json
import hashlib
import os
import re
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[3]
SERVICES = ["portal-web", "book-memo", "youtube-memo", "crawler-worker", "car-care-worker", "homeops-executor", "system-agent"]
GROUPS = {"311": SERVICES[:4], "312": SERVICES[4:]}


class FixtureError(RuntimeError):
    pass


def render_runner(base):
    if not re.fullmatch(r"python:3\.(?:11|12)-alpine3\.24@sha256:[0-9a-f]{64}", base):
        raise FixtureError("immutable_approved_app_base_required")
    template = (ROOT / "infra/k8s/ha-lab/Dockerfile.template").read_text()
    if (template.count("@APP_BASE_IMAGE@") != 1 or not template.startswith("FROM @APP_BASE_IMAGE@\n")
            or len(re.findall(r"(?im)^\s*FROM\s+", template)) != 1 or "ARG BASE_IMAGE" in template):
        raise FixtureError("invalid_runner_template")
    return template.replace("@APP_BASE_IMAGE@", base)


def approved_base(group):
    if group not in GROUPS:
        raise FixtureError("unknown_runner_group")
    source = ROOT / ("portal-web" if group == "311" else "system-agent") / "Dockerfile"
    declarations = re.findall(r"(?im)^\s*FROM\s+([^\r\n]+)", source.read_text())
    if len(declarations) != 1:
        raise FixtureError("single_approved_app_base_required")
    base = declarations[0].strip()
    render_runner(base)
    return base


class Fixture:
    def __init__(self, directory):
        self.directory = directory
        self.environment = {k: v for k, v in os.environ.items() if not k.startswith("DOCKER_")}
        self.owner = str(uuid.uuid4())
        self.label = "personal-server.ha-app-fixture=" + self.owner
        self.phase = "local_context"
        self.report = {"scope": "local_actual_apps_pg_only", "production_access": False, "services": {}}

    def docker(self, *args, input=None, timeout=60):
        result = subprocess.run(["docker", "--context", "orbstack", *args], input=input,
                                env=self.environment, text=True, capture_output=True, timeout=timeout)
        if result.returncode:
            if args[0] == "exec":
                try:
                    diagnostic = json.loads(result.stdout.splitlines()[-1])
                    frames = diagnostic.get("frames", [])
                    if diagnostic.get("status") == "FAIL" and isinstance(frames, list):
                        self.report["failure_location"] = {
                            "error_class": diagnostic.get("error_class"),
                            "runtime_reason": diagnostic.get("runtime_reason"),
                            "frames": [{key: frame[key] for key in ("file", "function", "line") if key in frame}
                                       for frame in frames if isinstance(frame, dict)],
                        }
                except (ValueError, IndexError, TypeError):
                    pass
            # Retain only static error categories, never SQL, passwords or data.
            categories = [s for s in ("permission denied", "does not exist", "syntax error", "authentication failed", "certificate verify failed", "AssertionError", "DatabaseUnavailable", "ModuleNotFoundError")
                          if s.lower() in result.stderr.lower()]
            raise FixtureError(self.phase + ":" + args[0] + ":" + ",".join(categories))
        return (result.stdout + (result.stderr if args[0] == "logs" else "")).strip()

    def owned(self, kind, identifier):
        value = json.loads(self.docker(kind, "inspect", identifier))[0]
        labels = value.get("Config", {}).get("Labels", {}) if kind in {"container", "image"} else value.get("Labels", {})
        if value["Id"] != identifier or labels.get("personal-server.ha-app-fixture") != self.owner:
            raise FixtureError("fixture_ownership_mismatch")

    def wait(self, predicate, seconds=40):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                if predicate():
                    return
            except FixtureError:
                pass
            time.sleep(0.2)
        node = getattr(self, "b", None) or getattr(self, "a", None)
        if node:
            logs = self.docker("logs", "--tail", "40", node)
            self.report["startup_flags"] = {key: phrase.lower() in logs.lower() for key, phrase in {
                "replication_auth": "password authentication failed", "hba": "no pg_hba.conf entry",
                "permission": "Permission denied", "missing_file": "No such file or directory",
                "certificate": "certificate verify failed", "basebackup": "pg_basebackup: error",
            }.items()}
        raise FixtureError(self.phase + ":deadline")

    def psql(self, statement, database="ha"):
        self.owned("container", self.a)
        return self.docker("exec", "-i", self.a, "psql", "-XAt", "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d", database,
                           input=statement, timeout=20)

    def certificate(self, name):
        fixture = self.directory / "fixture"
        key, crt, csr, extensions = [fixture / (name + suffix) for suffix in (".key", ".crt", ".csr", ".ext")]
        extensions.write_text("subjectAltName=DNS:" + name + "\nextendedKeyUsage=serverAuth\n")
        for command in (
            ["openssl", "req", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(csr), "-subj", "/CN=" + name],
            ["openssl", "x509", "-req", "-in", str(csr), "-CA", str(fixture / "ca.crt"), "-CAkey", str(fixture / "ca.key"), "-CAcreateserial", "-out", str(crt), "-days", "2", "-extfile", str(extensions)],
        ):
            subprocess.run(command, check=True, capture_output=True, timeout=20)
        key.chmod(0o600)

    def prepare(self):
        contexts = json.loads(self.docker("context", "inspect", "orbstack"))
        if contexts[0]["Endpoints"]["docker"]["Host"] != "unix://" + str(Path.home() / ".orbstack/run/docker.sock"):
            raise FixtureError("local_context_required")
        fixture = self.directory / "fixture"
        fixture.mkdir(mode=0o700)
        (fixture / "config").mkdir(mode=0o700)
        (fixture / "objects").mkdir(mode=0o700)
        (fixture / "results").mkdir(mode=0o700)
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(fixture / "ca.key"), "-out", str(fixture / "ca.crt"), "-days", "2", "-subj", "/CN=Isolated HA Test CA"], check=True, capture_output=True, timeout=20)
        (fixture / "ca.key").chmod(0o600)
        (fixture / "ca.crt").chmod(0o644)
        self.certificate("ha-a")
        self.certificate("ha-b")
        self.passwords = {"postgres": secrets.token_urlsafe(32), "ha_repl": secrets.token_urlsafe(32)}
        for service in SERVICES:
            for node in ("a", "b"):
                self.passwords[service.replace("-", "_") + "_" + node] = secrets.token_urlsafe(32)
        (fixture / "postgres-password").write_text(self.passwords["postgres"])
        (fixture / "postgres-password").chmod(0o600)
        (fixture / "repl.pgpass").write_text("ha-a:5432:replication:ha_repl:" + self.passwords["ha_repl"] + "\n")
        (fixture / "repl.pgpass").chmod(0o600)
        self.network = self.docker("network", "create", "--internal", "--label", self.label, "ha-app-" + self.owner)
        self.owned("network", self.network)
        self.image = self.docker("image", "inspect", "postgres:17-bookworm", "--format", "{{.Id}}")

    def start_pg(self, node):
        fixture = self.directory / "fixture"
        if node == "A":
            command = "chown 999:999 /tmp/server.key /tmp/server.crt /tmp/pgpassword; exec docker-entrypoint.sh postgres \"$@\""
        tuning = ["-c", "cluster_name=ha_" + node, "-c", "ssl=on", "-c", "ssl_cert_file=/tmp/server.crt", "-c", "ssl_key_file=/tmp/server.key",
                  "-c", "shared_buffers=32MB", "-c", "max_connections=40", "-c", "wal_level=replica", "-c", "max_wal_senders=4", "-c", "max_wal_size=64MB", "-c", "min_wal_size=32MB",
                  "-c", "wal_sender_timeout=2000", "-c", "wal_receiver_timeout=2000", "-c", "wal_retrieve_retry_interval=1000"]
        if node == "B":
            # Keep shell argument boundaries exact; no dynamic credentials in it.
            command = "chown 999:999 /tmp/server.key /tmp/server.crt /tmp/repl.pgpass; exec gosu postgres bash -ceu 'pg_basebackup -d \"host=ha-a user=ha_repl sslmode=verify-full sslrootcert=/tmp/ca.crt passfile=/tmp/repl.pgpass application_name=ha_b\" -D /var/lib/postgresql/data -R -X stream --checkpoint=fast; exec postgres -D /var/lib/postgresql/data \"$@\"' -- \"$@\""
        identifier = self.docker("create", "--pull=never", "--label", self.label, "--network", self.network, "--network-alias", "ha-" + node.lower(),
                                 "--memory", "384m", "--cpus", "1", "--tmpfs", "/var/lib/postgresql/data:rw,uid=999,gid=999,mode=0700,size=256m",
                                 "-e", "POSTGRES_PASSWORD_FILE=/tmp/pgpassword", "-e", "POSTGRES_INITDB_ARGS=--data-checksums", "--entrypoint", "bash", self.image,
                                 "-ceu", command, "--", *tuning)
        self.owned("container", identifier)
        for source, target in (("ha-" + node.lower() + ".key", "server.key"), ("ha-" + node.lower() + ".crt", "server.crt"), ("ca.crt", "ca.crt"), ("postgres-password", "pgpassword"), ("repl.pgpass", "repl.pgpass")):
            self.docker("cp", str(fixture / source), identifier + ":/tmp/" + target)
        self.docker("start", identifier)
        return identifier

    def databases(self):
        self.phase = "primary_start"
        self.a = self.start_pg("A")
        self.wait(lambda: self.docker("exec", self.a, "cat", "/proc/1/comm") == "postgres" and self.psql("SELECT 1", "postgres") == "1")
        self.psql("CREATE DATABASE ha", "postgres")
        self.psql("CREATE ROLE ha_repl LOGIN REPLICATION PASSWORD '" + self.passwords["ha_repl"] + "'")
        self.docker("exec", self.a, "bash", "-ceu", "printf '%s\\n' 'hostssl replication ha_repl all scram-sha-256' >> /var/lib/postgresql/data/pg_hba.conf")
        self.psql("SELECT pg_reload_conf()")
        self.psql((ROOT / "infra/k8s/ha_runtime/schema.sql").read_text())
        self.cluster = "local_" + self.owner.replace("-", "")
        self.psql("INSERT INTO ha.control VALUES(1,'" + self.cluster + "',1,1,'A','B',1,ARRAY['A'],'A','AS',true,true,clock_timestamp()+interval '2 hours')")
        for service in SERVICES:
            for node in ("A", "B"):
                role = service.replace("-", "_") + "_" + node.lower()
                self.psql("CREATE ROLE " + role + " LOGIN PASSWORD '" + self.passwords[role] + "'; GRANT USAGE ON SCHEMA ha TO " + role + "; GRANT EXECUTE ON FUNCTION ha.observe() TO " + role + "; INSERT INTO ha.identities VALUES('" + role + "','" + service + "','" + node + "')")
        self.system_id = self.psql("SELECT system_identifier::text FROM pg_control_system()")
        self.phase = "standby_start"
        self.b = self.start_pg("B")
        self.wait(lambda: self.docker("exec", self.b, "psql", "-XAt", "-U", "postgres", "-d", "ha", "-c", "SELECT pg_is_in_recovery()") == "t")
        self.psql("ALTER SYSTEM SET synchronous_standby_names='FIRST 1 (ha_b)'")
        self.psql("SELECT pg_reload_conf()")
        self.wait(lambda: self.psql("SELECT count(*) FROM pg_stat_replication WHERE application_name='ha_b' AND sync_state='sync'") == "1")
        self.report["authenticated_sync_pair"] = True
        self.configurations()

    def configurations(self):
        fixture = self.directory / "fixture"
        for role, password in self.passwords.items():
            path = fixture / (role + ".pgpass")
            path.write_text("ha-a:5432:ha:" + role + ":" + password + "\nha-b:5432:ha:" + role + ":" + password + "\n")
            path.chmod(0o600)
        def endpoint(role, host):
            return {"host": host, "port": 5432, "dbname": "ha", "user": role, "sslmode": "verify-full", "sslrootcert": "/fixture/ca.crt", "passfile": "/fixture/" + role + ".pgpass"}
        (fixture / "admin.json").write_text(json.dumps(endpoint("postgres", "ha-a")))
        (fixture / "admin.json").chmod(0o600)
        for service in SERVICES:
            for node in ("A", "B"):
                role = service.replace("-", "_") + "_" + node.lower()
                value = {"scope": "local-isolated", "cluster": self.cluster, "system_id": self.system_id, "node": node, "service": service,
                         "primary": endpoint(role, "ha-a"), "partner": endpoint(role, "ha-b")}
                path = fixture / "config" / (service + "-" + node + ".json")
                path.write_text(json.dumps(value))
                path.chmod(0o600)

    def build(self):
        self.phase = "build_context"
        context = self.directory / "context"
        (context / "source").mkdir(parents=True)
        (context / "requirements").mkdir()
        for service in SERVICES:
            shutil.copyfile(ROOT / service / "requirements.txt", context / "requirements" / (service + ".txt"))
        shutil.copyfile(ROOT / "infra/k8s/ha-lab/requirements.txt", context / "requirements/runtime.txt")
        files = []
        for service in SERVICES:
            files.append(ROOT / service / "requirements.txt")
            for path in (ROOT / service / "app").rglob("*"):
                relative = path.relative_to(ROOT / service / "app")
                if path.is_file() and not path.is_symlink() and "__pycache__" not in path.parts and (path.suffix == ".py" or relative.parts[0] in {"static", "templates"}):
                    files.append(path)
        files += [p for p in (ROOT / "infra/k8s/ha_runtime").iterdir() if p.is_file() and p.suffix in {".py", ".sql"}]
        files += [p for p in (ROOT / "infra/k8s/ha-lab").iterdir() if p.is_file() and p.suffix in {".py", ".txt"}]
        files.append(ROOT / "infra/k8s/tools/n100_ha_contract.py")
        files += [ROOT / "infra/k8s/ha-lab/Dockerfile.template", ROOT / "portal-web/Dockerfile", ROOT / "system-agent/Dockerfile"]
        for path in files:
            target = context / "source" / path.relative_to(ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
        fingerprint = hashlib.sha256()
        for path in sorted(files):
            fingerprint.update(str(path.relative_to(ROOT)).encode() + b"\0" + path.read_bytes())
        self.report["source_sha256"] = fingerprint.hexdigest()
        self.runners = {}
        for group, services in GROUPS.items():
            base = approved_base(group)
            (context / "Dockerfile").write_text(render_runner(base))
            tag = "n100-ha-app-fixture:" + self.owner + "-" + group
            self.phase = "build_" + group
            self.docker("build", "--label", self.label, "--build-arg", "SERVICE_GROUP=" + ",".join(services), "-t", tag, str(context), timeout=600)
            image_id = self.docker("image", "inspect", tag, "--format", "{{.Id}}")
            self.owned("image", image_id)
            runner = self.docker("create", "--label", self.label, "--network", self.network, "--memory", "768m", "--cpus", "2", image_id)
            self.owned("container", runner)
            self.docker("cp", str(self.directory / "fixture") + "/.", runner + ":/fixture")
            self.docker("start", runner)
            # docker cp may retain the Mac owner's numeric uid. These are
            # disposable synthetic credentials owned by the unprivileged runner.
            self.docker("exec", "--user", "0:0", runner, "chown", "-R", "10001:10001", "/fixture")
            self.runners[group] = runner

    def execute(self, service, node, phase):
        group = "311" if service in GROUPS["311"] else "312"
        runner = self.runners[group]
        script = "web_smoke.py" if service in {"portal-web", "book-memo", "youtube-memo"} else ("seed.py" if phase == "seed" else "app_smoke.py")
        command = ["exec", "-w", "/source/" + service,
                   "-e", "PYTHONPATH=/source/infra/k8s:/source/" + service, "-e", "APP_ENV=test", "-e", "HA_LOCAL_RUNTIME=1",
                   "-e", "HA_PG_CONFIG=/fixture/config/" + service + "-" + node + ".json",
                   "-e", "HA_FILE_OBJECT_ROOT=/fixture/objects", "-e", "HA_HOMEOPS_TARGET_NODE=" + node,
                   runner, "/venvs/" + service + "/bin/python", "/source/infra/k8s/ha-lab/" + script,
                   "--service", service]
        if script == "web_smoke.py":
            command += ["--phase", phase, "--node", node]
        elif phase != "seed":
            command += ["--node", node]
        self.phase = phase + "_" + service + "_" + node
        output = self.docker(*command, timeout=90)
        try:
            value = json.loads(output.splitlines()[-1])
        except (ValueError, IndexError):
            raise FixtureError(self.phase + ":invalid_result") from None
        if value.get("status") not in {"PASS", "pass"} and value.get("pass") is not True:
            raise FixtureError(self.phase + ":application_check_failed")
        return value

    def applications(self):
        for service in SERVICES:
            self.report["services"][service] = {"migration": self.execute(service, "A", "seed")}
            for component in ("main", "state", "homeops", "bookmarks"):
                schema = "s_" + service.replace("-", "_") + "_" + component
                roles = [service.replace("-", "_") + "_a", service.replace("-", "_") + "_b"]
                exists = self.psql("SELECT count(*) FROM pg_namespace WHERE nspname='" + schema + "'") == "1"
                if not exists:
                    continue
                role_list = ",".join(roles)
                self.psql("GRANT USAGE ON SCHEMA " + schema + " TO " + role_list + "; GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA " + schema + " TO " + role_list + "; REVOKE UPDATE,DELETE ON " + schema + ".ha_receipts," + schema + ".ha_barriers FROM " + role_list + "; GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA " + schema + " TO " + role_list)
        for service in SERVICES:
            self.report["services"][service]["node_A"] = self.execute(service, "A", "smoke")
        self.report["permits"] = [self.permit_check("active")]
        self.driver_faults()
        self.phase = "app_role_switch_only"
        self.psql("SELECT ha.local_roles(ARRAY['B'],'B',true)")
        self.report["permits"].append(self.permit_check("stale"))
        for service in SERVICES:
            self.report["services"][service]["node_B"] = self.execute(service, "B", "smoke")
        self.report["db_primary_retained"] = self.psql("SELECT primary_node FROM ha.control") == "A"

    def permit_check(self, phase):
        self.phase = "permit_" + phase
        output = self.docker("exec", "-w", "/source/crawler-worker", "-e", "PYTHONPATH=/source/infra/k8s:/source/crawler-worker",
                             "-e", "APP_ENV=test", "-e", "HA_LOCAL_RUNTIME=1", "-e", "HA_PG_CONFIG=/fixture/config/crawler-worker-A.json",
                             self.runners["311"], "/venvs/crawler-worker/bin/python", "/source/infra/k8s/ha-lab/permit_smoke.py", "--phase", phase, timeout=20)
        result = json.loads(output.splitlines()[-1])
        if result.get("status") != "PASS":
            raise FixtureError(self.phase + ":application_check_failed")
        return result

    def driver_faults(self):
        runner = self.runners["311"]
        def check(phase):
            self.phase = "driver_" + phase
            output = self.docker("exec", "-w", "/source/book-memo", "-e", "PYTHONPATH=/source/infra/k8s:/source/book-memo",
                                 "-e", "APP_ENV=test", "-e", "HA_LOCAL_RUNTIME=1", "-e", "HA_PG_CONFIG=/fixture/config/book-memo-A.json",
                                 runner, "/venvs/book-memo/bin/python", "/source/infra/k8s/ha-lab/retry_smoke.py", "--phase", phase, timeout=30)
            return json.loads(output.splitlines()[-1])
        self.phase = "driver_peer_disconnect"
        self.owned("container", self.b)
        self.docker("network", "disconnect", self.network, self.b)
        self.wait(lambda: self.psql("SELECT count(*) FROM pg_stat_replication") == "0")
        self.report["driver_faults"] = [check("cancelled"), check("retry_without_peer")]
        self.docker("network", "connect", "--alias", "ha-b", self.network, self.b)
        self.wait(lambda: self.psql("SELECT count(*) FROM pg_stat_replication WHERE application_name='ha_b' AND sync_state='sync'") == "1")
        self.report["driver_faults"].append(check("reprotected"))

    def cleanup(self):
        failed = False
        for kind, listing, removal in (
            ("container", ("ps", "-aq", "--no-trunc"), ("rm", "-f", "-v")),
            ("network", ("network", "ls", "-q", "--no-trunc"), ("network", "rm")),
            ("image", ("image", "ls", "-q", "--no-trunc"), ("image", "rm")),
        ):
            try:
                identifiers = self.docker(*listing, "--filter", "label=" + self.label).split()
            except Exception:
                failed = True
                continue
            for identifier in dict.fromkeys(identifiers):
                try:
                    self.owned(kind, identifier)
                    self.docker(*removal, identifier)
                except Exception:
                    failed = True
        return not failed


def main():
    with tempfile.TemporaryDirectory(prefix="n100-ha-apps-") as path:
        fixture = Fixture(Path(path).resolve())
        error = None
        try:
            fixture.prepare()
            fixture.databases()
            fixture.build()
            fixture.applications()
        except Exception as value:
            error = type(value).__name__ + ":" + (str(value) if isinstance(value, FixtureError) else fixture.phase)
        finally:
            cleaned = fixture.cleanup()
        fixture.report.update(status="FAIL" if error or not cleaned else "PASS", error=error, owned_fixture_removed=cleaned)
        print(json.dumps(fixture.report, sort_keys=True))
        return int(error is not None or not cleaned)


if __name__ == "__main__":
    raise SystemExit(main())
