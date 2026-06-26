from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/")
def dashboard(request: Request):
    services = [
        {
            "icon": "N",
            "name": "뉴스 허브",
            "description": "일반 뉴스와 주식 뉴스를 수집하고, 나중에 AI 요약까지 연결합니다.",
            "url": "http://news.lenserver.com",
            "status": "준비중",
            "meta": "News / Stock / Summary",
        },
        {
            "icon": "Y",
            "name": "유튜브 메모장",
            "description": "유튜브 영상별 학습 메모와 타임스탬프를 기록합니다.",
            "url": "http://memo.lenserver.com",
            "status": "준비중",
            "meta": "YouTube / Memo / Timestamp",
        },
        {
            "icon": "B",
            "name": "책 메모장",
            "description": "읽은 책을 저장하고 목차별 진행률과 독서 메모를 관리합니다.",
            "url": "http://book.lenserver.com",
            "status": "준비중",
            "meta": "Book / Reading / Memo",
        },
        {
            "icon": "T",
            "name": "자동매매 결과지",
            "description": "매매 결과, 수익률, 전략별 복기 내용을 관리합니다.",
            "url": "#",
            "status": "나중에",
            "meta": "Trading / Report / Review",
        },
    ]

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "title": "Len의 개인서버",
            "services": services,
        },
    )
