#!/usr/bin/env python3
"""Explicit, reviewed integration of the fixed Caddy and ingress controllers.

Preparation is read-only against production. Execution requires the exact clean
main commit and the externally reviewed preparation hash. No ACL changes, SSH,
credential copies, bootstrap changes, or automatic restart recovery exist here.
An uncertain run retains the existing sequence lock in a quiet live worker.
After process death, durable unfinished metadata blocks a new forward run;
status is read-only and never attempts recovery mutations.
"""
import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import signal
import stat
import subprocess
import sys
import time
import base64

TOOLS = ('caddy-ingress-transition.py', 'fixed-ingress-rollout.py',
         'fixed-ingress-evidence.py', 'prepare-ingress-networkpolicy.py',
         'prepare-app-hardening.py', 'pvc-backup-sequence.py', 'fixed-ingress-operation.py')
RUNTIME = ('.env', 'docker-compose.yml', 'docker-compose.n100.yml', 'docker-compose.portal-bridge.yml')
FINAL = {'PREPARED', 'COMPLETE', 'ROLLED_BACK', 'BLOCKED'}


class Blocked(Exception):
    pass


class JournalFailure(Blocked):
    pass


TRUSTED_FAILURE_TYPES = {Blocked, JournalFailure}


def require(ok, code):
    if not ok:
        raise Blocked(code)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def stamp():
    return datetime.now(timezone.utc).isoformat()


def reason(exc):
    if not isinstance(exc, tuple(TRUSTED_FAILURE_TYPES)):
        return 'operation_failed'
    value = str(exc)
    return value if re.fullmatch('[a-z0-9_]{1,80}', value) else 'operation_failed'


def run_readonly(args, *, cwd=None, timeout=30):
    """Bootstrap reader: no external output or exception text is displayed."""
    child = None
    try:
        env = None
        if args[0] == 'git':
            env = {**{k: v for k, v in os.environ.items() if not k.startswith('GIT_')}, 'GIT_CONFIG_NOSYSTEM': '1',
                   'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_ATTR_NOSYSTEM': '1'}
        child = subprocess.Popen(args, cwd=cwd, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True, env=env)
        raw, _ = child.communicate(timeout=timeout)
        require(child.returncode == 0, 'bootstrap_command_failed')
        return raw
    except (OSError, subprocess.TimeoutExpired):
        raise Blocked('bootstrap_command_unavailable') from None
    finally:
        if child is not None:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                pass
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            child.stdout.close()


def regular(path, *, private=False):
    path = Path(path).absolute()
    require(path.resolve() == path, 'symlink_path_forbidden')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.getuid(), 'unsafe_regular_file')
        if private:
            require(stat.S_IMODE(info.st_mode) == 0o600, 'private_file_mode')
        raw = os.read(fd, 2 * 1024 * 1024 + 1)
        require(len(raw) <= 2 * 1024 * 1024, 'private_file_size')
        after = os.stat(path, follow_symlinks=False)
        require((after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) ==
                (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns), 'file_identity_race')
        return raw
    finally:
        os.close(fd)


def bootstrap_source_guard(root, commit, runner=run_readonly):
    require(bool(re.fullmatch('[a-f0-9]{40}', commit)), 'exact_source_commit_required')
    require(not any(name.startswith('GIT_') for name in os.environ), 'git_environment_override')
    root = Path(root).absolute()
    require(root.resolve() == root, 'source_root_symlink')
    call = lambda *args: runner(['git', '--no-replace-objects', '--no-optional-locks',
                                '-c', 'core.attributesFile=/dev/null', *args], cwd=root)
    config = call('config', '--no-includes', '--null', '--list').decode().split('\0')
    for entry in config:
        key, _, value = entry.partition('\n')
        key = key.lower()
        require(not (key.startswith(('include.', 'includeif.', 'filter.')) or
            key in ('core.hookspath', 'core.sshcommand', 'diff.external', 'interactive.difffilter') or
            key.startswith('diff.') and key.endswith('.command') or
            key == 'core.fsmonitor' and value.lower() not in ('false', '0') or
            key == 'core.attributesfile' and value != '/dev/null'), 'unsafe_git_configuration')
    require(call('rev-parse', 'HEAD').decode().strip() == commit and
        call('branch', '--show-current').strip() == b'main', 'exact_clean_main_required')
    flags = call('ls-files', '-v', '-z').decode().split('\0')
    require(all(not x or x[0].isupper() and x[0] != 'S' for x in flags), 'hidden_git_index_flags')
    gitdir = Path(call('rev-parse', '--absolute-git-dir').decode().strip())
    require(gitdir == root / '.git' and gitdir.resolve() == gitdir, 'git_directory_binding_required')
    require(not call('for-each-ref', '--format=%(refname)', 'refs/replace').strip() and
            not (gitdir / 'info/grafts').exists() and not (gitdir / 'objects/info/alternates').exists(),
            'git_replacement_or_alternate_forbidden')
    require(not any((gitdir / name).exists() for name in ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD',
        'rebase-merge', 'rebase-apply', 'index.lock')), 'git_operation_active')
    require(not call('status', '--porcelain=v1', '--untracked-files=no'), 'tracked_source_dirty')
    call('diff', '--cached', '--quiet', commit)
    pins = {}
    paths = ['infra/k8s/tools/' + name for name in TOOLS] + ['scripts/runtime-service-state-reader.py']
    for relative in paths:
        blob = call('show', commit + ':' + relative)
        require(regular(root / relative) == blob, 'source_tool_pin_drift')
        pins[relative] = sha(blob)
    return pins


