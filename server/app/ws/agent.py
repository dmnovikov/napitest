"""WebSocket агентов: одно соединение на плату."""
from __future__ import annotations

import hmac
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..services.factory import AgentConn, FactoryError

log = logging.getLogger("napi.ws.agent")
router = APIRouter()

MAX_MESSAGE = 1 << 20


@router.websocket("/ws/agent")
async def agent_ws(ws: WebSocket):
    core = ws.app.state.core
    token = None
    auth = ws.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    token = token or ws.query_params.get("token")
    if not token or not hmac.compare_digest(token, core.settings.agent_token):
        await ws.close(code=4401)
        return
    await ws.accept()
    server_host = (ws.scope.get("server") or ("127.0.0.1", 0))[0]
    conn = AgentConn(ws=ws, server_host=server_host)
    factory = core.factory
    try:
        while True:
            raw = await ws.receive_text()
            if len(raw) > MAX_MESSAGE:
                await conn.send({"type": "error", "detail": "message too large"})
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await conn.send({"type": "error", "detail": "invalid json"})
                continue
            if not isinstance(msg, dict):
                continue
            if msg.get("type") == "hello":
                await factory.agent_hello(conn, msg)
            else:
                await factory.agent_message(conn, msg)
    except WebSocketDisconnect:
        pass
    except FactoryError as exc:
        try:
            await ws.close(code=exc.status if 4000 <= exc.status < 5000 else 4400)
        except Exception:
            pass
    except Exception:
        if not conn.closed:  # закрытое при замене соединение не ошибка
            log.exception("agent connection failed")
    finally:
        await factory.agent_disconnected(conn)
