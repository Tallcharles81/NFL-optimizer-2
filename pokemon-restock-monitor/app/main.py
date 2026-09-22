"""Entry point.

    python -m app.main                  # dashboard + monitor
    python -m app.main --no-monitor     # dashboard only
    python -m app.main --init-db        # create tables and exit
    python -m app.main --simulation     # end-to-end simulated restock test
    python -m app.main --once           # check everything once and exit (GitHub Actions)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import dashboard, events, health, products, retailers
from app.api.deps import templates
from app.config import Settings, get_settings
from app.runtime import Runtime, build_runtime
from app.utils.logging import configure_logging, log_event

logger = logging.getLogger("main")


def create_app(settings: Settings | None = None, runtime: Runtime | None = None) -> FastAPI:
    settings = settings or (runtime.settings if runtime else get_settings())
    runtime = runtime or build_runtime(settings)
    templates.env.globals["tz_name"] = settings.timezone

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.monitor_enabled:
            await runtime.scheduler.start()
        else:
            log_event(logger, "monitor_disabled", note="dashboard only (MONITOR_ENABLED=false or --no-monitor)")
        try:
            yield
        finally:
            await runtime.aclose()

    app = FastAPI(title=settings.app_name, lifespan=lifespan, docs_url="/api/docs", redoc_url=None)
    app.state.runtime = runtime
    for module in (health, dashboard, products, retailers, events):
        app.include_router(module.router)
    return app


def _factory() -> FastAPI:
    """For ``uvicorn --factory app.main:_factory``."""
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    return create_app(settings)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pokémon restock monitor")
    parser.add_argument("--simulation", action="store_true", help="run the end-to-end simulation and exit")
    parser.add_argument("--keep-running", action="store_true",
                        help="with --simulation: keep serving the dashboard on the simulation DB afterwards")
    parser.add_argument("--no-external-notify", action="store_true",
                        help="with --simulation: only use the console channel (don't send to Discord etc.)")
    parser.add_argument("--no-monitor", action="store_true", help="serve the dashboard without polling")
    parser.add_argument("--init-db", action="store_true", help="create database tables and exit")
    parser.add_argument("--once", action="store_true", help="check every product once, alert, and exit")
    parser.add_argument("--catalog", help="with --once: CSV of products to import first (idempotent)")
    parser.add_argument("--test-run", action="store_true",
                        help="with --once: label alerts [SIMULATION] (use with a scratch DATABASE_URL)")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)

    if args.simulation:
        from app.simulation import run_simulation

        return asyncio.run(run_simulation(settings, keep_running=args.keep_running,
                                          external_notify=not args.no_external_notify,
                                          host=args.host, port=args.port))

    if args.once:
        from app.oneshot import run_once

        asyncio.run(run_once(settings, catalog=args.catalog, is_test=args.test_run))
        return 0

    if args.init_db:
        from app.database import init_database
        from app.services.product_service import ensure_retailers, sync_config_stores

        db = init_database(settings.database_url)
        with db.session() as s:
            ensure_retailers(s)
            n = sync_config_stores(s, settings)
        print(f"Database ready at {settings.database_url} ({n} configured store(s) synced)")
        return 0

    if args.no_monitor:
        settings = settings.model_copy(update={"monitor_enabled": False})

    import uvicorn

    app = create_app(settings)
    uvicorn.run(app, host=args.host or settings.host, port=args.port or settings.port,
                log_level=settings.log_level.lower(), log_config=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
