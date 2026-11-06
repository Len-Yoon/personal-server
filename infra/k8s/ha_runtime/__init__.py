"""Shared optional runtime for the actual seven applications' local PG mode."""
from .config import DatabaseUnavailable, enabled, node_id
from .db import connect, read_state, require_schema, require_worker, state_transaction, worker_scope, worker_owned, write_state


def register_fastapi(app):
    from fastapi.responses import JSONResponse

    async def unavailable(request, error):
        return JSONResponse(status_code=503, content={"detail": "공유 저장소 또는 복제 보호를 확인할 수 없습니다. 같은 요청으로 다시 시도해주세요."}, headers={"Retry-After": "2"})

    app.add_exception_handler(DatabaseUnavailable, unavailable)
