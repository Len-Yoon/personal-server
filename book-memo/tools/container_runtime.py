"""Ownership-scoped, bounded Docker adapter for the isolated Book version lab.

This adapter never connects to a network Docker endpoint, publishes ports, mounts
host data, or discovers resources for deletion beyond its own registered names.
"""
from __future__ import annotations

import hashlib
import base64
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import resource
import selectors
import signal
import shutil
import stat
import subprocess
import tarfile
import time
from typing import Callable

ERROR = 'Local Docker lab failed'
OWNER_LABEL = 'org.personal-server.lab.owner'
KIND_LABEL = 'org.personal-server.lab.kind'
KIND = 'book-version'
_HEX32 = re.compile(r'[0-9a-f]{32}\Z')
_HEX40 = re.compile(r'[0-9a-f]{40}\Z')
_HEX64 = re.compile(r'[0-9a-f]{64}\Z')
_IMAGE = re.compile(r'sha256:[0-9a-f]{64}\Z')
_NAME = re.compile(r'(?:[A-Za-z0-9_][A-Za-z0-9_.-]*|\.gitkeep)\Z')
DOCKERFILE_BYTES = (Path(__file__).resolve().parents[1] / 'Dockerfile').read_bytes()
DOCKERFILE_SHA = '72842b9e27b9eee85c1e1add2821bedb3055315c713e2a8396615912cf1675ea'
ARCHIVE_LIMIT = 1024 * 1024 * 1024


def _canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode()


def _fail() -> None:
    raise RuntimeError(ERROR)


def _docker_binary() -> str:
    """Do not resolve executable names through an ambient PATH."""
    candidates = ('/usr/local/bin/docker', '/opt/homebrew/bin/docker', '/usr/bin/docker',
                  '/Applications/OrbStack.app/Contents/MacOS/xbin/docker',
                  '/Applications/Docker.app/Contents/Resources/bin/docker')
    prefixes = ('/usr/bin/', '/usr/local/', '/opt/homebrew/',
                '/Applications/OrbStack.app/', '/Applications/Docker.app/')
    for candidate in candidates:
        path = Path(candidate)
        try:
            resolved = path.resolve(strict=True)
            mode = resolved.stat().st_mode
            if str(resolved).startswith(prefixes) and stat.S_ISREG(mode) and not mode & 0o022 and os.access(resolved, os.X_OK):
                return str(path)
        except OSError:
            continue
    _fail()


