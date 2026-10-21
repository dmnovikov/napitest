"""REST API оператора `/api/v1` и приём отчёта первой загрузки."""
from __future__ import annotations

import hmac
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from ..services.catalog import CatalogError
from ..services.factory import FactoryError
from ..services.sdcard import SdError

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


class BoardIn(BaseModel):
    device_ids: list[int] = Field(default_factory=list)
    board: Literal["napi-c", "napi-p"] | None = None      # None: автоопределение


class ExpectedIn(BaseModel):
    count: int


class ImageAddIn(BaseModel):
    name: str
    url: str
    password: str = ""


class ImageEditIn(BaseModel):
    password: str = ""
    url: str | None = None
    name: str | None = None
    visible: bool | None = None
    move: int | None = None


class CustomIn(BaseModel):
    url: str
    password: str = ""


class SdRebuildIn(BaseModel):
    password: str = ""
    url: str | None = None


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


@router.post("/devices/refresh")
async def refresh_grid(request: Request, op: dict = Depends(operator)):
    return await core(request).factory.refresh_grid(op)


@router.post("/devices/bootcheck")
async def retry_boot(body: StopIn, request: Request, op: dict = Depends(operator)):
    """«Только BOOTCHECK»: заново ждать загрузку с SD-NAND у прошитых плат, где она не подтвердилась."""
    try:
        return await core(request).factory.retry_boot(op, body.device_ids)
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


@router.put("/devices/board")
def set_board(body: BoardIn, request: Request, op: dict = Depends(operator)):
    """Тип платы вручную (для одной или выделенных плат); board: null — снова автоопределение."""
    try:
        warnings = core(request).factory.set_board(op, body.device_ids, body.board)
    except FactoryError as exc:
        return handle(exc)
    return {"ok": True, "warnings": warnings}


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


def _confirm_password(request: Request, op: dict, password: str, action: str) -> None:
    """Смена образа ОС — после повторного ввода пароля входа (ролей пока нет)."""
    if not core(request).auth.check_password(op["id"], password):
        core(request).factory.audit(op["username"], "password_failed", {"action": action})
        raise HTTPException(403, "неверный пароль")


def _catalog_error(exc: CatalogError):
    return JSONResponse({"detail": str(exc), "extra": None}, status_code=exc.status)


@router.get("/images")
def images(request: Request, op: dict = Depends(operator)):
    f = core(request).factory
    return {**f.images_view(), "history": f.catalog.history()}


@router.post("/images")
def image_add(body: ImageAddIn, request: Request, op: dict = Depends(operator)):
    _confirm_password(request, op, body.password, "image_add")
    f = core(request).factory
    try:
        key = f.catalog.add(body.name, body.url, op["username"])
    except CatalogError as exc:
        return _catalog_error(exc)
    f.audit(op["username"], "image_add", {"key": key, "name": body.name, "url": body.url})
    return {"ok": True, "key": key}


@router.put("/images/{key}")
def image_edit(key: str, body: ImageEditIn, request: Request, op: dict = Depends(operator)):
    _confirm_password(request, op, body.password, "image_edit")
    f = core(request).factory
    old = f.catalog.current(key)
    try:
        if body.url is not None:
            f.catalog.set_url(key, body.url, op["username"])
        if body.name is not None or body.visible is not None or body.move:
            f.catalog.update(key, name=body.name, visible=body.visible, move=body.move)
    except CatalogError as exc:
        return _catalog_error(exc)
    detail = {k: v for k, v in body.model_dump().items() if k != "password" and v is not None}
    if body.url is not None:
        detail["old_url"] = old["url"] if old else None
    f.audit(op["username"], "image_edit", {"key": key, **detail})
    return {"ok": True}


@router.post("/images/custom")
def image_custom(body: CustomIn, request: Request, op: dict = Depends(operator)):
    _confirm_password(request, op, body.password, "image_custom")
    f = core(request).factory
    try:
        f.catalog.set_custom(body.url, op["username"])
    except CatalogError as exc:
        return _catalog_error(exc)
    f.audit(op["username"], "image_custom", {"url": body.url})
    return {"ok": True}


@router.get("/agent/images/{name}")
def agent_image(name: str, request: Request):
    """Образ для тестовой microSD (с докачкой по Range)."""
    if not agent_token_ok(request, bearer(request)):
        raise HTTPException(401, "неверный токен агента")
    path = core(request).factory.catalog.file_allowed(name)
    if path is None:
        raise HTTPException(404, "нет такого образа")
    return FileResponse(path, media_type="application/octet-stream")


@router.get("/agent/update/{name}")
def agent_update(name: str, request: Request):
    """Пакет агента для автообновления карт (selfupdate на карте)."""
    if not agent_token_ok(request, bearer(request)):
        raise HTTPException(401, "неверный токен агента")
    sd = core(request).factory.sdcard
    if name != f"{sd.agent_version}.tar.gz":
        raise HTTPException(404, "нет такой версии агента")
    return Response(sd.agent_pkg, media_type="application/gzip")


# ---------------------------------------------------------------------- тестовая microSD

@router.get("/sdcard")
def sdcard(request: Request, op: dict = Depends(operator)):
    f = core(request).factory
    return {**f.sdcard.view(), "agent": f.images_view()["agent"]}


@router.post("/sdcard")
def sdcard_rebuild(body: SdRebuildIn, request: Request, op: dict = Depends(operator)):
    _confirm_password(request, op, body.password, "sd_rebuild")
    f = core(request).factory
    try:
        f.sdcard.request(body.url, op["username"])
    except SdError as exc:
        return JSONResponse({"detail": str(exc), "extra": None}, status_code=exc.status)
    f.audit(op["username"], "sd_rebuild", {"url": body.url} if body.url else None)
    return {"ok": True}


@router.get("/public")
def public_status(request: Request):
    """Без входа: что идёт на стенде (предупреждение на странице входа) и образ microSD для инструкции."""
    f = core(request).factory
    img = f.sdcard.view().get("image")
    return {"active": f.active_batches(),
            "sd_image": {k: img.get(k) for k in ("file", "size", "version", "built_at", "sha256")} if img else None}


@router.get("/sdcard/download")
def sdcard_download(request: Request):
    """Без входа: образ нужен тестировщику по инструкции до того, как он получит пароль."""
    path = core(request).factory.sdcard.image_path()
    if path is None:
        raise HTTPException(404, "образ microSD ещё не собран")
    return FileResponse(path, media_type="application/x-xz", filename=path.name)


@router.post("/agent/boot_ok")
async def boot_ok(body: BootOkIn, request: Request):
    if not agent_token_ok(request, bearer(request)):
        raise HTTPException(401, "неверный токен агента")
    return await core(request).factory.boot_ok(body.model_dump())
