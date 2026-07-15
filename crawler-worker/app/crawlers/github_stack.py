from __future__ import annotations

import json
from datetime import datetime
from urllib.parse import quote_plus
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


GITHUB_API_URL = "https://api.github.com/search/repositories"
STACK_KEYWORDS = (
    "react",
    "next.js",
    "nextjs",
    "typescript",
    "fastapi",
    "spring boot",
    "spring-boot",
    "kubernetes",
    "rust",
    "golang",
    " go ",
)


def search_github_stack_repositories(limit: int = 20) -> list[dict]:
    today = datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat()
    query = (
        "(react OR nextjs OR typescript OR fastapi OR spring-boot OR "
        f"kubernetes OR rust OR golang) in:name,description,readme pushed:>={today}"
    )
    url = f"{GITHUB_API_URL}?q={quote_plus(query)}&sort=stars&order=desc&per_page={min(limit, 50)}"
    request = Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "personal-server-news",
        },
    )

    try:
        with urlopen(request, timeout=8) as response:
            payload = json.load(response)
    except Exception:
        return []

    repositories = []
    for repository in payload.get("items", []):
        if not _is_stack_repository(repository):
            continue
        repositories.append(_to_article(repository))

    return repositories[:limit]


def _is_stack_repository(repository: dict) -> bool:
    searchable = " ".join(
        str(repository.get(field, ""))
        for field in ("full_name", "name", "description", "language")
    ).casefold()
    return any(keyword in searchable for keyword in STACK_KEYWORDS)


def _to_article(repository: dict) -> dict:
    full_name = str(repository.get("full_name", "repository"))
    description = str(repository.get("description") or "GitHub 인기 저장소")
    stars = int(repository.get("stargazers_count") or 0)
    language = str(repository.get("language") or "")
    updated_at = str(repository.get("pushed_at") or repository.get("updated_at") or "")
    stack = language or "Stack"

    return {
        "category": "KR_STACK",
        "title": full_name,
        "title_ko": full_name,
        "title_original": full_name,
        "url": repository.get("html_url", ""),
        "source": "GitHub",
        "provider": "GitHub Repository Search",
        "published_at": updated_at,
        "published_at_sort": updated_at,
        "summary": f"{description} · {stack} · stars {stars:,}",
        "topics": [stack],
    }