def load_tools(root, pins):
    modules = []
    for filename in TOOLS[:3]:
        relative = 'infra/k8s/tools/' + filename
        raw = regular(Path(root) / relative)
        require(sha(raw) == pins[relative], 'source_tool_pin_drift')
        name = 'fixed_operation_' + filename.replace('-', '_').replace('.py', '')
        spec = importlib.util.spec_from_file_location(name, Path(root) / relative)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        exec(compile(raw, str(Path(root) / relative), 'exec'), module.__dict__)
        modules.append(module)
    return tuple(modules)


def ps_json(runner, script):
    encoded = base64.b64encode(script.encode('utf-16le')).decode()
    try:
        return json.loads(runner(['powershell.exe', '-NoLogo', '-NoProfile', '-NonInteractive',
                                 '-EncodedCommand', encoded], timeout=30))
    except (ValueError, UnicodeError):
        raise Blocked('windows_inventory_invalid') from None


class WindowsRuntimeGuard:
    """Fresh read-only Drvfs, inode and Windows effective-permission checks.

    The default cannot silently accept POSIX-looking modes on a Windows volume.
    No boolean in a plan, cached ACL, or remediation command can bypass this.
    """
    def __init__(self, root, runner=run_readonly):
        self.root = Path(root).absolute()
        self.runner = runner
        self.owner_hashes = {}
        require(self.root.parts[:3] == ('/', 'mnt', 'c') and self.root.resolve() == self.root,
                'specific_drvfs_root_required')

    def mounted(self):
        try:
            mounts = json.loads(self.runner(['findmnt', '--json', '--target', str(self.root),
                '--output', 'TARGET,SOURCE,FSTYPE,OPTIONS']))['filesystems']
        except (ValueError, KeyError, TypeError):
            raise Blocked('drvfs_inventory_invalid') from None
        require(len(mounts) == 1, 'specific_drvfs_mount_required')
        mount = mounts[0]
        require(mount.get('target') == '/mnt/c' and mount.get('source', '').lower() == 'c:\\' and
            mount.get('fstype') in ('9p', 'drvfs') and
            (mount['fstype'] == 'drvfs' or 'aname=drvfs' in mount.get('options', '')),
            'specific_drvfs_mount_required')

    def windows_path(self, path):
        relative = Path(path).relative_to('/mnt/c')
        return 'C:\\' + '\\'.join(relative.parts)

    def acl(self, path):
        encoded = base64.b64encode(self.windows_path(path).encode()).decode()
        script = r'''$ErrorActionPreference='Stop'
$path=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String(''' + "'" + encoded + "'" + r'''))
$sid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$ti=([Security.Principal.NTAccount]'NT SERVICE\TrustedInstaller').Translate([Security.Principal.SecurityIdentifier]).Value
$rows=@();$item=Get-Item -LiteralPath $path -Force
while($null -ne $item){
 $isDirectory=($item -is [IO.DirectoryInfo])
 $acl=Get-Acl -LiteralPath $item.FullName
 $rules=@($acl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier]) | ForEach-Object {
  @{sid=$_.IdentityReference.Value;rights=[int64]$_.FileSystemRights;allow=($_.AccessControlType -eq 'Allow');inherit_only=([int]$_.PropagationFlags -band 2) -ne 0;inherited=$_.IsInherited}
 })
 $rows+=@{path=$item.FullName;owner=$acl.GetOwner([Security.Principal.SecurityIdentifier]).Value;protected=$acl.AreAccessRulesProtected;canonical=$acl.AreAccessRulesCanonical;reparse=([int]$item.Attributes -band 1024) -ne 0;directory=$isDirectory;rules=$rules}
 $item=if($isDirectory){$item.Parent}else{$item.Directory}
}
@{current_sid=$sid;trusted_installer_sid=$ti;objects=$rows} | ConvertTo-Json -Depth 7 -Compress
'''
        return ps_json(self.runner, script)

    def check_acl(self, observed, path, *, source=False, directory=None):
        current = observed.get('current_sid')
        require(isinstance(current, str) and re.fullmatch(r'S-1-5-21-[0-9-]+', current), 'windows_current_sid_required')
        trusted = {current, 'S-1-5-18', 'S-1-5-32-544'}
        ancestor_owners = trusted | {observed.get('trusted_installer_sid')}
        rows = observed.get('objects', [])
        require(rows and rows[0].get('path', '').lower() == self.windows_path(path).lower(), 'windows_path_identity')
        expected = Path(path)
        expected_paths = []
        while expected != Path('/mnt/c').parent:
            expected_paths.append(self.windows_path(expected).lower())
            if expected == Path('/mnt/c'):
                break
            expected = expected.parent
        require([r.get('path', '').lower().rstrip('\\') for r in rows] ==
                [p.rstrip('\\') for p in expected_paths], 'windows_ancestor_inventory_required')
        file_target = Path(path) != self.root if directory is None else not directory
        for index, row in enumerate(rows):
            is_root = row.get('path', '').lower().rstrip('\\') == self.windows_path(self.root).lower().rstrip('\\')
            require(row.get('reparse') is False and row.get('canonical') is True and
                    row.get('owner') in (trusted if index == 0 or is_root else ancestor_owners), 'windows_unsafe_ancestor')
            require(row.get('directory') is (not (index == 0 and file_target)), 'windows_object_type')
            rules = row.get('rules', [])
            if index == 0 and file_target and not source:
                require(row.get('owner') in trusted and row.get('protected') is True and len(rules) == 3 and
                    {r.get('sid') for r in rules} == trusted and all(r.get('allow') is True and
                        r.get('inherited') is False and r.get('inherit_only') is False and
                        r.get('rights') == 2032127 for r in rules), 'windows_runtime_acl_required')
            else:
                if is_root:
                    require(row.get('owner') in trusted and row.get('protected') is True, 'windows_root_acl_required')
                within_root = row.get('path', '').lower().startswith(self.windows_path(self.root).lower() + '\\')
                mask = 0xD0156 if is_root or within_root else 0xD0040
                require(all(not r.get('allow') or r.get('inherit_only') or r.get('sid') in trusted or
                            type(r.get('rights')) is int and not r['rights'] & mask for r in rules),
                        'windows_untrusted_ancestor_write')

    def root_guard(self):
        self.mounted()
        require(self.root.resolve() == self.root and self.root.is_dir(), 'windows_root_identity')
        observed = self.acl(self.root)
        self.check_acl(observed, self.root)
        self.owner_hashes['root'] = sha(canonical(observed['objects'][0]['owner']))

    def __call__(self, path, supplied=None):
        path = Path(path).absolute()
        require(path.parent == self.root and path.name in RUNTIME and path.resolve() == path, 'runtime_path_forbidden')
        self.root_guard()
        before = os.stat(path, follow_symlinks=False)
        require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and before.st_uid == os.getuid(), 'runtime_regular_single_link_required')
        if supplied is not None:
            require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) ==
                    (supplied.st_dev, supplied.st_ino, supplied.st_size, supplied.st_mtime_ns, supplied.st_ctime_ns), 'runtime_open_identity_race')
        observed = self.acl(path)
        self.check_acl(observed, path)
        self.owner_hashes[path.name] = sha(canonical(observed['objects'][0]['owner']))
        after = os.stat(path, follow_symlinks=False)
        require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) ==
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns), 'runtime_acl_identity_race')
        return True

    def owner_binding(self):
        require(set(self.owner_hashes) == {'root', *RUNTIME}, 'runtime_owner_inventory_required')
        return sha(canonical(self.owner_hashes))

    def source_guard(self, path):
        path = Path(path).absolute()
        require(self.root in path.parents and path.resolve() == path, 'source_acl_path_forbidden')
        self.root_guard()
        before = os.stat(path, follow_symlinks=False)
        require((stat.S_ISREG(before.st_mode) and before.st_nlink == 1 or stat.S_ISDIR(before.st_mode)) and
                before.st_uid == os.getuid(), 'source_acl_identity_required')
        self.check_acl(self.acl(path), path, source=True, directory=stat.S_ISDIR(before.st_mode))
        after = os.stat(path, follow_symlinks=False)
        require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) ==
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns), 'source_acl_identity_race')
        return True

    def execution_inputs_guard(self, paths):
        """Fresh bulk ACL scan includes all Git objects, refs/replace and hooks.

        Windows computes the safety decision and ACL digest in memory. Only
        booleans/counts/hashes cross back; every call scans fresh ACLs.
        """
        self.root_guard()
        fixed = [self.windows_path(path) for path in paths]
        encoded = base64.b64encode(canonical({'paths': fixed, 'git': self.windows_path(self.root / '.git')})).decode()
        script = r'''$ErrorActionPreference='Stop'
$v=([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String(''' + "'" + encoded + "'" + r'''))) | ConvertFrom-Json
$sid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$trusted=@($sid,'S-1-5-18','S-1-5-32-544');$safe=$true;$lines=@()
$items=@(Get-Item -LiteralPath $v.git -Force)+@(Get-ChildItem -LiteralPath $v.git -Force -Recurse)
foreach($path in $v.paths){$items+=Get-Item -LiteralPath $path -Force}
$items=@($items | Sort-Object FullName -Unique)
foreach($item in $items){
 if(([int]$item.Attributes -band 1024) -ne 0){$safe=$false}
 $acl=Get-Acl -LiteralPath $item.FullName
 if(-not $acl.AreAccessRulesCanonical -or $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -notin $trusted){$safe=$false}
 foreach($rule in $acl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier])){
  if($rule.AccessControlType -eq 'Allow' -and ([int]$rule.PropagationFlags -band 2) -eq 0 -and $rule.IdentityReference.Value -notin $trusted -and ([int64]$rule.FileSystemRights -band 0xD0156) -ne 0){$safe=$false}
 }
 $lines+=($item.FullName.ToLowerInvariant()+'|'+$acl.Sddl+'|'+[int]$item.Attributes)
}
$bytes=[Text.Encoding]::UTF8.GetBytes(($lines -join "`n"))
$hash=([BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash($bytes))).Replace('-','').ToLowerInvariant()
@{safe=$safe;count=$items.Count;acl_sha256=$hash} | ConvertTo-Json -Compress
'''
        value = ps_json(self.runner, script)
        require(value.get('safe') is True and type(value.get('count')) is int and
                value['count'] >= len(paths) + 1 and isinstance(value.get('acl_sha256'), str) and
                re.fullmatch('[a-f0-9]{64}', value['acl_sha256']), 'execution_input_acl_unsafe')
        return value['acl_sha256']


