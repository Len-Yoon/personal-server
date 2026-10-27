#!/usr/bin/env python3
"""Render new immutable Book recovery staging resources; never access a cluster.

The disposable Pod reads one approved existing encrypted backup using the existing
Book backup image and Secret references. Rendering grants no creation/data-access
authority: the operator must review and obtain the final scoped approval first.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re

NAMESPACE = "personal-server"
SECRET_NAME = "book-memo-pvc-backup-runtime"
DIGEST = re.compile(r"sha256:[a-f0-9]{64}\Z")
IMAGE = re.compile(r"(?:[a-z0-9][a-z0-9._:/-]{0,200}/)?personal-server-book-pvc-backup"
                   r"(?::[A-Za-z0-9._-]{1,128})?@sha256:[a-f0-9]{64}\Z")
EVIDENCE_KEYS = {"schema_version", "scope", "backup_status", "encrypted", "restore_status",
                 "source_runtime", "restore_check", "restore_path_check", "backup_completed_at",
                 "restore_verified_at", "evidence_expires_at", "backup_id", "artifact_digest", "source_digest"}

# This code runs only after the separately approved Pod creation. It imports no
# Kubernetes/backup controller and never mounts a source PVC or service token.
STAGE_CODE = r'''import hashlib,json,os,resource,shutil,signal,stat,subprocess,time
from datetime import datetime,timezone
from pathlib import Path

WORK=Path('/work')
MEMORY=Path('/config-memory')
SECRET=Path('/run/secrets/book-backup')
INPUT=Path('/stage')
CIPHER_LIMIT=128*1024*1024
PLAIN_LIMIT=256*1024*1024
CONFIG_LIMIT=1024*1024
REMOTE='gdrive:PersonalServer-encrypted-backups/book-memo/'

class StageError(RuntimeError): pass

def require(condition,code):
    if not condition: raise StageError(code)

def utc(): return datetime.now(timezone.utc).isoformat()

def digest(path):
    result=hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda:source.read(1024*1024),b''): result.update(block)
    return 'sha256:'+result.hexdigest()

def memory_backed(path):
    mounts=[]
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        left,right=line.split(' - ',1)
        mount=Path(left.split()[4])
        if path.is_relative_to(mount): mounts.append((len(mount.parts),right.split()[0]))
    return bool(mounts) and max(mounts)[1]=='tmpfs'

def config_snapshot(path,projected=False):
    descriptor=os.open(path,os.O_RDONLY|(0 if projected else os.O_NOFOLLOW))
    with os.fdopen(descriptor,'rb') as source:
        info=os.fstat(source.fileno())
        require(stat.S_ISREG(info.st_mode) and 0<info.st_size<=CONFIG_LIMIT,'encrypted_config_invalid')
        if not projected:
            require(info.st_uid==os.getuid() and stat.S_IMODE(info.st_mode)==0o600,'encrypted_config_invalid')
        raw=source.read(CONFIG_LIMIT+1)
    lines=[line.strip() for line in raw.splitlines() if line.strip() and not line.strip().startswith((b'#',b';'))]
    require(len(raw)<=CONFIG_LIMIT and bool(lines) and lines[0]==b'RCLONE_ENCRYPT_V0:','encrypted_config_invalid')
    return raw

def capped_command(command,limit,timeout):
    def cap():
        old=resource.getrlimit(resource.RLIMIT_FSIZE)
        resource.setrlimit(resource.RLIMIT_FSIZE,tuple(limit if value==resource.RLIM_INFINITY else min(value,limit) for value in old))
    process=None
    try:
        process=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,start_new_session=True,preexec_fn=cap,cwd=WORK,
            env={'PATH':'/usr/local/bin:/usr/bin:/bin','HOME':str(WORK),'LC_ALL':'C','TMPDIR':str(WORK)})
        require(process.wait(timeout=timeout)==0,'external_stage_failed')
    except (OSError,subprocess.TimeoutExpired):
        raise StageError('external_stage_failed') from None
    finally:
        if process is not None:
            try: os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError: pass
            process.wait()

def bounded_file(path,maximum):
    info=path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_nlink==1 and info.st_uid==os.getuid()
            and 0<info.st_size<=maximum,'artifact_size_or_type_invalid')
    return info.st_size

def phase_result(started_at,started):
    return {'started_at':started_at,'completed_at':utc(),'seconds':time.monotonic()-started}

def stage(request,runner=capped_command,hold=True):
    require(set(request)=={'schema_version','backup_id','artifact_digest','source_digest','evidence_sha256','code_sha256','image'},'request_invalid')
    require(type(request['schema_version']) is int and request['schema_version']==1,'request_invalid')
    import re
    require(re.fullmatch(r'book-[0-9]{8}T[0-9]{6}Z-[0-9]{1,10}',request['backup_id']) is not None,'request_invalid')
    for key in ('artifact_digest','source_digest','evidence_sha256','code_sha256'):
        require(re.fullmatch(r'sha256:[a-f0-9]{64}',request[key]) is not None,'request_invalid')
    require(digest(INPUT/'evidence.txt')==request['evidence_sha256'],'evidence_binding_failed')
    require(digest(INPUT/'stage.py')==request['code_sha256'],'code_binding_failed')
    require(memory_backed(MEMORY),'memory_config_required')
    work_info,memory_info=WORK.lstat(),MEMORY.lstat()
    require(stat.S_ISDIR(work_info.st_mode) and stat.S_ISDIR(memory_info.st_mode)
            and not any(WORK.iterdir()) and not any(MEMORY.iterdir()),'fresh_scratch_required')
    for key in ('rclone-config-passphrase','age-identity'):
        info=(SECRET/key).stat()
        require(stat.S_ISREG(info.st_mode) and 0<info.st_size<=65536,'secret_reference_invalid')
    # The Secret bytes stay projected/read-only; only the encrypted config is
    # copied into a dedicated bounded tmpfs to permit encrypted token refresh.
    raw=config_snapshot(SECRET/'rclone-config',projected=True)
    config=MEMORY/'rclone.conf'
    ciphertext=WORK/'backup.tar.age'
    plaintext=WORK/'backup.tar'
    ready=WORK/'ready.json'
    try:
        with config.open('xb') as output:
            os.chmod(config,0o600)
            output.write(raw)
        acquisition_at,acquisition_tick=utc(),time.monotonic()
        try:
            runner(['/usr/bin/rclone','--config',str(config),'--password-command',
                '/usr/bin/cat /run/secrets/book-backup/rclone-config-passphrase',
                'copyto','--log-level','ERROR','--retries','1','--low-level-retries','1',
                '--max-transfer',str(CIPHER_LIMIT),'--cutoff-mode','hard','--buffer-size','0',
                '--multi-thread-streams','0',REMOTE+request['backup_id'][5:]+'.tar.age',str(ciphertext)],CIPHER_LIMIT,240)
        finally:
            config_snapshot(config)
        cipher_size=bounded_file(ciphertext,CIPHER_LIMIT)
        require(digest(ciphertext)==request['artifact_digest'],'ciphertext_digest_mismatch')
        acquisition=phase_result(acquisition_at,acquisition_tick)
        decrypt_at,decrypt_tick=utc(),time.monotonic()
        runner(['/usr/bin/age','-d','-i',str(SECRET/'age-identity'),'-o',str(plaintext),str(ciphertext)],PLAIN_LIMIT,240)
        plain_size=bounded_file(plaintext,PLAIN_LIMIT)
        plain_digest=digest(plaintext)
        decryption=phase_result(decrypt_at,decrypt_tick)
        result={'schema_version':1,'status':'ready','backup_id':request['backup_id'],
            'evidence_sha256':request['evidence_sha256'],'code_sha256':request['code_sha256'],
            'ciphertext_sha256':request['artifact_digest'],'source_tree_sha256':request['source_digest'],
            'plaintext_sha256':plain_digest,'ciphertext_bytes':cipher_size,'plaintext_bytes':plain_size,
            'preparation':{'acquisition':acquisition,'decryption':decryption},
            'staging_image_expected':request['image'],
            'ready_at':utc(),'hold_seconds':300,'service_recovery_verified':False,'total_rto_seconds':None}
        with ready.open('x',encoding='utf-8') as output:
            os.chmod(ready,0o600)
            json.dump(result,output,sort_keys=True)
        print(json.dumps(result,sort_keys=True),flush=True)
        if hold: time.sleep(300)
        return result
    finally:
        # Both emptyDirs were confirmed fresh before this invocation. Remove
        # all tool-created partial files and encrypted refresh temporaries, too.
        cleanup_failed=False
        for directory,initial in ((WORK,work_info),(MEMORY,memory_info)):
            try:
                current=directory.lstat()
                require(stat.S_ISDIR(current.st_mode) and (current.st_dev,current.st_ino)==
                        (initial.st_dev,initial.st_ino),'scratch_identity_changed')
                for path in directory.iterdir():
                    if path.is_dir() and not path.is_symlink(): shutil.rmtree(path)
                    else: path.unlink()
            except (OSError,StageError): cleanup_failed=True
        if cleanup_failed: raise StageError('private_scratch_cleanup_failed') from None

def main():
    os.umask(0o077)
    def interrupted(_number,_frame):
        signal.signal(signal.SIGTERM,signal.SIG_IGN)
        signal.signal(signal.SIGINT,signal.SIG_IGN)
        raise StageError('stage_interrupted')
    signal.signal(signal.SIGTERM,interrupted)
    signal.signal(signal.SIGINT,interrupted)
    try:
        request=json.loads((INPUT/'request.json').read_text())
        stage(request)
        print('stage_complete',flush=True)
        return 0
    except (StageError,OSError,ValueError,TypeError,KeyError) as error:
        print(json.dumps({'status':'failed','reason':str(error) if isinstance(error,StageError) else 'stage_failed'}),flush=True)
        return 1

if __name__=='__main__': raise SystemExit(main())
'''


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def render(image, evidence_path, evidence_sha256):
    if not isinstance(image, str) or not IMAGE.fullmatch(image):
        raise ValueError("pinned_book_backup_image_required")
    if not isinstance(evidence_sha256, str) or not DIGEST.fullmatch(evidence_sha256):
        raise ValueError("evidence_digest_required")
    path = Path(__file__).with_name("book-independent-restore.py")
    spec = importlib.util.spec_from_file_location("book_restore_stage_evidence", path)
    consumer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(consumer)
    evidence = consumer.evidence_input(evidence_path, evidence_sha256)
    if (set(evidence) != EVIDENCE_KEYS or not re.fullmatch(r"book-[0-9]{8}T[0-9]{6}Z-[0-9]{1,10}", evidence["backup_id"])):
        raise ValueError("production_book_evidence_required")
    # Reconstruct only the fixed validated schema, so arbitrary original bytes
    # can never be copied into the public code/evidence ConfigMap.
    with consumer.private_input(evidence_path, consumer.MAX_METADATA) as stream:
        evidence_raw = stream.read(consumer.MAX_METADATA + 1)
    if consumer.sha(evidence_raw) != evidence_sha256:
        raise ValueError("evidence_digest_mismatch")
    request = {"schema_version": 1, "backup_id": evidence["backup_id"],
               "artifact_digest": evidence["artifact_digest"], "source_digest": evidence["source_digest"],
               "evidence_sha256": evidence_sha256, "code_sha256": consumer.sha(STAGE_CODE.encode()), "image": image}
    data = {"stage.py": STAGE_CODE, "evidence.txt": evidence_raw.decode("utf-8"), "request.json": canonical(request).decode()}
    content_hash = hashlib.sha256(canonical(data)).hexdigest()
    name = "book-restore-stage-" + content_hash[:24]
    labels = {"app.kubernetes.io/name": "book-independent-restore-stage",
              "app.kubernetes.io/part-of": "book-recovery-verification"}
    annotations = {"personal-server/content-sha256": content_hash,
                   "personal-server/approval-required": "specific-source-read-and-disposable-staging-only"}
    metadata = {"name": name, "namespace": NAMESPACE, "labels": labels, "annotations": annotations}
    configmap = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": metadata, "immutable": True, "data": data}
    pod = {"apiVersion": "v1", "kind": "Pod", "metadata": dict(metadata), "spec": {
        "restartPolicy": "Never", "activeDeadlineSeconds": 900, "terminationGracePeriodSeconds": 15,
        "automountServiceAccountToken": False, "enableServiceLinks": False,
        "hostNetwork": False, "hostPID": False, "hostIPC": False,
        "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001,
                            "fsGroup": 10001, "fsGroupChangePolicy": "OnRootMismatch",
                            "seccompProfile": {"type": "RuntimeDefault"}},
        "containers": [{"name": "stage", "image": image, "imagePullPolicy": "Never",
            "command": ["/usr/local/bin/python", "-B", "/stage/stage.py"],
            "securityContext": {"runAsNonRoot": True, "readOnlyRootFilesystem": True,
                                "allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}},
            "resources": {"requests": {"cpu": "100m", "memory": "128Mi", "ephemeral-storage": "512Mi"},
                          "limits": {"cpu": "1", "memory": "1Gi", "ephemeral-storage": "1Gi"}},
            "volumeMounts": [{"name": "code", "mountPath": "/stage", "readOnly": True},
                             {"name": "credentials", "mountPath": "/run/secrets/book-backup", "readOnly": True},
                             {"name": "scratch", "mountPath": "/work"},
                             {"name": "config-memory", "mountPath": "/config-memory"}]}],
        "volumes": [{"name": "code", "configMap": {"name": name, "defaultMode": 0o444}},
                    {"name": "credentials", "secret": {"secretName": SECRET_NAME, "optional": False,
                         "defaultMode": 0o440, "items": [{"key": key, "path": key} for key in
                         ("rclone-config", "rclone-config-passphrase", "age-identity")]}},
                    {"name": "scratch", "emptyDir": {"sizeLimit": "512Mi"}},
                    {"name": "config-memory", "emptyDir": {"medium": "Memory", "sizeLimit": "16Mi"}}]}}
    return {"apiVersion": "v1", "kind": "List", "items": [configmap, pod]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--evidence-sha256", required=True)
    args = parser.parse_args()
    try:
        resources = render(args.image, args.evidence, args.evidence_sha256)
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, ImportError):
        print(json.dumps({"status": "render_failed", "reason": "invalid_pinned_image_or_private_backup_evidence"}))
        return 1
    print(json.dumps(resources, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
