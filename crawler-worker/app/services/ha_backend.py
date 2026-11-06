"""Optional PostgreSQL runtime; legacy deployments need no shared package."""
import os
from functools import wraps


def runtime():
    if "HA_PG_CONFIG" not in os.environ:
        return None
    import ha_runtime
    return ha_runtime


def worker_operation(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        backend = runtime()
        if backend is None:
            return function(*args, **kwargs)
        with backend.worker_scope("crawler-worker"):
            return function(*args, **kwargs)
    return wrapped
