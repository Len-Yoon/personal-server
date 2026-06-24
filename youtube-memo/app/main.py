from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

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
        },
    )


@app.get("/health")
def health():
    return {
        "service": "youtube-memo",
        "status": "ok",
    }
