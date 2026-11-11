"""Local test HTTP composition backed by actual PG session authority."""

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import asyncpg
import uvicorn
from fastapi import HTTPException, Request
from services.control_api.app.config import ControlSettings
from services.control_api.app.security import mint_memoria_access_token
from services.control_api.tests.test_device_control_api import _profile_app
from services.session_runtime.commands import (
    StartPersistentSessionCommand,
    SwitchPersistentSubjectCommand,
)
from services.session_runtime.service import build_postgres_session_runtime_service
from services.session_runtime.tests.postgres_store_test_support import (
    _SIGNING_KEY,
    _seed_delegated_binding,
    _seed_verified_device,
    postgres_runtime,
    postgres_runtime_with_consent,
)

TOKEN = "r5-local-policy-authority-token-32bytes"


async def main():
    output = Path(os.environ["MEMORIA_R5_OUTPUT"])
    while not (output / "edge-url").exists():
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
                binding_id="r5-binding",
                declared_mode="self_use",
                subject_category="adult",
                age_band="adult",
            )
            await _seed_verified_device(
                admin, device_id="dev_test_01", binding_id="r5-binding", now=datetime.now(UTC)
            )
        finally:
            await admin.close()
        service = build_postgres_session_runtime_service(
            store=store, signing_key=_SIGNING_KEY, profile_ttl=timedelta(hours=1)
        )

        async def start(session_id):
            return await service.start(
                StartPersistentSessionCommand(
                    session_id=session_id,
                    actor_id="person_a",
                    device_id="dev_test_01",
                    expected_binding_version=1,
                    idempotency_key=session_id,
                    now=datetime.now(UTC),
                )
            )

        issued = await start("session_1")
        retired = issued.model_dump(mode="json")
        issued = await service.switch_subject(
            SwitchPersistentSubjectCommand(
                session_id="session_1",
                actor_id="person_a",
                subject_id=None,
                now=datetime.now(UTC),
                requested_capabilities=("chat",),
            )
        )
        (output / "retired-profile.json").write_text(json.dumps(retired))
        app, _ = _profile_app(output, category="adult")
        app.dependency_overrides.clear()

        async def require_token(request):
            if request.headers.get("X-Memoria-Internal-Token") != TOKEN:
                raise HTTPException(401)

        @app.post("/fixture/start")
        async def start_session(request: Request):
            await require_token(request)
            body = await request.json()
            profile = await start(body["session_id"])
            return profile.model_dump(mode="json")

        @app.post("/fixture/session-policy")
        async def session_policy(request: Request):
            await require_token(request)
            body = await request.json()
            try:
                profile, _ = await service.current(
                    actor_id="person_a", session_id=body["session_id"], now=datetime.now(UTC)
                )
            except Exception as exc:
                print(
                    json.dumps(
                        {
                            "event": "fixture_policy_response",
                            "session_id": body["session_id"],
                            "status": 403,
                            "reason": type(exc).__name__,
                        }
                    ),
                    flush=True,
                )
                raise HTTPException(403, detail=type(exc).__name__) from exc
            print(
                json.dumps(
                    {
                        "event": "fixture_policy_response",
                        "session_id": body["session_id"],
                        "status": 200,
                    }
                ),
                flush=True,
            )
            return {
                "runtime_profile": profile.model_dump(mode="json"),
                "runtime_profile_version": 1,
            }

        app.state.settings = ControlSettings(_env_file=None)
        app.state.session_runtime_service = service
        del app.state.device_runtime_status_reader
        app.state.settings.media_edge_internal_control_url = (output / "edge-url").read_text()
        app.state.settings.media_edge_internal_control_token = "r5-local-edge-control-token-32bytes"
        now = datetime.now(UTC)
        app.state.memory_store.create_auth_session(
            session_id="r5-auth",
            user_id="person_a",
            refresh_hash="r5-hash",
            expires_at=(now + timedelta(hours=1)).isoformat(),
            now=now.isoformat(),
        )
        token, _ = mint_memoria_access_token(
            app.state.settings, user_id="person_a", session_id="r5-auth"
        )
        (output / "control-ready").write_text(
            json.dumps(
                {
                    "token": token,
                    "pid": os.getpid(),
                    "profile": issued.model_dump(mode="json"),
                }
            )
        )
        await uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=int(os.environ["MEMORIA_R5_CONTROL_PORT"]),
                log_level="info",
            )
        ).serve()
    finally:
        await consent.aclose()
        await pg.aclose()


if __name__ == "__main__":
    asyncio.run(main())
