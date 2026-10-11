"""Точка входа: `python -m app.main` или `uvicorn app.main:app`."""
from __future__ import annotations

import argparse
import getpass
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse

from .api.routes import router as api_router
from .config import Profile, Settings
from .db.database import Database
from .discovery import DiscoveryResponder
from .scheduler.iperf import IperfServers
from .services.auth import AuthService
from .services.factory import Factory
from .services.reports import Reports
from .version import __version__
from .ws.agent import router as agent_ws_router
from .ws.ui import router as ui_ws_router

log = logging.getLogger("napi")


@dataclass
class Core:
    settings: Settings
    profile: Profile
    db: Database
    auth: AuthService
    factory: Factory
    reports: Reports


def build_core(settings: Settings) -> Core:
    profile = Profile.load(settings.profile_path)
    db = Database(settings.db_path)
    auth = AuthService(db, settings.session_hours)
    auth.ensure_operator(settings.admin_user, settings.admin_password)
    factory = Factory(settings, profile, db)
    factory.normalize_serials()
    factory.normalize_boot_timeouts()
    return Core(settings, profile, db, auth, factory, Reports(factory))


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.validate()
    core = build_core(settings)
    if not core.profile.hardware_validated:
        log.warning("профиль %s не помечен hardware_validated: реальные агенты не выполнят разрушающие операции",
                    core.profile.name)
    if settings.destructive_override:
        log.warning("NAPI_DESTRUCTIVE_OVERRIDE включён: разрушающие операции разрешены без валидации профиля")

    iperf = IperfServers(settings.iperf_bin, core.factory.net.ports) if settings.iperf_enable else None
    discovery = (DiscoveryResponder(settings.discovery_port, settings.port, settings.public_url)
                 if settings.discovery_port else None)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await core.factory.run_background()
        if iperf:
            await iperf.start()
        if discovery:
            await discovery.start()
        yield
        if discovery:
            await discovery.stop()
        await core.factory.stop_background()
        if iperf:
            await iperf.stop()
        core.db.close()

    app = FastAPI(title="NAPI Factory Test", version=__version__, lifespan=lifespan, docs_url="/api/docs",
                  openapi_url="/api/openapi.json", redoc_url=None)
    app.state.core = core
    app.include_router(api_router)
    app.include_router(agent_ws_router)
    app.include_router(ui_ws_router)

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "version": __version__}

    dist = Path(settings.web_dist)

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        if path.startswith(("api/", "ws/")):
            return JSONResponse({"detail": "not found"}, status_code=404)
        target = (dist / path).resolve()
        if path and target.is_file() and dist.resolve() in target.parents:
            return FileResponse(target)
        index = dist / "index.html"
        if index.is_file():
            return FileResponse(index, headers={"Cache-Control": "no-cache"})
        return JSONResponse({"detail": "веб-интерфейс не собран (web/dist)"}, status_code=503)

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="NAPI Factory Test server")
    parser.add_argument("--set-password", metavar="USER", help="задать пароль оператора и выйти")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()
    if args.set_password:
        password = getpass.getpass(f"Пароль для {args.set_password}: ")
        db = Database(settings.db_path)
        AuthService(db, settings.session_hours).set_password(args.set_password, password)
        print("готово")
        return
    import uvicorn

    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_level="info",
                ws_max_size=1 << 20)


if __name__ == "__main__":
    main()
