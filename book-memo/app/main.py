import os
import secrets

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.services.book_search import search_books
from app.services.book_service import (
    create_chapter,
    create_chapters,
    create_memo,
    create_or_get_book,
    delete_book,
    delete_chapter,
    delete_memo,
    get_book,
    list_books,
    list_chapters,
    list_memos,
    search_books_and_memos,
    update_chapter,
    update_chapter_comment,
    update_chapter_statuses,
    update_progress,
)
from app.services.toc_service import fetch_toc_candidates


app = FastAPI(title="Book Memo")

app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


@app.get("/")
def home(
    request: Request,
    q: str = Query(default=""),
):
    results = []
    error = ""

    if q.strip():
        try:
            results = search_books(q)
        except Exception as exc:
            error = str(exc)

    books = list_books()

    return templates.TemplateResponse(
        "home.html",
        {
            "request": request,
            "title": "책 메모장",
            "query": q,
            "results": results,
            "books": books,
            "chapters_by_book": {book["id"]: list_chapters(book["id"]) for book in books},
            "error": error,
            "has_aladin_key": bool(os.getenv("ALADIN_TTB_KEY", "").strip()),
            "statuses": ["읽을 예정", "읽는 중", "완료", "보류"],
        },
    )


@app.post("/books")
def create_book(
    isbn: str = Form(...),
    external_id: str = Form(default=""),
    title: str = Form(...),
    authors: str = Form(default=""),
    publisher: str = Form(default=""),
    published_date: str = Form(default=""),
    description: str = Form(default=""),
    thumbnail: str = Form(default=""),
    preview_url: str = Form(default=""),
    source: str = Form(default="google_books"),
    titles: list[str] = Form(default=[]),
):
    book = create_or_get_book(
        {
            "isbn": isbn,
            "external_id": external_id,
            "title": title,
            "authors": authors,
            "publisher": publisher,
            "published_date": published_date,
            "description": description,
            "thumbnail": thumbnail,
            "preview_url": preview_url,
            "source": source,
        }
    )

    if titles:
        create_chapters(book_id=book["id"], titles=titles)

    return RedirectResponse(url=f"/books/{book['id']}", status_code=303)


@app.post("/toc-candidates")
def get_unsaved_book_toc_candidates(
    isbn: str = Form(default=""),
    title: str = Form(default=""),
):
    return fetch_toc_candidates(
        {
            "isbn": isbn,
            "title": title,
        }
    )


@app.get("/books/{book_id}")
def book_detail(request: Request, book_id: int):
    book = get_book(book_id)

    if not book:
        raise HTTPException(status_code=404, detail="Book not found")

    return templates.TemplateResponse(
        "book_detail.html",
        {
            "request": request,
            "title": book["title"],
            "book": book,
            "chapters": list_chapters(book_id),
            "memos": list_memos(book_id),
            "statuses": ["읽을 예정", "읽는 중", "완료", "보류"],
        },
    )


@app.post("/books/{book_id}/delete")
def delete_saved_book(
    book_id: int,
    delete_password: str = Form(default=""),
):
    _require_delete_password(delete_password)

    if not delete_book(book_id):
        raise HTTPException(status_code=404, detail="Book not found")

    return RedirectResponse(url="/", status_code=303)


@app.post("/books/{book_id}/progress")
def update_book_progress(
    book_id: int,
    reading_status: str = Form(...),
    current_page: int = Form(default=0),
    current_chapter: str = Form(default=""),
    progress_percent: int = Form(default=0),
    redirect_to: str = Form(default=""),
):
    if not get_book(book_id):
        raise HTTPException(status_code=404, detail="Book not found")

    update_progress(
        book_id=book_id,
        reading_status=reading_status,
        current_page=current_page,
        current_chapter=current_chapter,
        progress_percent=progress_percent,
    )

    return RedirectResponse(url=_safe_redirect(redirect_to) or f"/books/{book_id}", status_code=303)


@app.post("/books/{book_id}/chapters")
def create_book_chapter(
    book_id: int,
    title: str = Form(...),
):
    if not get_book(book_id):
        raise HTTPException(status_code=404, detail="Book not found")

    try:
        create_chapter(book_id=book_id, title=title)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    return RedirectResponse(url=f"/books/{book_id}", status_code=303)