class LocalDocker:
    """A single invocation owns all names and immutable identities it records."""

    def __init__(self, owner: str, deadline: float, interrupted: Callable[[], bool]):
        if (not isinstance(owner, str) or not _HEX32.fullmatch(owner)
                or type(deadline) not in (int, float) or not math.isfinite(deadline)
                or deadline <= time.monotonic() or not callable(interrupted)):
            _fail()
        self.owner = owner
        self.deadline = deadline
        self.interrupted = interrupted
        self._cleanup_deadline = None
        self._binary = _docker_binary()
        self._endpoint = None
        self._env = {'PATH': os.defpath, 'HOME': str(Path.home()), 'LC_ALL': 'C'}
        self._containers: dict[str, dict] = {}
        self._volumes: dict[str, None] = {}
        self._images: dict[str, dict | None] = {}
        self._unknown_builds: set[str] = set()
        self._build_labels: dict[str, dict] = {}
        self._counter = 0
        self._closed = False
        try:
            host = os.environ.get('DOCKER_HOST')
            if host is not None:
                self._validate_endpoint(host)
            context = os.environ.get('DOCKER_CONTEXT')
            if context is not None and (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', context)):
                _fail()
            args = ['context', 'inspect']
            if context:
                args.append(context)
            args += ['--format', '{{json .Endpoints.docker.Host}}']
            endpoint = json.loads(self._run(args))
            self._validate_endpoint(endpoint)
            self._endpoint = host or endpoint
        except (OSError, ValueError, TypeError, KeyError, RuntimeError):
            raise RuntimeError(ERROR) from None

    @staticmethod
    def _validate_endpoint(endpoint):
        if (not isinstance(endpoint, str) or not endpoint.startswith('unix:///')
                or len(endpoint) > 4096 or any(ord(c) < 32 for c in endpoint)
                or '?' in endpoint or '#' in endpoint or '..' in PurePosixPath(endpoint[7:]).parts):
            _fail()

    def _budget(self, cleanup=False):
        try:
            deadline = self._cleanup_deadline if cleanup else self.deadline
            if deadline is None or time.monotonic() >= deadline or (not cleanup and self.interrupted()):
                _fail()
            return deadline
        except Exception:
            raise RuntimeError(ERROR) from None

    def _run(self, args: list[str], *, cap=15, limit=2 * 1024 * 1024, cleanup=False, bounded_file=None) -> str:
        """Bound both running time and buffered output; reap cancelled children."""
        process = None
        try:
            deadline = min(self._budget(cleanup), time.monotonic() + cap)
            command = [self._binary]
            if self._endpoint is not None:
                command += ['--host', self._endpoint]
            command += args
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                       env=self._env, stdin=subprocess.DEVNULL, start_new_session=True,
                                       preexec_fn=(lambda: resource.setrlimit(resource.RLIMIT_FSIZE, (ARCHIVE_LIMIT, ARCHIVE_LIMIT)))
                                       if bounded_file is not None else None)
            output = bytearray()
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while True:
                    self._budget(cleanup)
                    if bounded_file is not None and bounded_file.exists() and bounded_file.stat().st_size > ARCHIVE_LIMIT:
                        _fail()
                    if time.monotonic() >= deadline:
                        _fail()
                    if not selector.select(min(.05, max(.001, deadline - time.monotonic()))):
                        continue
                    block = os.read(process.stdout.fileno(), min(65536, limit + 1 - len(output)))
                    if not block:
                        break
                    output.extend(block)
                    if len(output) > limit:
                        _fail()
            while process.poll() is None:
                self._budget(cleanup)
                if time.monotonic() >= deadline:
                    _fail()
                try:
                    process.wait(timeout=min(.05, max(.001, deadline - time.monotonic())))
                except subprocess.TimeoutExpired:
                    pass
            self._budget(cleanup)
            if process.returncode:
                _fail()
            return output.decode('utf-8')
        except (OSError, ValueError, UnicodeError, RuntimeError):
            raise RuntimeError(ERROR) from None
        finally:
            if process is not None:
                # The child owns a new session/group. Kill that group even if
                # its CLI leader has exited: buildx/plugin descendants can keep
                # pipes or work alive after the leader terminates.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                if process.poll() is None:
                    process.kill()
                process.wait()
                process.stdout.close()

    def _labels(self):
        return {OWNER_LABEL: self.owner, KIND_LABEL: KIND}

    def _owned(self, labels):
        return isinstance(labels, dict) and all(labels.get(key) == value for key, value in self._labels().items())

    @staticmethod
    def _list_args(kind, name):
        if kind == 'container':
            return ['container', 'ls', '--all', '--no-trunc', '--filter', f'name=^/{name}$', '--format', '{{.ID}}']
        if kind == 'volume':
            return ['volume', 'ls', '--filter', f'name=^{name}$', '--format', '{{.Name}}']
        if kind == 'image':
            return ['image', 'ls', '--no-trunc', '--filter', f'reference={name}', '--format', '{{.ID}}']
        _fail()

    def _present(self, kind, name, *, cleanup=False):
        # Only successful list output establishes absence. Inspect failures never do.
        lines = self._run(self._list_args(kind, name), cleanup=cleanup).splitlines()
        if len(lines) > 1 or any(not line for line in lines):
            _fail()
        if lines and ((kind == 'volume' and lines[0] != name)
                      or (kind == 'container' and not _HEX64.fullmatch(lines[0]))
                      or (kind == 'image' and not _IMAGE.fullmatch(lines[0]))):
            _fail()
        return bool(lines)

    def _image_id_present(self, identity, *, cleanup=False):
        # Inspect failure cannot distinguish absence from a daemon/transport error.
        # A successful ownership-filtered list verifies a previously recorded ID.
        lines = self._run(['image', 'ls', '--all', '--no-trunc', '--filter',
                           f'label={OWNER_LABEL}={self.owner}', '--format', '{{.ID}}'],
                          cleanup=cleanup).splitlines()
        if any(not _IMAGE.fullmatch(line) for line in lines):
            _fail()
        return identity in lines

    def _inspect(self, kind, identity, *, cleanup=False):
        try:
            values = json.loads(self._run([kind, 'inspect', identity], cleanup=cleanup))
            if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], dict):
                _fail()
            return values[0]
        except (ValueError, TypeError):
            raise RuntimeError(ERROR) from None

    def info(self) -> dict:
        try:
            data = json.loads(self._run(['info', '--format', '{{json .}}']))
            version, system, architecture = data['ServerVersion'], data['OSType'], data['Architecture']
            if (not isinstance(version, str) or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+(?:[-+.][A-Za-z0-9.-]+)?', version)
                    or system != 'linux' or architecture not in ('aarch64', 'arm64', 'x86_64', 'amd64')):
                _fail()
            return {'engine_version': version, 'os': system, 'architecture': architecture}
        except (ValueError, KeyError, TypeError):
            raise RuntimeError(ERROR) from None

    def _context(self, context, descriptor, role):
        try:
            self._budget()
            if role not in ('v1', 'v2') or not isinstance(context, Path) or not isinstance(descriptor, dict):
                _fail()
            path = context.absolute()
            if '..' in context.parts or path.resolve(strict=True) != path or path.name != role:
                _fail()
            marker = path.parent / '.container-lab-owner'
            mode = marker.lstat()
            if not stat.S_ISREG(mode.st_mode) or mode.st_nlink != 1 or marker.read_text() != self.owner:
                _fail()
            expected = {'schema_version', 'app_commit', 'build_commit', 'files', 'payload_digest', 'main_sha', 'service_sha'}
            if set(descriptor) != expected or type(descriptor['schema_version']) is not int or descriptor['schema_version'] != 1:
                _fail()
            for name in ('app_commit', 'build_commit'):
                if not isinstance(descriptor[name], str) or not _HEX40.fullmatch(descriptor[name]):
                    _fail()
            files = descriptor['files']
            if not isinstance(files, dict) or not 4 <= len(files) <= 256 or 'release.json' in files:
                _fail()
            for name, digest in files.items():
                relative = PurePosixPath(name)
                if (not isinstance(name, str) or str(relative) != name or relative.is_absolute()
                        or not all(_NAME.fullmatch(part) for part in relative.parts)
                        or not (name in ('Dockerfile', 'requirements.txt') or name.startswith('app/'))
                        or not isinstance(digest, str) or not _HEX64.fullmatch(digest)):
                    _fail()
            if (descriptor['payload_digest'] != hashlib.sha256(_canonical(files)).hexdigest()
                    or descriptor['main_sha'] != files['app/main.py']
                    or descriptor['service_sha'] != files['app/services/book_service.py']
                    or files['Dockerfile'] != DOCKERFILE_SHA
                    or hashlib.sha256(DOCKERFILE_BYTES).hexdigest() != DOCKERFILE_SHA):
                _fail()
            seen = set()
            allowed_dirs = {str(parent) for name in files for parent in PurePosixPath(name).parents if str(parent) != '.'}
            total = 0
            for target in path.rglob('*'):
                self._budget()
                relative = target.relative_to(path).as_posix()
                info = target.lstat()
                if stat.S_ISDIR(info.st_mode):
                    if relative not in allowed_dirs:
                        _fail()
                    continue
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 4 * 1024 * 1024
                        or relative not in set(files) | {'release.json'}):
                    _fail()
                total += info.st_size
                if total > 4 * 1024 * 1024:
                    _fail()
                data = target.read_bytes()
                if relative == 'release.json':
                    if data != _canonical(descriptor):
                        _fail()
                elif hashlib.sha256(data).hexdigest() != files[relative]:
                    _fail()
                seen.add(relative)
            if seen != set(files) | {'release.json'}:
                _fail()
            return path
        except (OSError, ValueError, TypeError, KeyError):
            raise RuntimeError(ERROR) from None

    def build(self, context: Path, descriptor: dict, role: str) -> dict:
        if self._closed:
            _fail()
        path = self._context(context, descriptor, role)
        tag = f'ps-book-version-{self.owner}:{role}'
        if tag in self._images or self._present('image', tag):
            _fail()
        self._images[tag] = None  # Register before mutation; reconcile lost responses in cleanup.
        labels = self._labels() | {'org.personal-server.lab.role': role}
        for key in ('app_commit', 'build_commit', 'main_sha', 'service_sha'):
            labels[f'org.personal-server.lab.{key}'] = descriptor[key]
        self._build_labels[tag] = dict(labels)
        self._unknown_builds.add(tag)
        args = ['build', '--network', 'default', '--tag', tag]
        for key, value in sorted(labels.items()):
            args += ['--label', f'{key}={value}']
        args.append(str(path))
        self._run(args, cap=300)
        data = self._inspect('image', tag)
        try:
            image_id = data['Id']
            if (not _IMAGE.fullmatch(image_id) or not self._owned(data['Config']['Labels'])
                    or any(data['Config']['Labels'].get(key) != value for key, value in labels.items())
                    or data['Config'].get('User') != '10001:10001' or data['Os'] != 'linux'
                    or data['Architecture'] not in ('arm64', 'amd64', 'aarch64', 'x86_64')):
                _fail()
            result = {'id': image_id, 'role': role, 'os': data['Os'], 'architecture': data['Architecture']}
            result.update({key: descriptor[key] for key in ('app_commit', 'build_commit', 'main_sha', 'service_sha')})
            self._images[tag] = dict(result)
            self._unknown_builds.discard(tag)
            return result
        except (KeyError, TypeError):
            raise RuntimeError(ERROR) from None

    def volume(self) -> str:
        if self._closed:
            _fail()
        self._counter += 1
        name = f'ps-book-version-{self.owner}-data-{self._counter}'
        if self._present('volume', name):
            _fail()
        self._volumes[name] = None
        args = ['volume', 'create']
        for key, value in sorted(self._labels().items()):
            args += ['--label', f'{key}={value}']
        args.append(name)
        if self._run(args).strip() != name:
            _fail()
        data = self._inspect('volume', name)
        if data.get('Name') != name or not self._owned(data.get('Labels')):
            _fail()
        return name

    def _archive_path(self, path, *, new=False):
        path = Path(path)
        parent = path.parent.resolve(strict=True)
        marker = parent / '.container-lab-owner'
        if (path.parent != parent or path.name != 'image.tar' or marker.is_symlink()
                or not marker.is_file() or marker.read_text() != self.owner):
            _fail()
        if new:
            if path.exists() or path.is_symlink() or shutil.disk_usage(parent).free < 2 * ARCHIVE_LIMIT:
                _fail()
        else:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 < info.st_size <= ARCHIVE_LIMIT:
                _fail()
        return path

    def _archive_digest(self, path):
        value = hashlib.sha256()
        with path.open('rb') as source:
            for block in iter(lambda: source.read(1024 * 1024), b''):
                self._budget()
                value.update(block)
        return value.hexdigest()

    def verify_image_archive(self, path, image, expected):
        """Inspect bounded metadata without extracting any archive member."""
        try:
            path = self._archive_path(path)
            tag = f'ps-book-version-{self.owner}:{image["role"]}'
            if (image not in self._images.values() or expected.get('tag') != tag
                    or expected.get('image_id') != image['id']
                    or path.stat().st_size != expected.get('size_bytes')
                    or self._archive_digest(path) != expected.get('sha256')):
                _fail()
            with tarfile.open(path, 'r:') as archive:
                members = {}
                total = 0
                for member in archive:
                    self._budget()
                    name = PurePosixPath(member.name)
                    if (name.is_absolute() or '..' in name.parts or member.name in members
                            or not (member.isfile() or member.isdir()) or member.size < 0):
                        _fail()
                    total += member.size
                    if total > ARCHIVE_LIMIT or len(members) >= 4096:
                        _fail()
                    members[member.name] = member
                def json_member(name):
                    member = members.get(name)
                    if member is None or not member.isfile() or not 0 < member.size <= 2 * 1024 * 1024:
                        _fail()
                    return json.load(archive.extractfile(member))
                manifest = json_member('manifest.json')
                if (not isinstance(manifest, list) or len(manifest) != 1
                        or manifest[0].get('RepoTags') != [tag]):
                    _fail()
                item = manifest[0]
                config_name = item['Config']
                config = json_member(config_name)
                config_raw = archive.extractfile(members[config_name]).read()
                if ('sha256:' + hashlib.sha256(config_raw).hexdigest() != image['id']
                        or not self._owned(config.get('config', {}).get('Labels'))
                        or config.get('config', {}).get('User') != '10001:10001'
                        or not isinstance(item.get('Layers'), list)
                        or not item['Layers'] or any(name not in members or not members[name].isfile() for name in item['Layers'])):
                    _fail()
                def member_digest(name):
                    value = hashlib.sha256()
                    with archive.extractfile(members[name]) as source:
                        for block in iter(lambda: source.read(1024 * 1024), b''):
                            self._budget()
                            value.update(block)
                    return 'sha256:' + value.hexdigest()
                # An OCI index is another loader entry point. Pin its entire
                # descriptor graph to exactly the Docker manifest's config and
                # layers; checking only reference annotations is insufficient.
                if 'index.json' in members:
                    index = json_member('index.json')
                    layer_digests = [member_digest(name) for name in item['Layers']]
                    def descriptor_member(descriptor):
                        if set(descriptor) - {'mediaType', 'digest', 'size', 'annotations', 'platform'}:
                            _fail()
                        identity = descriptor.get('digest', '')
                        if not _IMAGE.fullmatch(identity):
                            _fail()
                        name = 'blobs/sha256/' + identity[7:]
                        member = members.get(name)
                        if (member is None or not member.isfile() or type(descriptor.get('size')) is not int
                                or member.size != descriptor['size'] or member_digest(name) != identity):
                            _fail()
                        for key, value in descriptor.get('annotations', {}).items():
                            if (key == 'io.containerd.image.name'
                                    and value not in (tag, 'docker.io/library/' + tag)):
                                _fail()
                            if (key == 'org.opencontainers.image.ref.name'
                                    and value not in (tag, 'docker.io/library/' + tag, image['role'])):
                                _fail()
                        return name
                    for depth in range(8):
                        entries = index.get('manifests')
                        if index.get('schemaVersion') != 2 or not isinstance(entries, list) or len(entries) != 1:
                            _fail()
                        descriptor = entries[0]
                        name = descriptor_member(descriptor)
                        media = descriptor.get('mediaType')
                        document = json_member(name)
                        if media in ('application/vnd.oci.image.index.v1+json', 'application/vnd.docker.distribution.manifest.list.v2+json'):
                            index = document
                            continue
                        if (media not in ('application/vnd.oci.image.manifest.v1+json', 'application/vnd.docker.distribution.manifest.v2+json')
                                or document.get('schemaVersion') != 2 or 'subject' in document or 'artifactType' in document
                                or document.get('config', {}).get('digest') != image['id']
                                or not isinstance(document.get('layers'), list)
                                or [layer.get('digest') for layer in document['layers']] != layer_digests):
                            _fail()
                        descriptor_member(document['config'])
                        for layer in document['layers']:
                            descriptor_member(layer)
                        break
                    else:
                        _fail()
                if 'repositories' in members:
                    repositories = json_member('repositories')
                    repository, role = tag.rsplit(':', 1)
                    if set(repositories) != {repository} or set(repositories[repository]) != {role}:
                        _fail()
            return dict(expected)
        except (OSError, ValueError, TypeError, KeyError, AttributeError, tarfile.TarError):
            raise RuntimeError(ERROR) from None

    def export_image(self, image, path):
        if self._closed or image not in self._images.values():
            _fail()
        path = self._archive_path(path, new=True)
        tag = f'ps-book-version-{self.owner}:{image["role"]}'
        data = self._inspect('image', tag)
        if data.get('Id') != image['id'] or not self._owned(data.get('Config', {}).get('Labels')):
            _fail()
        # File output avoids buffering a potentially large archive in memory.
        self._run(['image', 'save', '--output', str(path), tag], cap=120, limit=65536, bounded_file=path)
        path = self._archive_path(path)
        expected = {'sha256': self._archive_digest(path), 'size_bytes': path.stat().st_size,
                    'image_id': image['id'], 'tag': tag}
        return self.verify_image_archive(path, image, expected)

    def discard_source(self, container, volume, image):
        """Remove only this invocation's stopped source and unshared image."""
        name, record = self._record(container)
        tag = f'ps-book-version-{self.owner}:{image["role"]}'
        if (self._closed or record['volume'] != volume or record['image'] != image
                or not record.get('clean') or volume not in self._volumes or self._images.get(tag) != image):
            _fail()
        data = self._container(container)
        state = data.get('State', {})
        if state.get('Running') is not False or state.get('Pid') != 0:
            _fail()
        volume_data = self._inspect('volume', volume)
        image_data = self._inspect('image', tag)
        if (volume_data.get('Name') != volume or not self._owned(volume_data.get('Labels'))
                or image_data.get('Id') != image['id'] or not self._owned(image_data.get('Config', {}).get('Labels'))
                or image_data.get('RepoTags') != [tag] or image_data.get('RepoDigests') != []):
            _fail()
        self._run(['container', 'rm', data['Id']])
        if self._present('container', name):
            _fail()
        del self._containers[name]
        self._run(['volume', 'rm', volume])
        if self._present('volume', volume):
            _fail()
        del self._volumes[volume]
        self._run(['image', 'rm', '--no-prune', tag])
        if self._present('image', tag) or self._image_id_present(image['id']):
            _fail()
        return {'container_absent': True, 'volume_absent': True, 'image_absent': True,
                'build_layer_cache_purged': False}

    def import_image(self, image, path, expected):
        if self._closed:
            _fail()
        self.verify_image_archive(path, image, expected)
        tag = expected['tag']
        if self._present('image', tag) or self._image_id_present(image['id']):
            _fail()
        # Registration survives source deletion, so an uncertain load response
        # is reconciled by the existing ownership-scoped cleanup path.
        self._unknown_builds.add(tag)
        self._run(['image', 'load', '--input', str(path)], cap=120, limit=65536)
        data = self._inspect('image', tag)
        labels = data.get('Config', {}).get('Labels', {})
        if (data.get('Id') != image['id'] or data.get('RepoTags') != [tag] or data.get('RepoDigests') != []
                or not self._owned(labels) or any(labels.get(key) != value for key, value in self._build_labels[tag].items())):
            _fail()
        self._unknown_builds.discard(tag)
        return dict(image)

    def _record(self, identity):
        if not isinstance(identity, str):
            _fail()
        for name, record in self._containers.items():
            if identity == name or identity == record.get('id'):
                return name, record
        _fail()

    def _container(self, identity, *, cleanup=False):
        name, record = self._record(identity)
        data = self._inspect('container', name, cleanup=cleanup)
        try:
            actual_id = data['Id']
            if (not _HEX64.fullmatch(actual_id) or data['Name'] != '/' + name
                    or not self._owned(data['Config']['Labels'])
                    or data['Image'] != record['image']['id']
                    or data['Config']['Labels'].get('org.personal-server.lab.role') != record['role']
                    or (record.get('id') is not None and record['id'] != actual_id)):
                _fail()
            record['id'] = actual_id
            return data
        except (KeyError, TypeError):
            raise RuntimeError(ERROR) from None

    def _verify_runtime(self, data, record):
        try:
            host, config = data['HostConfig'], data['Config']
            mounts = data['Mounts']
            if (config['User'] != '10001:10001' or host['NetworkMode'] != 'none'
                    or host.get('Binds') or host.get('PortBindings') or host.get('PublishAllPorts')
                    or not host['ReadonlyRootfs'] or host.get('Privileged')
                    or host.get('CapAdd') or host['CapDrop'] != ['ALL']
                    or host['SecurityOpt'] not in (['no-new-privileges'], ['no-new-privileges=true'])
                    or host['PidsLimit'] != 64 or host['Memory'] != 256 * 1024 * 1024
                    or host['NanoCpus'] != 1000000000
                    or host.get('Tmpfs') != {'/tmp': 'rw,noexec,nosuid,size=16m'}
                    or len(mounts) != 1 or mounts[0]['Type'] != 'volume'
                    or mounts[0]['Name'] != record['volume'] or mounts[0]['Destination'] != '/app/data'
                    or mounts[0].get('RW') is not True
                    or config.get('Cmd') != ['uvicorn', 'app.main:app', '--host', '0.0.0.0', '--port', '8003']):
                _fail()
            required = {'BOOK_MEMO_DB_PATH=/app/data/fixture.sqlite3', 'DEMO_MODE=0',
                        'PYTHONDONTWRITEBYTECODE=1', 'NO_PROXY=127.0.0.1,localhost'}
            if not required.issubset(set(config['Env'])):
                _fail()
        except (KeyError, TypeError):
            raise RuntimeError(ERROR) from None

    def start(self, image: dict, volume: str, role: str) -> str:
        if (self._closed or any(not record.get('clean') for record in self._containers.values())
                or not isinstance(image, dict) or role not in ('v1', 'v2') or image.get('role') != role
                or image not in self._images.values() or not isinstance(volume, str) or volume not in self._volumes):
            _fail()
        # Any running actor of this invocation prevents a second writer.
        actors = self._run(['container', 'ls', '--no-trunc', '--filter', f'label={OWNER_LABEL}={self.owner}', '--format', '{{.ID}}'])
        if actors.strip():
            _fail()
        volume_data = self._inspect('volume', volume)
        if volume_data.get('Name') != volume or not self._owned(volume_data.get('Labels')):
            _fail()
        self._counter += 1
        name = f'ps-book-version-{self.owner}-{role}-{self._counter}'
        if self._present('container', name):
            _fail()
        self._containers[name] = {'id': None, 'image': dict(image), 'volume': volume, 'role': role, 'clean': False}
        args = ['run', '--detach', '--name', name]
        for key, value in sorted((self._labels() | {'org.personal-server.lab.role': role}).items()):
            args += ['--label', f'{key}={value}']
        args += ['--network', 'none', '--read-only', '--tmpfs', '/tmp:rw,noexec,nosuid,size=16m',
                 '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--pids-limit', '64',
                 '--memory', '256m', '--cpus', '1', '--user', '10001:10001',
                 '--mount', f'type=volume,source={volume},target=/app/data']
        for value in ('BOOK_MEMO_DB_PATH=/app/data/fixture.sqlite3', 'DEMO_MODE=0',
                      'PYTHONDONTWRITEBYTECODE=1', 'NO_PROXY=127.0.0.1,localhost'):
            args += ['--env', value]
        args.append(image['id'])
        identity = self._run(args).strip()
        if not _HEX64.fullmatch(identity):
            _fail()
        self._containers[name]['id'] = identity
        data = self._container(identity)
        self._verify_runtime(data, self._containers[name])
        if not data.get('State', {}).get('Running'):
            _fail()
        return identity

    def stop(self, container: str) -> None:
        self._stop(container, cleanup=False, require_clean=True)

    def _stop(self, container, *, cleanup, require_clean):
        _, record = self._record(container)
        record['clean'] = False
        data = self._container(container, cleanup=cleanup)
        if data.get('State', {}).get('Running'):
            self._run(['container', 'stop', '--signal', 'SIGTERM', '--time', '5', data['Id']], cleanup=cleanup)
            data = self._container(container, cleanup=cleanup)
        state = data.get('State', {})
        if (state.get('Running') is not False or state.get('Pid') != 0
                or state.get('Status') not in (('exited',) if require_clean else ('exited', 'created'))
                or (require_clean and (state.get('ExitCode') not in (0, 143)
                                      or state.get('OOMKilled') is not False
                                      or state.get('Error') != ''))):
            _fail()

        if require_clean:
            _, record = self._record(container)
            # Uvicorn 0.34 re-raises captured SIGTERM after graceful shutdown,
            # so 143 is an expected termination status alongside 0. Database
            # integrity/persistence are verified separately by the caller.
            record['clean'] = True

    def probe(self, container: str, operation: str) -> dict:
        if not isinstance(operation, str) or operation not in {'seed', 'snapshot', 'write_v2', 'write_v1', 'health', 'ready', 'missing', 'read', 'identity'}:
            _fail()
        data = self._container(container)
        _, record = self._record(container)
        self._verify_runtime(data, record)
        if not data.get('State', {}).get('Running'):
            _fail()
        # Import at call time to keep ownership of the product probe separate.
        from container_probe import PROBE_SCRIPT
        try:
            result = json.loads(self._run(['exec', data['Id'], 'python', '-c', PROBE_SCRIPT, operation]))
            if not isinstance(result, dict):
                _fail()
            return result
        except (ValueError, TypeError):
            raise RuntimeError(ERROR) from None

    def _snapshot_transfer(self, container: str, operation: str, payload=None) -> dict:
        """Fixed synthetic data transfer; no caller SQL, filesystem path or URL."""
        data = self._container(container)
        _, record = self._record(container)
        self._verify_runtime(data, record)
        if not data.get('State', {}).get('Running') or operation not in ('export', 'restore'):
            _fail()
        from container_probe import RESTORE_SCRIPT
        args = ['exec', data['Id'], 'python', '-c', RESTORE_SCRIPT, operation]
        if operation == 'restore':
            if not isinstance(payload, dict):
                _fail()
            encoded = _canonical(payload)
            if not 1 <= len(encoded) <= 65536:
                _fail()
            args.append(base64.b64encode(encoded).decode('ascii'))
        try:
            result = json.loads(self._run(args, limit=131072))
            if not isinstance(result, dict) or 'probe_error' in result:
                _fail()
            return result
        except (ValueError, TypeError):
            raise RuntimeError(ERROR) from None

    def export_snapshot(self, container: str) -> dict:
        return self._snapshot_transfer(container, 'export')

    def restore_snapshot(self, container: str, payload: dict) -> dict:
        return self._snapshot_transfer(container, 'restore', payload)

    def restore_evidence(self, container: str) -> dict:
        """Return only bounded runtime identity/mount proof, without environment."""
        data = self._container(container)
        _, record = self._record(container)
        self._verify_runtime(data, record)
        volume = self._inspect('volume', record['volume'])
        if volume.get('Name') != record['volume'] or not self._owned(volume.get('Labels')):
            _fail()
        return {'container_id': data['Id'], 'image_id': data['Image'],
                'volume_name': record['volume'], 'mount_volume_names': [item['Name'] for item in data['Mounts']],
                'owner_verified': True, 'user': data['Config']['User'], 'network': 'none',
                'readonly_root': True, 'published_ports': False}

    def cleanup(self) -> dict:
        """A fresh cleanup budget remains available after cancellation/failure."""
        self._closed = True
        self._cleanup_deadline = time.monotonic() + 90
        counts = {'containers_removed': 0, 'volumes_removed': 0, 'images_removed': 0}
        failed = False
        for name in list(self._containers):
            try:
                if self._present('container', name, cleanup=True):
                    self._stop(name, cleanup=True, require_clean=False)
                    data = self._container(name, cleanup=True)
                    self._run(['container', 'rm', data['Id']], cleanup=True)
                    if self._present('container', name, cleanup=True):
                        _fail()
                    counts['containers_removed'] += 1
                del self._containers[name]
            except RuntimeError:
                failed = True
        # Even a stopped or uncertain remaining container retains its volume.
        if not self._containers:
            for name in list(self._volumes):
                try:
                    if self._present('volume', name, cleanup=True):
                        data = self._inspect('volume', name, cleanup=True)
                        if data.get('Name') != name or not self._owned(data.get('Labels')):
                            _fail()
                        self._run(['volume', 'rm', name], cleanup=True)
                        if self._present('volume', name, cleanup=True):
                            _fail()
                        counts['volumes_removed'] += 1
                    del self._volumes[name]
                except RuntimeError:
                    failed = True
            for tag in list(self._images):
                try:
                    recorded = self._images[tag]
                    identity = None if recorded is None else recorded['id']
                    removed = False
                    if self._present('image', tag, cleanup=True):
                        data = self._inspect('image', tag, cleanup=True)
                        actual_id = data.get('Id', '')
                        if not _IMAGE.fullmatch(actual_id) or not self._owned(data.get('Config', {}).get('Labels')):
                            _fail()
                        if identity is not None and identity != actual_id:
                            _fail()
                        expected_labels = self._build_labels.get(tag, self._labels())
                        if any(data.get('Config', {}).get('Labels', {}).get(key) != value
                               for key, value in expected_labels.items()):
                            _fail()
                        identity = actual_id
                        if recorded is None:
                            self._images[tag] = {'id': identity}
                        self._unknown_builds.discard(tag)
                        self._run(['image', 'rm', '--no-prune', tag], cleanup=True)
                        if self._present('image', tag, cleanup=True):
                            _fail()
                        removed = True
                    if tag in self._unknown_builds:
                        # Cancelling the CLI does not prove a daemon-side build
                        # has stopped. An absent tag alone is insufficient.
                        _fail()
                    if identity is not None and self._image_id_present(identity, cleanup=True):
                        data = self._inspect('image', identity, cleanup=True)
                        if data.get('Id') != identity or not self._owned(data.get('Config', {}).get('Labels')):
                            _fail()
                        references = data.get('RepoTags')
                        digests = data.get('RepoDigests')
                        if (not isinstance(references, list) or not isinstance(digests, list)
                                or any(not isinstance(reference, str) or reference not in self._images
                                       for reference in references) or digests):
                            # Non-force ID deletion can still remove a sole tag.
                            # Protect every unregistered tag and registry digest.
                            _fail()
                        # --no-prune also preserves untagged parents/build cache.
                        self._run(['image', 'rm', '--no-prune', identity], cleanup=True)
                        if self._image_id_present(identity, cleanup=True):
                            _fail()
                        removed = True
                    if removed:
                        counts['images_removed'] += 1
                    del self._images[tag]
                except RuntimeError:
                    failed = True
        residual = {'containers': len(self._containers), 'volumes': len(self._volumes),
                    'images': len(self._images), 'unknown_builds': len(self._unknown_builds)}
        return {'ok': not failed and not any(residual.values()), **counts, 'residual_counts': residual}
