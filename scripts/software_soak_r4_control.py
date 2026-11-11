"""Isolated Control device route with real SQLite auth and PG authority."""

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import asyncpg
import uvicorn
from services.control_api.app.config import ControlSettings
from services.control_api.app.security import mint_memoria_access_token
from services.control_api.tests.test_device_control_api import _profile_app
from services.session_runtime.commands import StartPersistentSessionCommand
from services.session_runtime.service import build_postgres_session_runtime_service
from services.session_runtime.tests.postgres_store_test_support import (
    _SIGNING_KEY,
    _seed_delegated_binding,
    _seed_verified_device,
    postgres_runtime,
    postgres_runtime_with_consent,
)


async def main():
    output = Path(os.environ["MEMORIA_R4_OUTPUT"])
    for _ in range(600):
        if (output / "edge-url").exists():
            break
        await asyncio.sleep(0.1)
    pg = postgres_runtime.__wrapped__()
    store, dsn = await anext(pg)
    consent = postgres_runtime_with_consent.__wrapped__((store, dsn))
    await anext(consent)
    try:
        admin = await asyncpg.connect(dsn)
        try:
            await _seed_delegated_binding(
                admin,
                actor_id="person_a",
                subject_id="other",
                device_id="dev_test_01",
                binding_id="r4-binding",
                declared_mode="self_use",
                subject_category="adult",
                age_band="adult",
            )
            await _seed_verified_device(
                admin, device_id="dev_test_01", binding_id="r4-binding", now=datetime.now(UTC)
            )
        finally:
            await admin.close()
        service = build_postgres_session_runtime_service(
            store=store, signing_key=_SIGNING_KEY, profile_ttl=timedelta(hours=1)
        )
        await service.start(
            StartPersistentSessionCommand(
                session_id="session_1",
                actor_id="person_a",
                device_id="dev_test_01",
                expected_binding_version=1,
                idempotency_key="r4",
                now=datetime.now(UTC),
            )
        )
        app, _ = _profile_app(output, category="adult")
        app.dependency_overrides.clear()
        app.state.settings = ControlSettings(_env_file=None)
        app.state.session_runtime_service = service
        del app.state.device_runtime_status_reader
        app.state.settings.media_edge_internal_control_url = (output / "edge-url").read_text()
        app.state.settings.media_edge_internal_control_token = (
            "r4-local-internal-token-00000000000000000"
        )
        now = datetime.now(UTC)
        app.state.memory_store.create_auth_session(
            session_id="r4-auth",
            user_id="person_a",
            refresh_hash="r4-hash",
            expires_at=(now + timedelta(hours=1)).isoformat(),
            now=now.isoformat(),
        )
        token, _ = mint_memoria_access_token(
            app.state.settings, user_id="person_a", session_id="r4-auth"
        )
        (output / "control-ready").write_text(json.dumps({"token": token, "pid": os.getpid()}))
        await uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=int(os.environ["MEMORIA_R4_CONTROL_PORT"]),
                log_level="info",
            )
        ).serve()
    finally:
        await consent.aclose()
        await pg.aclose()


if __name__ == "__main__":
    asyncio.run(main())
