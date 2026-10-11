"""WebSocket браузера: снимок при подключении, далее изменения пачками."""
from __future__ import annotations

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..api.routes import SESSION_COOKIE

router = APIRouter()


@router.websocket("/ws/ui")
async def ui_ws(ws: WebSocket):
    core = ws.app.state.core
    op = core.auth.operator_for(ws.cookies.get(SESSION_COOKIE))
    if op is None:
        await ws.close(code=4401)
        return
    await ws.accept()
    factory = core.factory
    await ws.send_json({"type": "snapshot", **factory.state(op["username"])})
    factory.ui_clients.add(ws)
    try:
        while True:
            await ws.receive_text()  # браузер ничего не шлёт; чтение нужно для обнаружения закрытия
    except WebSocketDisconnect:
        pass
    finally:
        factory.ui_clients.discard(ws)
