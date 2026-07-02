from __future__ import annotations

import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def prepare_service_import(service_dir: str) -> None:
    service_path = str(REPO_ROOT / service_dir)
    sys.path = [path for path in sys.path if path != service_path]
    sys.path.insert(0, service_path)

    for module_name in list(sys.modules):
        if module_name == "app" or module_name.startswith("app."):
            del sys.modules[module_name]
