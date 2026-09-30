"""Stable, read-only representations of the saved Book Memo library."""

import json
from typing import Any

from app.services import book_service


_BOOK_FIELDS = (
    "id", "isbn", "external_id", "title", "authors", "publisher",
    "published_date", "description", "thumbnail", "preview_url", "source",
    "reading_status", "current_page", "current_chapter", "progress_percent",
    "created_at", "updated_at",
)
_CHAPTER_FIELDS = (
    "id", "book_id", "title", "position", "is_done", "comment",
    "created_at", "updated_at",
)
_MEMO_FIELDS = (
    "id", "book_id", "chapter_id", "title", "content", "page",
    "created_at", "updated_at",
)


def export_records(selected_ids: set[int] | None = None) -> dict[str, Any]:
    """Read saved records from one SQLite snapshot without UI-derived fields."""
    book_service.init_db()
    ids = sorted(selected_ids) if selected_ids is not None else []
    if selected_ids is not None and not ids:
        raise ValueError("내보낼 책을 선택해 주세요.")
    placeholders = ", ".join("?" for _ in ids)
    book_filter = f" WHERE id IN ({placeholders})" if selected_ids is not None else ""
    child_filter = f" WHERE book_id IN ({placeholders})" if selected_ids is not None else ""
    tag_filter = (
        f" WHERE memo_id IN (SELECT id FROM book_memos WHERE book_id IN ({placeholders}))"
        if selected_ids is not None else ""
    )
    with book_service._connect() as connection:
        connection.execute("BEGIN")
        books = connection.execute(
            f"SELECT {', '.join(_BOOK_FIELDS)} FROM books{book_filter} ORDER BY id", ids
        ).fetchall()
        if selected_ids is not None and len(books) != len(ids):
            raise ValueError("선택한 책을 찾을 수 없습니다.")
        chapters = connection.execute(
            f"SELECT {', '.join(_CHAPTER_FIELDS)} FROM book_chapters "
            f"{child_filter} ORDER BY book_id, position, id", ids
        ).fetchall()
        memos = connection.execute(
            f"SELECT {', '.join(_MEMO_FIELDS)} FROM book_memos{child_filter} ORDER BY book_id, id", ids
        ).fetchall()
        tag_rows = connection.execute(
            f"SELECT memo_id, tag FROM memo_tags{tag_filter} ORDER BY memo_id, position, tag_key", ids
        ).fetchall()

    chapters_by_book: dict[int, list[dict[str, Any]]] = {}
    for row in chapters:
        chapters_by_book.setdefault(row["book_id"], []).append(dict(row))

    tags_by_memo: dict[int, list[str]] = {}
    for row in tag_rows:
        tags_by_memo.setdefault(row["memo_id"], []).append(row["tag"])

    memos_by_book: dict[int, list[dict[str, Any]]] = {}
    for row in memos:
        memo = dict(row)
        memo["tags"] = tags_by_memo.get(row["id"], [])
        memos_by_book.setdefault(row["book_id"], []).append(memo)

    return {
        "metadata": {"service": "book-memo", "schema_version": 1},
        "records": [
            {
                **dict(book),
                "chapters": chapters_by_book.get(book["id"], []),
                "memos": memos_by_book.get(book["id"], []),
            }
            for book in books
        ],
    }


def export_json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def _inline(value: Any) -> str:
    """Keep user text from creating extra Markdown structure."""
    escaped = str(value if value is not None else "")
    for char in "\\`*_{}[]<>#+-.!|":
        escaped = escaped.replace(char, "\\" + char)
    return escaped.replace("\r", " ").replace("\n", " ")


def _block(value: Any) -> list[str]:
    text = str(value if value is not None else "")
    fence = "`" * max(3, _longest_backtick_run(text) + 1)
    return [fence, text, fence]


def _longest_backtick_run(value: str) -> int:
    longest = current = 0
    for char in value:
        current = current + 1 if char == "`" else 0
        longest = max(longest, current)
    return longest


def export_markdown(data: dict[str, Any]) -> str:
    lines = ["# Book Memo 내보내기", "", f"책: {len(data['records'])}권", ""]
    for book in data["records"]:
        lines.extend([f"## 책 {book['id']}: {_inline(book['title'])}", ""])
        for field, label in (
            ("isbn", "ISBN"), ("external_id", "외부 ID"),
            ("authors", "저자"), ("publisher", "출판사"),
            ("published_date", "출판일"), ("thumbnail", "표지 URL"),
            ("preview_url", "미리보기 URL"), ("source", "출처"),
            ("reading_status", "읽기 상태"), ("current_page", "현재 쪽"),
            ("current_chapter", "현재 장"), ("progress_percent", "진행률 (%)"),
            ("created_at", "생성 시각"), ("updated_at", "수정 시각"),
        ):
            lines.append(f"- {label}: {_inline(book[field])}")
        lines.extend(["", "### 설명", *_block(book["description"]), "", "### 목차", ""])
        for chapter in book["chapters"]:
            lines.extend([
                f"#### 목차 {chapter['id']}: {_inline(chapter['title'])}", "",
                f"- 순서: {chapter['position']}",
                f"- 완료: {'예' if chapter['is_done'] else '아니오'}",
                f"- 생성 시각: {_inline(chapter['created_at'])}",
                f"- 수정 시각: {_inline(chapter['updated_at'])}",
                "- 코멘트:", *_block(chapter["comment"]), "",
            ])
        lines.extend(["### 메모", ""])
        for memo in book["memos"]:
            lines.extend([
                f"#### 메모 {memo['id']}: {_inline(memo['title'])}", "",
                f"- 목차 ID: {_inline(memo['chapter_id'])}",
                f"- 쪽: {memo['page']}",
                f"- 태그: {', '.join(_inline(tag) for tag in memo['tags'])}",
                f"- 생성 시각: {_inline(memo['created_at'])}",
                f"- 수정 시각: {_inline(memo['updated_at'])}",
                "- 내용:", *_block(memo["content"]), "",
            ])
    return "\n".join(lines).rstrip() + "\n"