def windows_collisions(runner):
    value = ps_json(runner, r'''$ErrorActionPreference='Stop'
@{routes=@(Get-NetRoute -PolicyStore ActiveStore | ForEach-Object {$_.DestinationPrefix});addresses=@(Get-NetIPAddress | ForEach-Object {"$($_.IPAddress)/$($_.PrefixLength)"})} | ConvertTo-Json -Depth 4 -Compress''')
    result = []
    require(isinstance(value, dict) and set(value) == {'routes', 'addresses'} and
            all(isinstance(value[k], list) and value[k] for k in value), 'windows_collision_inventory_required')
    for prefix in value['routes'] + value['addresses']:
        try:
            network = ipaddress.ip_network(prefix.split('%')[0] if '%' not in prefix else
                prefix.split('%')[0] + '/' + prefix.rsplit('/', 1)[1], strict=False)
        except (ValueError, AttributeError):
            raise Blocked('windows_prefix_invalid') from None
        if network.prefixlen:
            result.append(str(network))
    return result


def native_caddy_type(module):
    class WindowsCaddyBackend(module.NativeBackend):
        def snapshot(self, **kwargs):
            value = super().snapshot(**kwargs)
            value['collisions'].extend(windows_collisions(lambda args, **options: self.run(args, **options)))
            return value
    return WindowsCaddyBackend


