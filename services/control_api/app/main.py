"""Control API FastAPI application."""

from __future__ import annotations

from asyncio import to_thread
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from services.control_api.app.config import ControlSettings
from services.control_api.app.database import MemoryStore
from services.control_api.app.routes import auth as auth_routes
from services.control_api.app.routes import memory as memory_routes
from services.control_api.app.routes import readiness as readiness_routes
from services.control_api.app.routes import session as session_routes


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = ControlSettings()
    try:
        settings.validate_production()
    except ValueError as exc:
        if settings.environment == "production":
            raise
        app.state.config_warning = str(exc)
    app.state.settings = settings
    store = MemoryStore(settings.memoria_db_path)
    await to_thread(store.initialize)
    app.state.memory_store = store
    yield


def create_app() -> FastAPI:
    settings = ControlSettings()
    production = settings.environment == "production"
    app = FastAPI(
        title="voice-agent-control-api",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None if production else "/docs",
        redoc_url=None if production else "/redoc",
        openapi_url=None if production else "/openapi.json",
    )
    # Eager defaults so tests without lifespan still work.
    app.state.settings = settings
    # The store initializes lazily for ASGI test clients that do not run lifespan.
    app.state.memory_store = MemoryStore(settings.memoria_db_path)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.origins_list(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(auth_routes.router)
    app.include_router(session_routes.router)
    app.include_router(memory_routes.router)
    app.include_router(readiness_routes.router)

    @app.get("/health/live")
    async def health_live() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
