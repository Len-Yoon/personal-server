"""Build an inert private Windows runtime; never install or register a service.

The package is not native ACL, Authenticode, SCM, VM or 2-second acceptance.
An administrator must verify these separately before enabling any controller.
"""
import argparse
import base64
import csv
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import stat
import zipfile

from . import prepare as source_bundle

PYTHON_FILE='python-3.13.17-embed-amd64.zip'
PYTHON_SHA256='9e35319bffedab8b36ad44d8f377cb4f1733bcec257a0b6d04d94f01486ddb38'
WHEELS={
    'psycopg-3.3.8-py3-none-any.whl':'ca50af6072ba9ea6866f33021975230c2555d7bfdb9d8f94ca3af5c3d1bf2167',
    'psycopg_binary-3.3.8-cp313-cp313-win_amd64.whl':'e42c0e37eaed1ff03c24008561bca218eae8ec5ca4405a37897c6245ead8d612',
    'tzdata-2026.2-py2.py3-none-any.whl':'bbe9af844f658da81a5f95019480da3a89415801f6cc966806612cc7169bffe7',
}
HEX=re.compile(r'[0-9a-f]{64}\Z')


class EnrollmentError(RuntimeError):
    pass


def fail():
    raise EnrollmentError('windows_runtime_input_unverified')


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def member_name(name):
    if (not isinstance(name,str) or not name or '\\' in name or ':' in name
            or any(ord(c)<32 for c in name) or name.startswith('/')
            or any(p in ('','.','..') or p.endswith((' ','.')) for p in name.split('/'))):
        fail()
    path=PurePosixPath(name)
    if any(p.split('.')[0].upper() in {'CON','PRN','AUX','NUL',*[f'COM{i}' for i in range(1,10)],*[f'LPT{i}' for i in range(1,10)]} for p in path.parts):
        fail()
    return path.as_posix()


def archive_files(raw):
    result={};seen=set();total=0
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            if len(archive.infolist())>8192:
                fail()
            for item in archive.infolist():
                if item.orig_filename!=item.filename:
                    fail()
                name=member_name(item.filename.rstrip('/') if item.is_dir() else item.filename)
                folded=name.casefold()
                if folded in seen or stat.S_ISLNK(item.external_attr>>16) or item.flag_bits&1:
                    fail()
                seen.add(folded)
                if item.is_dir():
                    continue
                total+=item.file_size
                if total>134217728 or item.file_size>67108864 or name.lower().endswith('.pth'):
                    fail()
                result[name]=archive.read(item)
    except (ValueError,zipfile.BadZipFile,RuntimeError):
        fail()
    return result


def verify_record(files):
    records=[name for name in files if name.endswith('.dist-info/RECORD')]
    if len(records)!=1:
        fail()
    record=records[0];seen=set()
    try:
        for row in csv.reader(io.StringIO(files[record].decode('utf-8'))):
            if len(row)!=3:
                fail()
            name,hash_value,size=row
            if name not in files or name in seen:
                fail()
            seen.add(name)
            if name==record:
                if hash_value or size:
                    fail()
            elif hash_value!='sha256='+base64.urlsafe_b64encode(hashlib.sha256(files[name]).digest()).decode().rstrip('=') or size!=str(len(files[name])):
                fail()
    except (UnicodeError,csv.Error):
        fail()
    if seen!=set(files):
        fail()


def assemble(artifacts):
    if set(artifacts)!={PYTHON_FILE,*WHEELS}:
        fail()
    for filename,expected in {PYTHON_FILE:PYTHON_SHA256,**WHEELS}.items():
        if sha(artifacts[filename])!=expected:
            fail()
    files=archive_files(artifacts[PYTHON_FILE]);seen={name.casefold() for name in files}
    if not {'python.exe','python313.dll','python313.zip','python313._pth'}<=set(files):
        fail()
    for filename in WHEELS:
        wheel=archive_files(artifacts[filename]);verify_record(wheel)
        for name,raw in wheel.items():
            if any(part.endswith('.data') for part in PurePosixPath(name).parts):
                fail()
            dest='Lib/site-packages/'+name
            if dest.casefold() in seen:
                fail()
            seen.add(dest.casefold());files[dest]=raw
    # No import site: .pth hooks, user packages and inherited PYTHONPATH stay
    # disabled. Only the immutable standard library and exact wheel roots load.
    files['python313._pth']=b'python313.zip\n.\nLib/site-packages\n'
    return files


def prepare(commit,inputs,output):
    if not re.fullmatch(r'[0-9a-f]{40}',commit):
        fail()
    relative='infra/k8s/ha_control/enrollment.py'
    source=source_bundle.git('show',commit+':'+relative)
    if source!=Path(__file__).read_bytes() or output.exists() or output.is_symlink():
        fail()
    artifacts={}
    for name in (PYTHON_FILE,*WHEELS):
        path=inputs/name;info=path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size>67108864:
            fail()
        artifacts[name]=path.read_bytes()
    files=assemble(artifacts)
    manifest={'schema_version':1,'source_commit':commit,'builder_sha256':sha(source),'python_version':'3.13.17',
              'platform':'win_amd64','inputs':{name:sha(raw) for name,raw in artifacts.items()},
              'files':{name:sha(raw) for name,raw in sorted(files.items())},
              'native_accepted':False,'production_deployed':False}
    output.mkdir(mode=0o700)
    for name,raw in files.items():
        path=output/name;path.parent.mkdir(parents=True,exist_ok=True)
        with path.open('xb') as stream:
            stream.write(raw)
        path.chmod(0o444)
    raw=source_bundle.encode(manifest)
    path=output/'runtime-manifest.json';path.write_bytes(raw);path.chmod(0o444)
    return {'runtime_manifest_sha256':sha(raw),'source_commit':commit,'file_count':len(files),'native_accepted':False}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-commit',required=True)
    parser.add_argument('--inputs',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args()
    print(json.dumps(prepare(args.source_commit,args.inputs,args.output),sort_keys=True))


if __name__=='__main__':
    main()
