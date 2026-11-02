"""FastAPI app entrypoint."""

from __future__ import annotations

import logging
import sys
import uuid

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pmi_core.config import settings

from pmi_api.config import api_settings
from pmi_api.notify import notify_failure
from pmi_api.routes.admin import router as admin_router
from pmi_api.routes.ask import router as ask_router
from pmi_api.routes.auth import router as auth_router
from pmi_api.routes.climate import router as climate_router
from pmi_api.routes.esports import router as esports_router
from pmi_api.routes.gdp import router as gdp_router
from pmi_api.routes.health import router as health_router
from pmi_api.routes.indexes import router as indexes_router
from pmi_api.routes.insights import router as insights_router
from pmi_api.routes.jobs import router as jobs_router
from pmi_api.routes.landing import router as landing_router
from pmi_api.routes.maga import router as maga_router
from pmi_api.routes.markets import router as markets_router
from pmi_api.routes.settings import router as settings_router
from pmi_api.routes.sports import router as sports_router
from pmi_api.routes.subscriptions import router as subscriptions_router
from pmi_api.routes.war import router as war_router
from pmi_api.routes.wc import router as wc_router
from pmi_api.routes.worlds import router as worlds_router

log = structlog.get_logger(__name__)


def _setup_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(message)s",
        stream=sys.stdout,
    )
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ]
    )


def create_app() -> FastAPI:
    _setup_logging()
    from pmi_core.observability import init_sentry

    init_sentry("pmi-api")
    app = FastAPI(
        title="pmi-api",
        version="0.1.0",
        description=(
            "REST gateway over pmi-core. Reads are served from Postgres; the "
            "§3.2 on-demand POSTs (score refresh / backtest) enqueue onto the "
            "Postgres job queue and return 202 + a poll handle."
        ),
    )
    if api_settings.cors_origins_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=api_settings.cors_origins_list,
            allow_credentials=True,
            # POST: the on-demand enqueue endpoints (score/refresh, backtest).
            # PUT: the admin prompt editor (/admin/prompts).
            allow_methods=["GET", "POST", "PUT"],
            allow_headers=["*"],
        )
    app.include_router(health_router)
    app.include_router(indexes_router)
    app.include_router(jobs_router)
    app.include_router(maga_router)
    app.include_router(gdp_router)
    app.include_router(war_router)
    app.include_router(wc_router)
    app.include_router(esports_router)
    app.include_router(sports_router)
    app.include_router(worlds_router)
    app.include_router(climate_router)
    app.include_router(insights_router)
    app.include_router(landing_router)
    app.include_router(markets_router)
    app.include_router(subscriptions_router)
    app.include_router(auth_router)
    app.include_router(ask_router)
    app.include_router(settings_router)
    app.include_router(admin_router)

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        """Catch-all 500 boundary: log, alert Slack, return an opaque body.

        One hook point for every route — same shape as `PollerBase.run_once()`
        for pollers and `worker.py::_execute` for queue jobs — rather than a
        try/except in each of the 11 routers. `request_id` echoes the caller's
        `X-Request-Id` when present so a client-reported incident can be
        grepped straight out of the Slack alert; otherwise one is minted here
        so the log line and the alert always agree.
        """
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        log.error(
            "api.unhandled_exception",
            path=request.url.path,
            method=request.method,
            request_id=request_id,
            error=str(exc),
            exc_info=True,
        )
        await notify_failure(
            route=request.url.path,
            exc=exc,
            method=request.method,
            request_id=request_id,
        )
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "INTERNAL_ERROR", "request_id": request_id}},
        )

    return app


app = create_app()


def run() -> None:
    """Console script entrypoint: `uv run pmi-api`."""
    import uvicorn

    uvicorn.run(
        "pmi_api.main:app",
        host="0.0.0.0",
        port=api_settings.port,
        reload=False,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    run()