@app.post("/books/{book_id}/chapters/bulk")
def create_book_chapters_bulk(
    book_id: int,
    titles: list[str] = Form(default=[]),
):
    if not get_book(book_id):
        raise HTTPException(status_code=404, detail="Book not found")

    try:
        create_chapters(book_id=book_id, titles=titles)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    return RedirectResponse(url=f"/books/{book_id}", status_code=303)


@app.post("/books/{book_id}/chapter-statuses")
def update_book_chapter_statuses(
    book_id: int,
    done_chapter_ids: list[int] = Form(default=[]),
    redirect_to: str = Form(default=""),
):
    if not get_book(book_id):
        raise HTTPException(status_code=404, detail="Book not found")

    update_chapter_statuses(book_id=book_id, done_chapter_ids=done_chapter_ids)

    return RedirectResponse(url=_safe_redirect(redirect_to) or f"/books/{book_id}", status_code=303)


@app.get("/books/{book_id}/toc-candidates")
def get_book_toc_candidates(book_id: int):
    book = get_book(book_id)

    if not book:
        raise HTTPException(status_code=404, detail="Book not found")

    return fetch_toc_candidates(book)


@app.post("/chapters/{chapter_id}")
def update_book_chapter(
    chapter_id: int,
    is_done: str = Form(default="0"),
    comment: str = Form(default=""),
    redirect_to: str = Form(default=""),
):
    book_id = update_chapter(
        chapter_id=chapter_id,
        is_done=is_done == "1",
        comment=comment,
    )

    if not book_id:
        raise HTTPException(status_code=404, detail="Chapter not found")

    return RedirectResponse(url=_safe_redirect(redirect_to) or f"/books/{book_id}", status_code=303)


@app.post("/chapters/{chapter_id}/comment")
def update_book_chapter_comment(
    chapter_id: int,
    comment: str = Form(default=""),
    redirect_to: str = Form(default=""),
):
    book_id = update_chapter_comment(chapter_id=chapter_id, comment=comment)

    if not book_id:
        raise HTTPException(status_code=404, detail="Chapter not found")

    return RedirectResponse(url=_safe_redirect(redirect_to) or f"/books/{book_id}", status_code=303)


@app.post("/chapters/{chapter_id}/delete")
def delete_book_chapter(
    chapter_id: int,
    delete_password: str = Form(default=""),
):
    _require_delete_password(delete_password)

    book_id = delete_chapter(chapter_id)

    if not book_id:
        raise HTTPException(status_code=404, detail="Chapter not found")

    return RedirectResponse(url=f"/books/{book_id}", status_code=303)


@app.post("/books/{book_id}/memos")
def create_book_memo(
    book_id: int,
    chapter_id: int = Form(default=0),
    memo_title: str = Form(default=""),
    content: str = Form(...),
    page: int = Form(default=0),
):
    if not get_book(book_id):
        raise HTTPException(status_code=404, detail="Book not found")

    try:
        create_memo(
            book_id=book_id,
            chapter_id=chapter_id or None,
            title=memo_title,
            content=content,
            page=page,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    return RedirectResponse(url=f"/books/{book_id}", status_code=303)


@app.post("/memos/{memo_id}/delete")
def delete_book_memo(
    memo_id: int,
    delete_password: str = Form(default=""),
):
    _require_delete_password(delete_password)

    book_id = delete_memo(memo_id)

    if not book_id:
        raise HTTPException(status_code=404, detail="Memo not found")

    return RedirectResponse(url=f"/books/{book_id}", status_code=303)


@app.get("/health")
def health():
    return {
        "service": "book-memo",
        "status": "ok",
    }


@app.get("/api/search")
def search_api(q: str = "", limit: int = 5):
    limit = max(1, min(limit, 20))
    return {
        "results": search_books_and_memos(q, limit=limit),
    }


def _require_delete_password(password: str) -> None:
    configured_password = os.getenv("DELETE_PASSWORD", "").strip()

    if not configured_password:
        raise HTTPException(status_code=403, detail="삭제 비밀번호가 설정되지 않았습니다.")

    if not secrets.compare_digest(password, configured_password):
        raise HTTPException(status_code=403, detail="삭제 비밀번호가 올바르지 않습니다.")


def _safe_redirect(redirect_to: str) -> str:
    if redirect_to.startswith("/") and not redirect_to.startswith("//"):
        return redirect_to

    return ""
