import html
import json
import re
from typing import Any


def export_json(records: list[dict[str, Any]]) -> str:
    payload = {
        "metadata": {
            "service": "youtube-memo",
            "schema_version": 1,
            "video_count": len(records),
            "memo_count": sum(len(record["memos"]) for record in records),
        },
        "records": records,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def export_markdown(records: list[dict[str, Any]]) -> str:
    lines = [
        "# YouTube Memo 내보내기",
        "",
        f"영상: {len(records)}개 · 메모: {sum(len(record['memos']) for record in records)}개",
        "",
    ]
    for video in records:
        lines.extend([
            f"## 영상 {video['id']}: {_inline_text(video['title'])}",
            "",
            f"- YouTube ID: {_inline_code(video['youtube_id'])}",
            f"- URL: {_inline_code(video['url'])}",
            f"- 생성: {_inline_text(video['created_at'])}",
            f"- 수정: {_inline_text(video['updated_at'])}",
            "",
        ])
        for memo in video["memos"]:
            lines.extend([
                f"### 메모 {memo['id']}: {_inline_text(memo['title'])}",
                "",
                f"- 생성: {_inline_text(memo['created_at'])}",
                f"- 수정: {_inline_text(memo['updated_at'])}",
                f"- 태그: {', '.join(_inline_text(tag) for tag in memo['tags']) or '없음'}",
                f"- 타임스탬프: {', '.join(_inline_text(item['text']) for item in memo['timestamps']) or '없음'}",
                "",
                _code_block(memo["content"]),
                "",
            ])
    return "\n".join(lines).rstrip() + "\n"


def _inline_text(value: str) -> str:
    flattened = " ".join(value.split())
    return re.sub(r"([\\`*_{}\[\]()#+.!|>~-])", r"\\\1", html.escape(flattened, quote=False))


def _inline_code(value: str) -> str:
    delimiter = "`" * (max((len(match.group()) for match in re.finditer(r"`+", value)), default=0) + 1)
    return f"{delimiter} {value.replace(chr(10), ' ').replace(chr(13), ' ')} {delimiter}"


def _code_block(content: str) -> str:
    fence = "`" * max(3, max((len(match.group()) for match in re.finditer(r"`+", content)), default=0) + 1)
    return f"{fence}\n{content}\n{fence}"
