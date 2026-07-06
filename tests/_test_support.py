from __future__ import annotations

import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SERVICE_DIRS = {
    str(REPO_ROOT / "portal-web"),
    str(REPO_ROOT / "system-agent"),
    str(REPO_ROOT / "crawler-worker"),
    str(REPO_ROOT / "youtube-memo"),
    str(REPO_ROOT / "book-memo"),
}


def prepare_service_import(service_dir: str) -> None:
    service_path = str(REPO_ROOT / service_dir)
    # 서비스별 `app` 패키지가 서로 덮어쓰지 않도록 공통 경로를 먼저 제거함
    sys.path = [path for path in sys.path if path not in SERVICE_DIRS]
    sys.path.insert(0, service_path)

    for module_name in list(sys.modules):
        if module_name == "app" or module_name.startswith("app."):
            del sys.modules[module_name]