def private_tree(path, home, *, create=False):
    path, home = Path(path).absolute(), Path(home).absolute()
    require(path.resolve() == path and home.resolve() == home and home in path.parents and
            not str(path).startswith('/mnt/'), 'linux_home_private_path_required')
    for ancestor in (home, *home.parents):
        info = os.lstat(ancestor)
        require(stat.S_ISDIR(info.st_mode) and info.st_uid in (0, os.getuid()) and
                not info.st_mode & 0o022, 'unsafe_private_ancestor')
    cursor = home
    for component in path.relative_to(home).parts:
        parent = os.lstat(cursor)
        require(stat.S_ISDIR(parent.st_mode) and parent.st_uid == os.getuid() and not parent.st_mode & 0o022,
                'unsafe_private_ancestor')
        cursor /= component
        if create:
            try:
                os.mkdir(cursor, 0o700)
            except FileExistsError:
                pass
        info = os.lstat(cursor)
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o022,
                'unsafe_private_ancestor')
    require(stat.S_IMODE(os.lstat(path).st_mode) == 0o700, 'private_directory_mode')
    return path


class RunJournal:
    """Private fsynced ledger; unsafe or unfinished prior metadata is blocking."""
    def __init__(self, parent, run_id, home, *, create=False, writable=True):
        self.failed = False
        require(bool(re.fullmatch('[a-f0-9]{32}', run_id)), 'run_id_required')
        self.home = Path(home).absolute()
        self.parent = private_tree(parent, home, create=create)
        self.path = self.parent / run_id
        if create:
            os.mkdir(self.path, 0o700)
            self.sync_directory(self.parent)
        private_tree(self.path, home)
        self.fd = None
        if create:
            self.write_once('initial-intent.json', {'schema_version': 1, 'run_id': run_id, 'observed_at': stamp()})
            self.write_once('events.jsonl', None, raw=b'')
            self.state = {'schema_version': 1, 'run_id': run_id, 'status': 'PREPARING'}
            self.state_save()
        else:
            self.state = self.read('state.json')
            require(self.state.get('schema_version') == 1 and self.state.get('run_id') == run_id, 'run_metadata_identity')
        initial = self.read('initial-intent.json')
        require(initial.get('schema_version') == 1 and initial.get('run_id') == run_id, 'initial_intent_identity')
        regular(self.path / 'events.jsonl', private=True)
        if writable:
            self.fd = os.open(self.path / 'events.jsonl', os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC)

    @staticmethod
    def sync_directory(path):
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def write_once(self, name, value, *, raw=None):
        private_tree(self.path, self.home)
        fd = os.open(self.path / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            self.write_all(fd, canonical(value) if raw is None else raw)
            os.fsync(fd)
        finally:
            os.close(fd)
        self.sync_directory(self.path)

    @staticmethod
    def write_all(fd, raw):
        view = memoryview(raw)
        while view:
            count = os.write(fd, view)
            require(count > 0, 'journal_short_write')
            view = view[count:]

    def read(self, name):
        private_tree(self.path, self.home)
        try:
            return json.loads(regular(self.path / name, private=True))
        except (ValueError, UnicodeError):
            raise Blocked('run_metadata_invalid') from None

    def state_save(self, **updates):
        try:
            require(not self.failed, 'durable_journal_failed')
            self.state = {**self.state, **updates}
            private_tree(self.path, self.home)
            target = self.path / 'state.json'
            if target.exists():
                regular(target, private=True)
            temporary = '.state-' + secrets.token_hex(8)
            self.write_once(temporary, self.state)
            os.replace(self.path / temporary, target)
            self.sync_directory(self.path)
        except Exception:
            self.failed = True
            raise JournalFailure('durable_journal_failed') from None

    def emit(self, event, **fields):
        value = dict(event) if isinstance(event, dict) else {'event': event, **fields}
        value.setdefault('observed_at', stamp())
        # Collectors emit fixed codes, hashes and ownership metadata only.
        # Never copy arbitrary exception text, configurations, logs or addresses.
        def safe(item):
            if isinstance(item, dict):
                return all(isinstance(k, str) and not any(word in k.lower() for word in
                    ('password', 'token', 'secret', 'address', 'cidr', 'config', 'stdout', 'stderr', 'log')) and safe(v)
                    for k, v in item.items())
            if isinstance(item, list):
                return all(safe(v) for v in item)
            if isinstance(item, str):
                require(len(item) <= 256, 'unsafe_journal_value')
                try:
                    ipaddress.ip_address(item)
                except ValueError:
                    return '\n' not in item and '\r' not in item
                return False
            return item is None or type(item) in (int, bool)
        try:
            require(not self.failed, 'durable_journal_failed')
            require(safe(value), 'unsafe_journal_value')
            private_tree(self.path, self.home)
            current = os.stat(self.path / 'events.jsonl', follow_symlinks=False)
            opened = os.fstat(self.fd)
            require(current.st_ino == opened.st_ino and current.st_dev == opened.st_dev and
                stat.S_IMODE(current.st_mode) == 0o600 and current.st_nlink == 1 and current.st_uid == os.getuid(), 'journal_identity_race')
            self.write_all(self.fd, canonical(value) + b'\n')
            os.fsync(self.fd)
        except Exception:
            self.failed = True
            raise JournalFailure('durable_journal_failed') from None

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def transition_journal_type(module):
    """Preserve durable-write failure identity through transition error wrapping."""
    class TransitionJournal(module.Journal):
        failed = False

        def save(self, **updates):
            try:
                require(not self.failed, 'durable_journal_failed')
                return super().save(**updates)
            except Exception:
                self.failed = True
                raise JournalFailure('durable_journal_failed') from None
    return TransitionJournal


def startup_gate(parent, home, *, selected=None):
    path = private_tree(parent, home)
    for child in path.iterdir():
        require(child.is_dir() and re.fullmatch('[a-f0-9]{32}', child.name), 'unexpected_run_metadata')
        journal = RunJournal(path, child.name, home, writable=False)
        try:
            require(journal.state.get('status') in FINAL, 'unfinished_prior_run')
            if child.name == selected:
                require(journal.state['status'] == 'PREPARED', 'prepared_run_already_consumed')
            # Validate every persisted artifact; unrecognized files can conceal
            # interrupted atomic writes and therefore block a forward run.
            require(set(p.name for p in child.iterdir()) <= {'initial-intent.json', 'events.jsonl',
                'state.json', 'plan.json', 'projection.json', 'transition.json'}, 'unexpected_run_metadata')
            for artifact in child.iterdir():
                regular(artifact, private=True)
        finally:
            journal.close()


class Operation:
    def __init__(self, root, commit, run_parent, *, home=None, runner=run_readonly,
                 source_guard=bootstrap_source_guard, tools_loader=load_tools,
                 runtime_guard=None, sleep=time.sleep, hold=None, caddy_factory=None,
                 policy_factory=None, controller_factory=None, collector_factory=None):
        self.root, self.commit = Path(root).absolute(), commit
        self.home = Path(home or Path.home()).absolute()
        self.parent = Path(run_parent).absolute()
        self.runner, self.source_guard, self.tools_loader = runner, source_guard, tools_loader
        self.runtime_guard = runtime_guard
        self.sleep, self.hold_callback = sleep, hold
        self.caddy_factory, self.policy_factory = caddy_factory, policy_factory
        self.controller_factory, self.collector_factory = controller_factory, collector_factory
        self.stack, self.held, self.mutation_intent = None, False, False
        self.journal = None

    def initialize(self):
        # Root ACL and tool-source integrity precede dynamic module loading.
        require(self.root.resolve() == self.root, 'source_root_symlink')
        if self.runtime_guard is None:
            require(sys.platform.startswith('linux'), 'linux_wsl_runtime_required')
            self.runtime_guard = WindowsRuntimeGuard(self.root, self.runner)
        require(callable(self.runtime_guard) and callable(getattr(self.runtime_guard, 'root_guard', None)),
                'trusted_runtime_file_guard_required')
        self.runtime_guard.root_guard()
        require(callable(getattr(self.runtime_guard, 'source_guard', None)), 'trusted_source_acl_guard_required')
        self.source_paths = [self.root / 'infra/k8s/tools' / name for name in TOOLS]
        self.source_paths += [self.root / 'scripts/runtime-service-state-reader.py']
        gitdir = self.root / '.git'
        require(gitdir.is_dir() and gitdir.resolve() == gitdir, 'native_main_git_directory_required')
        self.source_paths += [gitdir, gitdir / 'config', gitdir / 'HEAD', gitdir / 'index', gitdir / 'hooks']
        self.source_paths += [p for p in (gitdir / 'packed-refs', gitdir / 'refs/heads/main') if p.exists()]
        self.source_paths += list((gitdir / 'hooks').iterdir())
        require(callable(getattr(self.runtime_guard, 'execution_inputs_guard', None)), 'bulk_source_acl_guard_required')
        self.source_acl_hash = self.runtime_guard.execution_inputs_guard(self.source_paths)
        for path in self.source_paths[:len(TOOLS)+1]:
            require(self.runtime_guard.source_guard(path) is True, 'source_acl_failed')
        self.pins = self.source_guard(self.root, self.commit, self.runner)
        self.caddy, self.rollout, self.evidence = self.tools_loader(self.root, self.pins)
        for module in (self.caddy, self.rollout, self.evidence):
            for name in ('Blocked', 'UnknownWrite', 'CommandFailed'):
                failure = getattr(module, name, None)
                if isinstance(failure, type) and issubclass(failure, Exception):
                    TRUSTED_FAILURE_TYPES.add(failure)
        for name in RUNTIME:
            require(self.runtime_guard(self.root / name) is True, 'runtime_acl_failed')
        require(callable(getattr(self.runtime_guard, 'owner_binding', None)), 'runtime_owner_binding_required')
        self.owner_binding = self.runtime_guard.owner_binding()
        factory = self.policy_factory or self.rollout.NativeBackend
        self.pb = factory(self.root, self.commit, journal=lambda event: self.journal.emit(event))
        self.pb.source_guard()
        self.baseline = self.pb.guards()

    def protected_guard(self):
        require(self.runtime_guard.execution_inputs_guard(self.source_paths) == self.source_acl_hash,
                'execution_input_acl_drift')
        self.pb.source_guard()
        for relative, expected in self.pins.items():
            require(sha(regular(self.root / relative)) == expected, 'source_tool_pin_drift')
        for name in RUNTIME:
            require(self.runtime_guard(self.root / name) is True, 'runtime_acl_failed')
        require(self.runtime_guard.owner_binding() == self.owner_binding, 'runtime_owner_drift')
        require(self.pb.guards() == self.baseline, 'protected_state_drift')

    def backend(self, plan):
        factory = self.caddy_factory or native_caddy_type(self.caddy)
        return factory(self.root, plan, protected_guard=self.protected_guard,
                       runtime_file_guard=self.runtime_guard)

    def lock(self, cb):
        self.stack = ExitStack()
        self.stack.enter_context(cb.local_lock())
        cb.lock_owned = True
        self.stack.callback(setattr, cb, 'lock_owned', False)

    def health(self):
        for round_number in range(3):
            for app in self.rollout.ORDER:
                require(self.pb.external_health(app) == 200, 'external_health_failed')
            if round_number < 2:
                self.sleep(10)

    def prepare(self):
        self.initialize()
        plan = {'schema_version': 1, 'source_commit': self.commit, 'owner_id': secrets.token_hex(32),
                'network_name': 'fixed_caddy_' + secrets.token_hex(6),
                'budget': {'forward_seconds': 600, 'rollback_seconds': 600}}
        cb = self.backend(plan)
        try:
            self.lock(cb)
            self.protected_guard()
            bridge, env = cb.runtime_bytes()
            plan.update(bridge_sha256=sha(bridge), env_sha256=sha(env))
            prepared = self.caddy.prepare_transition(cb.snapshot(), bridge, env, plan)
            projection = self.caddy.prepared_projection(prepared)
            plan.update(prepared_sha256=prepared['prepared_sha256'], prepared_projection=projection,
                        reviewed=False, protected_guard_ack=False, owner_binding_sha256=self.owner_binding)
            private_tree(self.parent, self.home, create=True)
            startup_gate(self.parent, self.home)
            run_id = secrets.token_hex(16)
            journal = RunJournal(self.parent, run_id, self.home, create=True)
            self.journal = journal
            self.stack.callback(journal.close)
            journal.write_once('plan.json', plan)
            journal.write_once('projection.json', projection)
            journal.emit('prepare_complete', source_commit=self.commit, prepared_sha256=prepared['prepared_sha256'])
            journal.state_save(status='PREPARED', source_commit=self.commit, prepared_sha256=prepared['prepared_sha256'])
            return {'status': 'PREPARED', 'run_id': run_id, 'prepared_sha256': prepared['prepared_sha256']}
        finally:
            if self.stack:
                self.stack.close()

    def held_wait(self, code):
        self.held = True
        try:
            self.journal.emit('operation_held', reason=code)
            self.journal.state_save(status='HELD', reason=code)
        except Exception:
            # The preceding RUNNING record remains blocking if persistence fails.
            pass
        result = {'status': 'HELD', 'reason': code, 'sequence_lock_retained': True}
        if self.hold_callback is not None:
            self.hold_callback(result)
            return result  # The ExitStack remains open even if a trusted callback returns.
        print(json.dumps(result), flush=True)
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: None)
        while True:
            try:
                self.sleep(30)
            except BaseException:
                pass

    def execute(self, run_id, reviewed_sha):
        require(bool(re.fullmatch('[a-f0-9]{64}', reviewed_sha)), 'externally_reviewed_preparation_required')
        self.initialize()
        startup_gate(self.parent, self.home, selected=run_id)
        journal = RunJournal(self.parent, run_id, self.home)
        self.journal = journal
        try:
            plan, projection = journal.read('plan.json'), journal.read('projection.json')
            require(plan.get('source_commit') == self.commit and journal.state.get('source_commit') == self.commit and
                plan.get('prepared_projection') == projection and sha(canonical(projection)) == reviewed_sha ==
                plan.get('prepared_sha256') == journal.state.get('prepared_sha256'), 'reviewed_preparation_mismatch')
            require(plan.get('owner_binding_sha256') == self.owner_binding, 'runtime_owner_changed_since_preparation')
        except BaseException:
            journal.close()
            raise
        plan.update(reviewed=True, protected_guard_ack=True)
        controller = None
        try:
            cb = self.backend(plan)
            self.lock(cb)
            self.stack.callback(journal.close)
            startup_gate(self.parent, self.home, selected=run_id)
            self.protected_guard()
            bridge, env = cb.runtime_bytes()
            prepared = self.caddy.prepare_transition(cb.snapshot(), bridge, env, plan)
            require(prepared['prepared_sha256'] == reviewed_sha and
                self.caddy.prepared_projection(prepared) == projection, 'fresh_preparation_mismatch')
            journal.emit('operation_intent', source_commit=self.commit, prepared_sha256=reviewed_sha)
            journal.state_save(status='RUNNING')
            transition_journal = transition_journal_type(self.caddy)(journal.path, self.root)
            self.stack.callback(transition_journal.close)
            self.mutation_intent = True
            journal.emit('transition_mutation_intent')
            state = self.caddy.transition(plan, cb, transition_journal)
            require(state == 'COMPLETE', 'caddy_transition_held')
            self.health()
            self.pb.deadline = time.monotonic() + 1800
            cb.deadline = self.pb.deadline
            factory = self.collector_factory or self.evidence.Collector
            self.pb.collector = factory(cb, cb._prepared, journal.emit)
            factory = self.controller_factory or self.rollout.Controller
            controller = factory(self.pb, sleep=self.sleep)
            result = controller.run()
            require(result.get('status') == 'PASS' and result.get('owned_cleanup_complete') is True,
                    'policy_rollout_failed')
            self.protected_guard()
            self.health()
            journal.emit('operation_complete', policy_count=4, result_sha256=sha(canonical(result)))
            journal.state_save(status='COMPLETE', policy_count=4)
            return {'status': 'COMPLETE', 'run_id': run_id, 'policy_count': 4, 'prepared_sha256': reviewed_sha}
        except BaseException as exc:
            code = reason(exc)
            unknown = (isinstance(exc, JournalFailure) or journal.failed or
                getattr(locals().get('cb'), 'command_termination_unknown', False)
                or getattr(self.pb, 'termination_unknown', False) or
                'transition_journal' in locals() and (transition_journal.failed or
                    transition_journal.state.get('child_termination_unknown') or
                    transition_journal.state.get('recreate_response_unknown') and not
                    transition_journal.state.get('recreate_response_observed') or
                    transition_journal.state.get('rollback_response_unknown') and
                    transition_journal.state.get('phase') != 'rolled_back'))
            if not self.mutation_intent:
                if isinstance(exc, JournalFailure):
                    return self.held_wait(code)
                try:
                    journal.state_save(status='BLOCKED', reason=code)
                except JournalFailure:
                    return self.held_wait('durable_journal_failed')
                raise Blocked(code) from None
            if unknown:
                return self.held_wait(code)
            try:
                self.pb.begin_recovery()
                cb.deadline = time.monotonic() + 600
                if controller is not None:
                    report = controller.report
                    if report.get('status') == 'PASS':
                        # This is the first rollback for a wrapper postcheck
                        # failure, never a retry of a failed controller recovery.
                        controller.rollback()
                        for intent in controller.intents:
                            controller.verify(intent['metadata']['name'].removesuffix('-ingress-reviewed'))
                        report.update(rollback_complete=True, restored_flow_proven=True,
                                      owned_cleanup_complete=self.pb.cleanup() is True)
                    require(report.get('rollback_complete') is True and report.get('owned_cleanup_complete') is True and
                            report.get('restored_flow_proven') is True, 'policy_recovery_incomplete')
                else:
                    require(not self.pb.policies() and (self.pb.collector is None or self.pb.cleanup() is True),
                            'policy_recovery_incomplete')
                require(not self.pb.policies(), 'policies_remain_before_caddy_rollback')
                journal.emit('caddy_rollback_intent')
                outcome = self.caddy.transition(plan, cb, transition_journal, rollback=True)
                require(outcome == 'ROLLED_BACK', 'caddy_rollback_held')
                self.protected_guard()
                self.health()
                journal.emit('operation_rolled_back', reason=code)
                journal.state_save(status='ROLLED_BACK', reason=code)
                return {'status': 'ROLLED_BACK', 'run_id': run_id, 'reason': code}
            except BaseException:
                return self.held_wait('owned_recovery_incomplete')
        finally:
            if self.stack and not self.held:
                self.stack.close()
            elif self.stack is None:
                journal.close()


def status(parent, run_id, home=None):
    journal = RunJournal(parent, run_id, home or Path.home(), writable=False)
    try:
        value = journal.state
        return {k: value[k] for k in ('status', 'run_id', 'source_commit', 'prepared_sha256', 'policy_count', 'reason') if k in value}
    finally:
        journal.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--prepare', action='store_true')
    mode.add_argument('--execute', action='store_true')
    mode.add_argument('--status', action='store_true')
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument('--source-commit')
    parser.add_argument('--run-parent', type=Path, default=Path.home() / '.local/state/personal-server/fixed-ingress')
    parser.add_argument('--run-id')
    parser.add_argument('--prepared-sha')
    args = parser.parse_args(argv)
    try:
        if args.status:
            result = status(args.run_parent, args.run_id)
        else:
            require(args.source_commit is not None, 'exact_source_commit_required')
            operation = Operation(args.root, args.source_commit, args.run_parent)
            result = operation.prepare() if args.prepare else operation.execute(args.run_id, args.prepared_sha or '')
        print(json.dumps(result, sort_keys=True))
        return 0 if result['status'] in ('PREPARED', 'COMPLETE') else 2
    except BaseException as exc:
        print(json.dumps({'status': 'BLOCKED', 'reason': reason(exc)}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
