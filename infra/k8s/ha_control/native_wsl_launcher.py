"""Isolated bootstrap for the exact WSL cutover source tree."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import sys


def checked(path, owner, limit):
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
        info = os.fstat(stream.fileno())
        raw = stream.read(limit + 1)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != owner or info.st_nlink != 1
                or info.st_mode & 0o077 or not len(raw) == info.st_size <= limit):
            raise ValueError('native_wsl_source_file_untrusted')
    return raw


def verify(root, manifest, digest, owner):
    if manifest.parent != root.parent:
        raise ValueError('native_wsl_source_manifest_path_untrusted')
    for directory in (root, *root.parents):
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, owner) or info.st_mode & 0o022:
            raise ValueError('native_wsl_source_path_untrusted')
    if root.stat().st_uid != owner or root.stat().st_mode & 0o077:
        raise ValueError('native_wsl_source_path_untrusted')
    raw = checked(manifest, owner, 262144)
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError('native_wsl_source_manifest_unbound')
    files = json.loads(raw)
    if type(files) is not dict or not files or len(files) > 1024:
        raise ValueError('native_wsl_source_manifest_invalid')
    expected_directories = set()
    for name, pin in files.items():
        if (not re.fullmatch(r'[A-Za-z0-9_./-]+', name)
                or any(part in ('', '.', '..') for part in name.split('/'))
                or type(pin) is not str or not re.fullmatch(r'[a-f0-9]{64}', pin)):
            raise ValueError('native_wsl_source_manifest_invalid')
        expected_directories.update(str(parent) for parent in Path(name).parents if parent != Path('.'))
    seen = set()
    sources = {}
    for directory, subdirs, names in os.walk(root, followlinks=False):
        for name in subdirs:
            child = Path(directory) / name
            info = child.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != owner or info.st_mode & 0o077
                    or child.relative_to(root).as_posix() not in expected_directories):
                raise ValueError('native_wsl_source_path_untrusted')
        for name in names:
            child = Path(directory) / name
            relative = child.relative_to(root).as_posix()
            if relative not in files:
                raise ValueError('native_wsl_source_file_unbound')
            raw = checked(child, owner, 4194304)
            if hashlib.sha256(raw).hexdigest() != files[relative]:
                raise ValueError('native_wsl_source_file_unbound')
            if relative.endswith('.py'):
                sources[relative] = raw
            seen.add(relative)
    if seen != files.keys() or 'ha_control/run_native_wsl_cutover.py' not in sources:
        raise ValueError('native_wsl_source_manifest_incomplete')
    return sources


class VerifiedSources:
    def __init__(self, root, sources):
        self.root = root
        self.sources = sources
        self.packages = {name.split('/')[0] for name in sources if '/' in name}

    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] not in self.packages:
            return None
        stem = fullname.replace('.', '/')
        package = stem + '/__init__.py'
        if package in self.sources:
            return importlib.util.spec_from_file_location(fullname, self.root / package, loader=self,
                                                          submodule_search_locations=[])
        module = stem + '.py'
        if module in self.sources:
            return importlib.util.spec_from_file_location(fullname, self.root / module, loader=self)
        if any(name.startswith(stem + '/') for name in self.sources):
            from importlib.machinery import ModuleSpec
            return ModuleSpec(fullname, None, is_package=True)
        raise ModuleNotFoundError(fullname)

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        path = Path(module.__spec__.origin)
        raw = self.sources[path.relative_to(self.root).as_posix()]
        exec(compile(raw, str(path), 'exec'), vars(module))


def main():
    root, digest, boot, uid, manifest = sys.argv[1:6]
    owner = os.geteuid()
    if owner == 0 or owner != int(uid) or hashlib.sha256(Path('/proc/sys/kernel/random/boot_id').read_bytes()).hexdigest() != boot:
        raise ValueError('native_wsl_identity_unbound')
    root = Path(root)
    sources = verify(root, Path(manifest), digest, owner)
    entry = 'ha_control/run_native_wsl_cutover.py'
    source = root / entry
    sys.argv[:] = [str(source), *sys.argv[6:]]
    sys.meta_path.insert(0, VerifiedSources(root, sources))
    exec(compile(sources[entry], str(source), 'exec'), {
        '__name__': '__main__', '__package__': 'ha_control', '__file__': str(source),
    })


if __name__ == '__main__':
    main()
