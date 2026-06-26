from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.services.memo_service import (
    create_memo,
    create_or_get_video,
    delete_memo,
    embed_url,
    get_video,
    list_memos,
    list_videos,
)

app = FastAPI(title="Youtube Memo")

app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


@app.get("/")
def home(request: Request):
    return templates.TemplateResponse(
        "home.html",
        {
            "request": request,
            "title": "유튜브 메모장",
            "videos": list_videos(),
        },
    )


@app.post("/videos")
def create_video(url: str = Form(...)):
    try:
        video = create_or_get_video(url)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    return RedirectResponse(
        url=f"/videos/{video['id']}",
        status_code=303,
    )


@app.get("/videos/{video_id}")
def video_detail(request: Request, video_id: int):
    video = get_video(video_id)

    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    return templates.TemplateResponse(
        "video_detail.html",
        {
            "request": request,
            "title": video["title"],
            "video": video,
            "embed_url": embed_url(video["youtube_id"]),
            "memos": list_memos(video_id),
        },
    )


@app.post("/videos/{video_id}/memos")
def create_video_memo(
    video_id: int,
    memo_title: str = Form(default=""),
    content: str = Form(...),
):
    if not get_video(video_id):
        raise HTTPException(status_code=404, detail="Video not found")

    try:
        create_memo(video_id=video_id, title=memo_title, content=content)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    return RedirectResponse(
        url=f"/videos/{video_id}",
        status_code=303,
    )


@app.post("/memos/{memo_id}/delete")
def delete_video_memo(memo_id: int):
    video_id = delete_memo(memo_id)

    if not video_id:
        raise HTTPException(status_code=404, detail="Memo not found")

    return RedirectResponse(
        url=f"/videos/{video_id}",
        status_code=303,
    )


@app.get("/health")
def health():
    return {
        "service": "youtube-memo",
        "status": "ok",
    }
