#!/usr/bin/env python3
"""Publish an unchanged, validated Portal ConfigMap snapshot for a read-only consumer.

Operator-run only: no scheduler, credential access, backup creation or expiry refresh.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location('portal_evidence_validator', HERE/'validate-backup-evidence.py')
VALIDATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VALIDATOR)
NAME = 'portal-backup.evidence'
PROOFS = {'source_runtime': 'k3s-pvc', 'restore_check': 'sqlite_quick_check', 'restore_path_check': 'success'}


def validate(raw: bytes, now: datetime) -> dict[str, str]:
    if not raw or len(raw) > 16384:
        raise ValueError('evidence size rejected')
    with tempfile.TemporaryDirectory(prefix='portal-evidence-validation-') as temp:
        path = Path(temp)/'evidence';path.write_bytes(raw)
        values = VALIDATOR.parse_evidence(path)
    VALIDATOR.validate_evidence(values, now, 86400)
    if any(values.get(key) != value for key,value in PROOFS.items()):
        raise ValueError('explicit restore proof missing')
    for key in ('artifact_digest','source_digest'):
        if not VALIDATOR.ARTIFACT_DIGEST_RE.fullmatch(values.get(key,'')):
            raise ValueError('explicit digest missing')
    return values


def decode_configmap(raw: bytes) -> bytes:
    if len(raw)>65536:
        raise ValueError('ConfigMap too large')
    doc=json.loads(raw)
    if not isinstance(doc,dict) or not isinstance(doc.get('metadata'),dict) or not isinstance(doc.get('data'),dict):
        raise ValueError('unexpected ConfigMap schema')
    metadata=doc.get('metadata',{})
    if metadata.get('name')!='portal-pvc-backup-evidence' or metadata.get('namespace')!='personal-server':
        raise ValueError('unexpected evidence source')
    text=doc.get('data',{}).get('evidence')
    if not isinstance(text,str) or not text or len(text.encode('utf-8'))>16384:
        raise ValueError('evidence missing or too large')
    return text.encode('utf-8')


def assert_directory(directory: Path, create: bool, temporary: str | None = None) -> None:
    if not directory.is_absolute():
        raise ValueError('absolute output directory required')
    for ancestor in (directory, *directory.parents):
        if ancestor.is_symlink():
            raise ValueError('symlink output ancestor')
    if not directory.exists() and create:
        directory.mkdir(mode=0o755,parents=True)
    if directory.is_symlink():
        raise ValueError('symlink output directory')
    if directory.exists():
        info=directory.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o022:
            raise ValueError('untrusted output directory')
        if any(entry.name not in {NAME, temporary} for entry in directory.iterdir()):
            raise ValueError('output directory is not dedicated to evidence')
    target=directory/NAME
    if target.is_symlink():
        raise ValueError('symlink output file')
    if target.exists():
        info=target.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=os.getuid():
            raise ValueError('untrusted output file')


def publish(directory: Path, raw: bytes, now: datetime) -> dict[str,str]:
    values=validate(raw,now)
    assert_directory(directory,True)
    directory.chmod(0o755)
    fd,name=tempfile.mkstemp(prefix='.evidence-',dir=directory)
    try:
        with os.fdopen(fd,'wb') as file:
            file.write(raw);file.flush();os.fchmod(file.fileno(),0o444);os.fsync(file.fileno())
        assert_directory(directory,False,Path(name).name)
        os.replace(name,directory/NAME)
        descriptor=os.open(directory,os.O_RDONLY)
        try:os.fsync(descriptor)
        finally:os.close(descriptor)
    finally:
        Path(name).unlink(missing_ok=True)
    return values


def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',required=True,type=Path)
    parser.add_argument('--check',action='store_true',help='validate source and destination without publishing')
    args=parser.parse_args()
    try:
        process=subprocess.run(['sudo','-n','k3s','kubectl','--request-timeout=15s','-n','personal-server','get','configmap','portal-pvc-backup-evidence','-o','json'],capture_output=True,timeout=20,check=True)
        raw=decode_configmap(process.stdout)
        now=datetime.now(timezone.utc)
        if args.check:
            validate(raw,now);assert_directory(args.output_dir,False)
        else:
            publish(args.output_dir,raw,now)
    except (ValueError,OSError,subprocess.SubprocessError):
        print('portal_backup_evidence_export=FAIL')
        return 1
    print('portal_backup_evidence_export=PASS mode='+('check' if args.check else 'snapshot'))
    return 0

if __name__=='__main__':
    raise SystemExit(main())
