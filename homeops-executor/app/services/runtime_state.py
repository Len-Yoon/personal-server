"""Read the N100 ownership marker on each operation, including atomic replacements."""
from __future__ import annotations

import os
from pathlib import Path
import stat


RUNTIME_SERVICES = frozenset({'crawler-worker', 'youtube-memo', 'book-memo'})


def compose_owned_services() -> frozenset[str]:
    configured = os.getenv('HOMEOPS_RUNTIME_STATE_PATH', '')
    if not configured:
        # Development Compose has no host marker; N100 always configures one.
        return RUNTIME_SERVICES
    path = Path(configured)
    try:
        if not path.is_absolute():
            return frozenset()
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(parent)
            if info.st_uid != 0 or info.st_mode & 0o022:
                return frozenset()
            descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            try:
                info = os.fstat(descriptor)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                        or info.st_mode & 0o022 or info.st_nlink != 1):
                    return frozenset()
                raw = os.read(descriptor, 4097)
            finally:
                os.close(descriptor)
        finally:
            os.close(parent)
        if len(raw) > 4096 or b'\0' in raw:
            return frozenset()
        values: dict[str, str] = {}
        for row in raw.decode('utf-8').splitlines():
            service, value = row.split('=')
            if service not in RUNTIME_SERVICES or service in values or value not in {'compose', 'k3s'}:
                return frozenset()
            values[service] = value
        if set(values) != RUNTIME_SERVICES:
            return frozenset()
        return frozenset(service for service, value in values.items() if value == 'compose')
    except (OSError, ValueError):
        # Missing/invalid ownership never grants a Docker writer restart.
        return frozenset()
