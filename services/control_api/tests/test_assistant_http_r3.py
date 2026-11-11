"""Real Edge HTTP, Control HTTP, SQLite activation and PG session authority."""

# ruff: noqa: F811
import asyncio
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import asyncpg
import httpx
import pytest
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
    _seed_self_consent,
    _seed_verified_device,
    postgres_runtime,  # noqa: F401
    postgres_runtime_with_consent,  # noqa: F401
)
from services.session_runtime.tests.test_postgres_action_authority import _revoke_self_consent


@pytest.mark.parametrize("withdrawal", ["expired", "revoked", "unbound", "manager"])
async def test_go_http_control_http_authority(tmp_path, postgres_runtime_with_consent, withdrawal):
    store, dsn = postgres_runtime_with_consent
    ready = tmp_path / "edge-url"
    output = (tmp_path / "go.log").open("w")
    binary = tmp_path / "edge.test"
    compiled = subprocess.run(
        ["go", "test", "-c", "-race", "-o", str(binary), "."],
        cwd=Path(__file__).parents[2] / "media_edge",
        stdout=output,
        stderr=subprocess.STDOUT,
        check=False,
    )
    output.flush()
    assert compiled.returncode == 0, (tmp_path / "go.log").read_text()
    proc = subprocess.Popen(
        [str(binary), "-test.count=1", "-test.run=^TestAssistantR3HTTPFixture$"],
        cwd=Path(__file__).parents[2] / "media_edge",
        env={**os.environ, "MEMORIA_R3_EDGE_READY": str(ready)},
        stdout=output,
        stderr=subprocess.STDOUT,
    )
    try:
        for _ in range(600):
            if ready.exists() or proc.poll() is not None:
                break
            await asyncio.sleep(0.1)
        assert ready.exists(), (tmp_path / "go.log").read_text()
        url = ready.read_text()
        app, _ = _profile_app(tmp_path, category="adult")
        app.dependency_overrides.clear()
        app.state.settings = ControlSettings()
        for user_id in ("person_a", "other"):
            app.state.memory_store.create_auth_session(
                session_id=f"auth-{user_id}",
                user_id=user_id,
                refresh_hash=f"hash-{user_id}",
                expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                now=datetime.now(UTC).isoformat(),
            )
        token, _ = mint_memoria_access_token(
            app.state.settings, user_id="person_a", session_id="auth-person_a"
        )
        other_token, _ = mint_memoria_access_token(
            app.state.settings, user_id="other", session_id="auth-other"
        )
        del app.state.device_runtime_status_reader
        app.state.settings.media_edge_internal_control_url = url
        app.state.settings.media_edge_internal_control_token = (
            "r3-local-internal-token-32-characters"
        )
        service = build_postgres_session_runtime_service(
            store=store,
            signing_key=_SIGNING_KEY,
            profile_ttl=timedelta(seconds=3) if withdrawal == "expired" else timedelta(minutes=5),
        )
        app.state.session_runtime_service = service
        now = datetime.now(UTC)
        admin = await asyncpg.connect(dsn)
        try:
            await _seed_delegated_binding(
                admin,
                actor_id="person_a",
                subject_id="r3-other",
                device_id="dev_test_01",
                binding_id="r3-binding",
                declared_mode="self_use",
                subject_category="adult",
                age_band="adult",
            )
            await _seed_verified_device(
                admin, device_id="dev_test_01", binding_id="r3-binding", now=now
            )
            evidence, snapshot = await _seed_self_consent(
                admin,
                actor_id="person_a",
                device_id="dev_test_01",
                binding_id="r3-binding",
                capability="memory_recall_private",
                purpose="memory_recall",
                now=now,
            )
        finally:
            await admin.close()
        await service.start(
            StartPersistentSessionCommand(
                session_id="session_1",
                actor_id="person_a",
                device_id="dev_test_01",
                expected_binding_version=1,
                idempotency_key="r3",
                now=datetime.now(UTC),
                profile_ttl=timedelta(seconds=2) if withdrawal == "expired" else None,
            )
        )
        profile = await service.switch_subject(
            SwitchPersistentSubjectCommand(
                session_id="session_1",
                actor_id="person_a",
                subject_id="person_a",
                requested_capabilities=("chat", "memory_recall_private"),
                now=datetime.now(UTC),
            )
        )
        async with (
            httpx.AsyncClient(base_url=url) as edge,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://control"
            ) as client,
        ):
            assert (
                await edge.get("/v1/internal/device-runtime/status?device_id=dev_test_01")
            ).status_code == 401

            assert (await client.get("/v1/devices/dev_test_01/session-state")).status_code == 401
            assert (
                await client.get(
                    "/v1/devices/dev_test_01/session-state",
                    headers={"Authorization": f"Bearer {other_token}"},
                )
            ).status_code in (403, 404)
            client.headers["Authorization"] = f"Bearer {token}"

            async def read():
                response = await client.get("/v1/devices/dev_test_01/session-state")
                assert response.status_code == 200, response.text
                return response.json()

            assert (await read())["status"] == "unknown"
            event = {
                "identity": {"session_id": "session_1", "stream_epoch": 18},
                "type": "assistant_state",
                "sequence": 2,
                "generation_id": 1,
                "json_payload": __import__("base64")
                .b64encode(
                    json.dumps(
                        {
                            "payload": {"phase": "speaking", "state": "listening_closed"},
                            "session_epoch": profile.session_epoch,
                        }
                    ).encode()
                )
                .decode(),
            }
            await edge.post("/fixture/event", json=event)
            actual = await read()
            assert actual["status"] == "online", actual
            assert actual["snapshot"]["sequence"] == 2
            assert actual["snapshot"]["stream_epoch"] == 18
            assert actual["snapshot"]["fence"]["generation_id"] == 1
            for bad in (
                {"sequence": 1},
                {"sequence": 3, "generation_id": 0},
                {"identity": {"session_id": "wrong", "stream_epoch": 18}},
                {"identity": {"session_id": "session_1", "stream_epoch": 17}},
            ):
                await edge.post("/fixture/event", json={**event, **bad})
                assert (await read())["snapshot"] == actual["snapshot"]
            if withdrawal == "revoked":
                admin = await asyncpg.connect(dsn)
                try:
                    await _revoke_self_consent(
                        admin, evidence=evidence, snapshot=snapshot, now=datetime.now(UTC)
                    )
                finally:
                    await admin.close()
            elif withdrawal in ("unbound", "manager"):
                admin = await asyncpg.connect(dsn)
                try:
                    if withdrawal == "unbound":
                        await admin.execute(
                            "UPDATE identity_device_bindings SET status='revoked' WHERE binding_id='r3-binding'"
                        )
                    else:
                        await admin.execute(
                            "UPDATE identity_device_bindings SET status='superseded' WHERE binding_id='r3-binding'"
                        )
                        await admin.execute("""INSERT INTO identity_device_bindings (
                            binding_id, device_id, declared_mode, family_space_id, account_owner_person_id,
                            binding_version, status, reason, valid_from, supersedes_binding_id,
                            service_profile_version, policy_bundle_version, persona_assignment_id, created_at
                        ) SELECT 'r3-rebound', device_id, declared_mode, family_space_id, 'r3-other', 2,
                            'active', 'supersede', now(), binding_id, service_profile_version,
                            policy_bundle_version, persona_assignment_id, now()
                            FROM identity_device_bindings WHERE binding_id='r3-binding'""")
                finally:
                    await admin.close()
            else:
                await asyncio.sleep(
                    max(0, (profile.expires_at - datetime.now(UTC)).total_seconds()) + 0.02
                )
            assert (await read())["status"] == "unknown"
            await edge.post("/fixture/disconnect")
            assert (await read())["status"] == "offline"
            await edge.post("/fixture/reconnect")
            assert (await read())["status"] == "unknown"
            app.state.settings.media_edge_internal_control_token = (
                "wrong-token-but-still-32-characters"
            )
            assert (await read())["status"] == "unknown"
            await edge.post("/fixture/stop")
            assert await asyncio.to_thread(proc.wait, 10) == 0, (tmp_path / "go.log").read_text()
            assert (await read())["status"] == "unknown"
        assert proc.wait(timeout=10) == 0, (tmp_path / "go.log").read_text()
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=10)
        output.close()
        receipt_dir = os.environ.get("MEMORIA_R3_GO_REPORT_DIR")
        if receipt_dir:
            (Path(__file__).parents[3] / receipt_dir / f"go-http-{withdrawal}.log").write_text(
                (tmp_path / "go.log").read_text()
            )
