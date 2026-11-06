"""Select the shared runtime only in an explicitly configured HA deployment."""
import os


def runtime():
    if "HA_PG_CONFIG" not in os.environ:
        return None
    import ha_runtime
    return ha_runtime
