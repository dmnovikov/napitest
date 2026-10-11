"""REST API оператора `/api/v1` и приём отчёта первой загрузки."""
from __future__ import annotations

import hmac
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from ..services.factory import FactoryError

SESSION_COOKIE = "napi_session"

router = APIRouter(prefix="/api/v1")


def core(request: Request):
    return request.app.state.core


def operator(request: Request) -> dict:
    op = core(request).auth.operator_for(request.cookies.get(SESSION_COOKIE))
    if op is None:
        raise HTTPException(401, "требуется вход")
    return op


def agent_token_ok(request: Request, token: str | None) -> bool:
    expected = core(request).settings.agent_token
    return bool(token) and hmac.compare_digest(token, expected)


def bearer(request: Request) -> str | None:
    value = request.headers.get("authorization", "")
    if value.lower().startswith("bearer "):
        return value[7:].strip()
    return None


def handle(exc: FactoryError):
    return JSONResponse({"detail": str(exc), "extra": exc.extra}, status_code=exc.status)


class LoginIn(BaseModel):
    username: str
    password: str


class BatchIn(BaseModel):
    mode: Literal["test_only", "test_flash", "flash_only"]
    device_ids: list[int] = Field(default_factory=list)
    confirm: bool = False
    kind: Literal["production", "golden"] = "production"
    options: dict[str, Any] = Field(default_factory=dict)


class StopIn(BaseModel):
    device_ids: list[int] = Field(default_factory=list)


class GoldenIn(BaseModel):
    is_golden: bool


class ExpectedIn(BaseModel):
    count: int


class BootOkIn(BaseModel):
    cpuid: str
    image_version: str | None = None
    uptime: float | None = None
    serial: str | None = None


@router.post("/auth/login")
def login(body: LoginIn, request: Request, response: Response):
    c = core(request)
    token = c.auth.login(body.username, body.password)
    if token is None:
        c.factory.audit(body.username, "login_failed")
        raise HTTPException(401, "неверный логин или пароль")
    c.factory.audit(body.username, "login")
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="strict",
                        max_age=int(c.settings.session_hours * 3600))
    return {"username": body.username}


@router.post("/auth/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        core(request).auth.logout(token)
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


@router.get("/auth/me")
def me(op: dict = Depends(operator)):
    return {"username": op["username"]}


@router.get("/state")
def state(request: Request, op: dict = Depends(operator)):
    return core(request).factory.state(op["username"])


@router.post("/batches")
async def start_batch(body: BatchIn, request: Request, op: dict = Depends(operator)):
    try:
        return await core(request).factory.start_batch(op, body.mode, body.device_ids, body.kind, body.options,
                                                       body.confirm)
    except FactoryError as exc:
        return handle(exc)


@router.post("/batches/{batch_id}/stop")
async def stop_batch(batch_id: int, body: StopIn, request: Request, op: dict = Depends(operator)):
    try:
        return await core(request).factory.stop(op, body.device_ids or None, batch_id)
    except FactoryError as exc:
        return handle(exc)


@router.post("/stop")
async def stop_devices(body: StopIn, request: Request, op: dict = Depends(operator)):
    try:
        return await core(request).factory.stop(op, body.device_ids or None)
    except FactoryError as exc:
        return handle(exc)


@router.get("/batches")
def batches(request: Request, op: dict = Depends(operator)):
    return core(request).reports.batches()


@router.get("/batches/{batch_id}")
def batch(batch_id: int, request: Request, op: dict = Depends(operator)):
    try:
        return core(request).reports.batch(batch_id)
    except FactoryError as exc:
        return handle(exc)


@router.get("/batches/{batch_id}/export.csv")
def export_csv(batch_id: int, request: Request, op: dict = Depends(operator)):
    try:
        data = core(request).reports.export_csv(batch_id)
    except FactoryError as exc:
        return handle(exc)
    core(request).factory.audit(op["username"], "export", {"batch_id": batch_id, "format": "csv"})
    return PlainTextResponse(data, media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="batch-{batch_id}.csv"'})


@router.get("/batches/{batch_id}/export.json")
def export_json(batch_id: int, request: Request, op: dict = Depends(operator)):
    try:
        data = core(request).reports.export_json(batch_id)
    except FactoryError as exc:
        return handle(exc)
    core(request).factory.audit(op["username"], "export", {"batch_id": batch_id, "format": "json"})
    return JSONResponse(data, headers={"Content-Disposition": f'attachment; filename="batch-{batch_id}.json"'})


@router.get("/devices/{device_id}")
def device(device_id: int, request: Request, op: dict = Depends(operator)):
    try:
        return core(request).reports.device_card(device_id)
    except FactoryError as exc:
        return handle(exc)


@router.post("/devices/{device_id}/identify")
async def identify(device_id: int, request: Request, op: dict = Depends(operator)):
    try:
        await core(request).factory.identify(op, device_id)
    except FactoryError as exc:
        return handle(exc)
    return {"ok": True}


@router.post("/devices/{device_id}/golden")
def golden(device_id: int, body: GoldenIn, request: Request, op: dict = Depends(operator)):
    try:
        core(request).factory.set_golden(op, device_id, body.is_golden)
    except FactoryError as exc:
        return handle(exc)
    return {"ok": True}


@router.put("/expected")
def set_expected(body: ExpectedIn, request: Request, op: dict = Depends(operator)):
    try:
        core(request).factory.set_expected_boards(op, body.count)
    except FactoryError as exc:
        return handle(exc)
    return {"ok": True, "expected": body.count}


@router.get("/devices/{device_id}/label", response_class=HTMLResponse)
def label(device_id: int, request: Request, op: dict = Depends(operator)):
    try:
        page = core(request).reports.label_html(device_id)
    except FactoryError as exc:
        return handle(exc)
    core(request).factory.audit(op["username"], "label", {"device_id": device_id})
    return HTMLResponse(page)


@router.get("/jobs/{job_id}")
def job(job_id: str, request: Request, op: dict = Depends(operator)):
    try:
        return core(request).reports.job(job_id)
    except FactoryError as exc:
        return handle(exc)


@router.get("/audit")
def audit(request: Request, op: dict = Depends(operator)):
    return core(request).reports.audit()


@router.post("/agent/boot_ok")
async def boot_ok(body: BootOkIn, request: Request):
    if not agent_token_ok(request, bearer(request)):
        raise HTTPException(401, "неверный токен агента")
    return await core(request).factory.boot_ok(body.model_dump())
