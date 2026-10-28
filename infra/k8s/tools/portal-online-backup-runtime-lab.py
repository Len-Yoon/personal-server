#!/usr/bin/env python3
"""Disposable GitHub-hosted Docker read-only SQLite runtime lab.

No production resource, engine restart, reusable volume, or image mutation.
"""
import argparse
import inspect
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import time
import uuid


LABEL = "ci.portal-readonly.owner"


class SafetyError(RuntimeError):
    pass


def check(condition):
    if not condition:
        raise SafetyError("runtime safety condition failed")


def validate_request(args, environ):
    check(environ.get("GITHUB_ACTIONS") == "true")
    check(environ.get("RUNNER_ENVIRONMENT") == "github-hosted")
    check(not any(key in environ for key in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH")))
    check(re.fullmatch(r"sha256:[0-9a-f]{64}", args.image_config) is not None)
    check(re.fullmatch(r"[0-9a-f]{40}", args.source_sha) is not None)
    check(re.fullmatch(r"[0-9]+", args.run_id) is not None)
    check(re.fullmatch(r"[0-9]+", args.attempt) is not None)
    check(environ.get("GITHUB_RUN_ID") == args.run_id)
    check(environ.get("GITHUB_RUN_ATTEMPT") == args.attempt)


def sqlite_wal_reset_fixed(version):
    """SQLite WAL-reset fixes, including the two documented backport lines.

    Reference: https://www.sqlite.org/wal.html#wal_reset_bug
    """
    if not isinstance(version, str):
        return False
    parts = version.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return False
    value = tuple(int(part) for part in parts)
    return (value >= (3, 51, 3) or (3, 50, 7) <= value < (3, 51, 0)
            or (3, 44, 6) <= value < (3, 45, 0))


SQLITE_GATE = "import sqlite3\n" + inspect.getsource(sqlite_wal_reset_fixed) + "\nassert sqlite_wal_reset_fixed(sqlite3.sqlite_version), 'unpatched_sqlite_runtime'\n"
SQLITE_PROBE = "import sqlite3,json\nprint(json.dumps({'sqlite_version':sqlite3.sqlite_version}))\n"


WRITER = SQLITE_GATE + r'''
import hashlib,json,os,pathlib,shutil,sqlite3,time
root=pathlib.Path('/source');connections={}
for name in ('active','idle','closed'):
 p=root/name;p.mkdir(mode=0o700);(p/'files').mkdir(mode=0o700);(p/'state').mkdir(mode=0o700)
 (p/'files'/'ack.txt').write_bytes(b'pre-capture acknowledged')
 if name=='active':
  with (p/'files'/'fixture.txt').open('wb') as f:
   for _ in range(32):f.write(b'x'*1024*1024)
 db=sqlite3.connect(p/'state'/'homeops.sqlite3');db.execute('PRAGMA journal_mode=WAL');db.execute('PRAGMA wal_autocheckpoint=0')
 db.execute('CREATE TABLE records (value INTEGER)');db.execute('INSERT INTO records VALUES (0)');db.commit()
 if name=='closed':db.close()
 else:connections[name]=db
(root/'ready').write_text(json.dumps({'sqlite_version':sqlite3.sqlite_version}))
try:
 while not (root/'stop').exists():
  db=connections['active'];db.execute('INSERT INTO records VALUES (1)');db.commit();time.sleep(.002)
finally:
 for db in connections.values():db.close()
 for child in root.iterdir():
  if child.is_dir():shutil.rmtree(child)
  else:child.unlink()
'''


READER = SQLITE_GATE + r'''
import hashlib,importlib.util,json,os,pathlib,sqlite3,sys,tempfile,time
from contextlib import closing
case=sys.argv[1];root=pathlib.Path('/source')/case;db=root/'state'/'homeops.sqlite3'
assert os.statvfs(root).f_flag & os.ST_RDONLY
def fingerprint():
 return [(str(p.relative_to(root)),p.stat().st_size,p.stat().st_mtime_ns,p.stat().st_ctime_ns,hashlib.sha256(p.read_bytes()).hexdigest()) for p in sorted(root.rglob('*')) if p.is_file()]
def count():
 with closing(sqlite3.connect(db.as_uri()+'?mode=ro',uri=True,timeout=.25)) as c:return c.execute('SELECT count(*) FROM records').fetchone()[0]
before=fingerprint() if case=='closed' else None
sidecars=[pathlib.Path(str(db)+s).exists() for s in ('-wal','-shm')]
assert sidecars==([False,False] if case=='closed' else [True,True])
spec=importlib.util.spec_from_file_location('snapshot','/opt/personal-server/portal-online-snapshot.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
result={'case':case,'source_readonly':True,'sidecars_present':all(sidecars),'sqlite_version':sqlite3.sqlite_version,'status':'FAIL'}
with tempfile.TemporaryDirectory() as out:
 try:
  initial=count() if case=='active' else None
  m.capture(root/'files',root/'state',out)
 except (OSError,sqlite3.Error,m.CaptureError):
  assert case=='closed' and not list(pathlib.Path(out).iterdir())
  result.update(status='SAFE_FAIL',incomplete_publish=0,source_unchanged=fingerprint()==before)
  assert result['source_unchanged']
 else:
  assert (pathlib.Path(out)/'files'/'ack.txt').read_bytes()==b'pre-capture acknowledged'
  with closing(sqlite3.connect(pathlib.Path(out)/'portal-web-state'/'homeops.sqlite3')) as restored:
   assert restored.execute('PRAGMA quick_check').fetchone()==('ok',)
   assert restored.execute('SELECT count(*) FROM records WHERE value=0').fetchone()[0]==1
  result.update(status='PASS',pre_capture_acknowledgements=2,integrity='PASS')
  if case=='active':
   final=count();assert final>initial;result['concurrent_commits']=final-initial
  if case=='closed':
   result['source_unchanged']=fingerprint()==before;assert result['source_unchanged']
print(json.dumps(result,sort_keys=True))
'''


class Runtime:
    def __init__(self, args, *, execute=subprocess.run, clock=time.monotonic):
        self.args, self.execute, self.clock = args, execute, clock
        self.deadline = clock() + 50  # Leave a bounded cleanup reserve.
        self.cleanup_deadline = self.deadline + 10
        self.owner = f"{args.run_id}-{args.attempt}-{uuid.uuid4().hex}"
        self.containers = []
        self.mounts = {}
        self.cleaning = False

    def docker(self, arguments, *, allow_failure=False):
        remaining = (self.cleanup_deadline if self.cleaning else self.deadline) - self.clock()
        check(remaining > 0)
        try:
            result = self.execute(["docker", "--context", "default", *arguments], capture_output=True,
                                  text=True, timeout=min(12, remaining))
        except (OSError, subprocess.TimeoutExpired):
            raise SafetyError("Docker operation unavailable") from None
        if not allow_failure:
            check(result.returncode == 0)
        return result

    def preflight(self):
        check(self.docker(["context", "show"]).stdout.strip() == "default")
        check(self.docker(["context", "inspect", "default", "--format", "{{.Endpoints.docker.Host}}"]).stdout.strip() == "unix:///var/run/docker.sock")
        image = json.loads(self.docker(["image", "inspect", self.args.image_config]).stdout)[0]
        check(image["Id"] == self.args.image_config)
        check(image["Config"]["Labels"]["org.opencontainers.image.revision"] == self.args.source_sha)

    def inspect_owned(self, name):
        value = json.loads(self.docker(["container", "inspect", name]).stdout)[0]
        check(value["Name"] == "/" + name)
        check(value["Config"]["Labels"][LABEL] == self.owner)
        check(value["Image"] == self.args.image_config)
        check(value["Config"]["User"] == "10001:10001")
        host = value["HostConfig"]
        check(host["NetworkMode"] == "none" and host["ReadonlyRootfs"])
        check(host["Memory"] == 256 * 1024**2 and host["NanoCpus"] == 10**9 and host["PidsLimit"] == 64)
        check("ALL" in host["CapDrop"] and not host.get("PortBindings"))
        check("no-new-privileges" in host.get("SecurityOpt", []))
        source, readonly = self.mounts[name]
        mounts = [mount for mount in value["Mounts"] if mount["Destination"] == "/source"]
        check(len(mounts) == 1 and mounts[0]["Type"] == "bind" and mounts[0]["Source"] == str(source))
        check(mounts[0]["RW"] is not readonly)
        check(not any(mount["Type"] == "bind" and mount["Destination"] != "/source" for mount in value["Mounts"]))
        return value

    def create(self, source, role, code, *arguments):
        name = "portal-ro-" + role + "-" + uuid.uuid4().hex
        readonly = role not in ("writer", "cleanup")
        mount = f"type=bind,source={source},target=/source" + (",readonly" if readonly else "")
        # Register before create: a daemon timeout may have created the resource.
        self.containers.append(name)
        self.mounts[name] = (source, readonly)
        self.docker(["create", "--name", name, "--label", LABEL + "=" + self.owner,
                     "--network", "none", "--user", "10001:10001", "--read-only", "--cap-drop", "ALL",
                     "--security-opt", "no-new-privileges", "--memory", "256m", "--cpus", "1", "--pids-limit", "64",
                     "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=768m,mode=1777", "--mount", mount,
                     "--env", "PYTHONDONTWRITEBYTECODE=1", "--entrypoint", "python3", self.args.image_config,
                     "-B", "-c", code, *arguments])
        self.inspect_owned(name)
        return name

    def cleanup(self):
        self.cleaning = True
        failed = False
        for name in reversed(self.containers):
            try:
                found = self.docker(["container", "ls", "-aq", "--filter", "name=^/" + name + "$"], allow_failure=True)
                check(found.returncode == 0)
                if found.stdout.strip():
                    self.inspect_owned(name)
                    self.docker(["container", "rm", "-f", name])
            except (SafetyError, OSError, ValueError, KeyError):
                failed = True
        check(not failed)


def run(args, *, execute=subprocess.run):
    validate_request(args, os.environ)
    runtime = Runtime(args, execute=execute)
    evidence = {"status": "FAIL", "source_sha": args.source_sha, "image_config": args.image_config,
                "scope": "synthetic-readonly-container-runtime", "cases": [], "cleanup": "unconfirmed"}
    # Parent root is writable solely to allow UID 10001 to create its private dirs.
    temporary = Path(tempfile.mkdtemp(prefix="portal-readonly-ci-"))
    temporary.chmod(0o777)
    writer = None
    try:
        runtime.preflight()
        # Probe before starting any WAL writer; reject unpatched runtime images.
        probe = runtime.create(temporary, "probe", SQLITE_PROBE)
        sqlite_probe = json.loads(runtime.docker(["start", "-a", probe]).stdout)
        evidence["sqlite_version"] = sqlite_probe["sqlite_version"]
        evidence["sqlite_wal_reset_fixed"] = sqlite_wal_reset_fixed(evidence["sqlite_version"])
        check(evidence["sqlite_wal_reset_fixed"])
        writer = runtime.create(temporary, "writer", WRITER)
        runtime.docker(["start", writer])
        for _ in range(60):
            ready = runtime.docker(["exec", writer, "python3", "-c", "import pathlib,sys;p=pathlib.Path('/source/ready');sys.exit(1) if not p.exists() else print(p.read_text())"], allow_failure=True)
            if ready.returncode == 0:
                break
            time.sleep(.05)
        else:
            raise SafetyError("writer readiness deadline exceeded")
        evidence["writer_sqlite_version"] = json.loads(ready.stdout)["sqlite_version"]
        check(evidence["writer_sqlite_version"] == evidence["sqlite_version"] and sqlite_wal_reset_fixed(evidence["writer_sqlite_version"]))
        for case in ("active", "idle", "closed"):
            reader = runtime.create(temporary, "reader", READER, case)
            output = runtime.docker(["start", "-a", reader])
            value = json.loads(output.stdout)
            check(value["case"] == case and value["source_readonly"] is True)
            check(value["sqlite_version"] == evidence["sqlite_version"] and sqlite_wal_reset_fixed(value["sqlite_version"]))
            check(value["status"] == "PASS" or case == "closed" and value["status"] == "SAFE_FAIL")
            check(runtime.inspect_owned(reader)["State"]["ExitCode"] == 0)
            evidence["cases"].append(value)
        # The same RW writer must still be able to commit after all RO readers.
        runtime.docker(["exec", writer, "python3", "-c", "import sqlite3; c=sqlite3.connect('/source/closed/state/homeops.sqlite3');c.execute('INSERT INTO records VALUES (2)');c.commit();c.close()"])
        evidence["rw_writer_recovered"] = True
        evidence["status"] = "PASS"
    finally:
        # Ask the owner UID to close all databases and remove its private fixtures.
        runtime.cleaning = True
        cleanup_ok = True
        try:
            if writer is not None and runtime.inspect_owned(writer)["State"]["Running"]:
                runtime.docker(["exec", writer, "python3", "-c", "import pathlib;pathlib.Path('/source/stop').touch()"])
                result = runtime.docker(["wait", writer])
                check(result.stdout.strip() == "0")
            if list(temporary.iterdir()):
                # Covers partial setup failure; never chown or use host sudo.
                cleaner = runtime.create(temporary, "cleanup", "import pathlib,shutil;[(shutil.rmtree(p) if p.is_dir() else p.unlink()) for p in pathlib.Path('/source').iterdir()]")
                runtime.docker(["start", "-a", cleaner])
        except (SafetyError, OSError, ValueError, KeyError):
            cleanup_ok = False
        try:
            runtime.cleanup()
            check(not list(temporary.iterdir()))
            temporary.rmdir()
        except (SafetyError, OSError, ValueError, KeyError):
            cleanup_ok = False
        if cleanup_ok:
            evidence["cleanup"] = "PASS"
        else:
            evidence["status"] = "FAIL"
            evidence["cleanup"] = "unconfirmed"
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(evidence, sort_keys=True, indent=2) + "\n")
    check(evidence["status"] == "PASS" and evidence["cleanup"] == "PASS")
    return evidence


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--image-config", required=True)
    result.add_argument("--source-sha", required=True)
    result.add_argument("--run-id", required=True)
    result.add_argument("--attempt", required=True)
    result.add_argument("--output", type=Path, required=True)
    return result


def main():
    args = parser().parse_args()
    def cancel(_signum, _frame):
        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(signum, signal.SIG_IGN)
        raise SafetyError("runtime lab cancelled")
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, cancel)
    try:
        run(args)
    except (SafetyError, OSError, ValueError, KeyError, TypeError):
        print("portal_readonly_runtime=FAIL")
        return 1
    print("portal_readonly_runtime=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
