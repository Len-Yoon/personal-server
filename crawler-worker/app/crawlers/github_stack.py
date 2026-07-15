from __future__ import annotations

import json
from datetime import datetime, timedelta
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
DEFAULT_STACK_REPOSITORIES = (
    {
        "full_name": "facebook/react",
        "html_url": "https://github.com/facebook/react",
        "description": "사용자 인터페이스를 위한 React 공식 저장소",
        "language": "JavaScript",
        "topics": ["react"],
    },
    {
        "full_name": "vercel/next.js",
        "html_url": "https://github.com/vercel/next.js",
        "description": "React 기반 웹 애플리케이션 프레임워크",
        "language": "JavaScript",
        "topics": ["nextjs", "react"],
    },
    {
        "full_name": "microsoft/TypeScript",
        "html_url": "https://github.com/microsoft/TypeScript",
        "description": "JavaScript에 정적 타입을 제공하는 언어",
        "language": "TypeScript",
        "topics": ["typescript"],
    },
    {
        "full_name": "fastapi/fastapi",
        "html_url": "https://github.com/fastapi/fastapi",
        "description": "Python 기반 고성능 API 프레임워크",
        "language": "Python",
        "topics": ["fastapi", "python"],
    },
    {
        "full_name": "spring-projects/spring-boot",
        "html_url": "https://github.com/spring-projects/spring-boot",
        "description": "Spring 기반 애플리케이션 개발 프레임워크",
        "language": "Java",
        "topics": ["spring-boot", "java"],
    },
    {
        "full_name": "kubernetes/kubernetes",
        "html_url": "https://github.com/kubernetes/kubernetes",
        "description": "컨테이너 오케스트레이션 플랫폼",
        "language": "Go",
        "topics": ["kubernetes", "cloud-native"],
    },
    {
        "full_name": "rust-lang/rust",
        "html_url": "https://github.com/rust-lang/rust",
        "description": "안전성과 성능을 위한 시스템 프로그래밍 언어",
        "language": "Rust",
        "topics": ["rust"],
    },
    {
        "full_name": "golang/go",
        "html_url": "https://github.com/golang/go",
        "description": "Go 프로그래밍 언어 공식 저장소",
        "language": "Go",
        "topics": ["golang", "go"],
    },
)


def search_github_stack_repositories(limit: int = 20) -> list[dict]:
    today = datetime.now(ZoneInfo("Asia/Seoul")).date()
    for days in (7, 30):
        query = _build_query(today - timedelta(days=days))
        payload = _request_repositories(query, limit)
        repositories = [
            _to_article(repository, source_mode="github")
            for repository in payload.get("items", [])
            if _is_stack_repository(repository)
        ]
        if repositories:
            return repositories[:limit]

    return [
        _to_article(repository, source_mode="fallback")
        for repository in DEFAULT_STACK_REPOSITORIES[:limit]
    ]


def _build_query(start_date) -> str:
    return (
        "(react OR nextjs OR typescript OR fastapi OR spring-boot OR "
        f"kubernetes OR rust OR golang) in:name,description,readme pushed:>={start_date}"
    )


def _request_repositories(query: str, limit: int) -> dict:
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
            return json.load(response)
    except Exception:
        return {}


def _is_stack_repository(repository: dict) -> bool:
    searchable = " ".join(
        str(repository.get(field, ""))
        for field in ("full_name", "name", "description", "language", "topics")
    ).casefold()
    return any(keyword in searchable for keyword in STACK_KEYWORDS)


def _to_article(repository: dict, source_mode: str = "github") -> dict:
    full_name = str(repository.get("full_name", "repository"))
    description = str(repository.get("description") or "GitHub 인기 저장소")
    stars = int(repository.get("stargazers_count") or 0)
    language = str(repository.get("language") or "")
    updated_at = str(repository.get("pushed_at") or repository.get("updated_at") or "")
    stack = language or "Stack"

    stars_text = f"stars {stars:,}" if stars else "대표 스택"
    return {
        "category": "KR_STACK",
        "title": full_name,
        "title_ko": full_name,
        "title_original": full_name,
        "url": repository.get("html_url", ""),
        "source": "GitHub" if source_mode == "github" else "기본 스택 목록",
        "provider": (
            "GitHub Repository Search"
            if source_mode == "github"
            else "Stack Catalog Fallback"
        ),
        "published_at": updated_at,
        "published_at_sort": updated_at,
        "summary": f"{description} · {stack} · {stars_text}",
        "topics": repository.get("topics") or [stack],
        "source_status": source_mode,
    }
