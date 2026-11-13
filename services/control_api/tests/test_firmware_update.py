import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from httpx import ASGITransport, AsyncClient
from services.control_api.app.device_firmware import FIRMWARE_BOARD, InvalidFirmwareRelease
from services.control_api.app.firmware_update import FirmwareReport, FirmwareTracker, report_payload
from services.control_api.tests.test_device_display_profile import (
    DEVICE_ID,
    PATH,
    _bound_device,
    _signed_headers,
)
from services.control_api.tests.test_device_firmware_release import (
    RELEASE_PATH,
    _image_path,
    _publish,
    _releases,
)
from services.control_api.tests.test_device_onboarding_binding_integration import _app
from services.device_fleet.bootstrap_domain import b64url_encode


def test_device_offer_and_corrupt_pointer(tmp_path):
    key = Ed25519PrivateKey.generate()
    _publish(tmp_path, key, build=21)
    global_pointer = (tmp_path / FIRMWARE_BOARD / "current.json").read_bytes()
    _publish(tmp_path, key, build=29)
    devices = tmp_path / FIRMWARE_BOARD / "devices"
    devices.mkdir()
    (devices / "target.json").write_bytes((tmp_path / FIRMWARE_BOARD / "current.json").read_bytes())
    (tmp_path / FIRMWARE_BOARD / "current.json").write_bytes(global_pointer)
    releases = _releases(tmp_path, key)
    assert releases.current(device_id="target").build == 29
    assert releases.current(device_id="other").build == 21
    (devices / "target.json").write_text("{broken")
    with pytest.raises(ValueError):
        releases.current(device_id="target")
    with pytest.raises(InvalidFirmwareRelease):
        releases.current(device_id="../target")


def test_state_order_retry_idempotency_stale_and_capacity():
    now = [10.0]
    tracker = FirmwareTracker(clock=lambda: now[0], capacity=2, ttl=1000)
    tracker.observe("a", FirmwareReport(28, "aaaaaaaa", "current"), "")
    first = tracker.start("a", 29)
    rid = first["request_id"]
    assert first["state"] == "waiting_device"
    assert tracker.start("a", 29)["request_id"] == rid
    tracker.observe("a", FirmwareReport(29, "bbbbbbbb", "current"), "stale")
    assert tracker.read("a")[1]["state"] == "waiting_device"
    tracker.event("a", "checking")
    assert tracker.read("a")[1]["state"] == "checking"
    tracker.event("a", "downloading")
    assert tracker.read("a")[1]["state"] == "downloading"
    tracker.observe("a", FirmwareReport(28, "aaaaaaaa", "staged"), rid)
    assert tracker.read("a")[1]["state"] == "restarting"
    tracker.observe("a", FirmwareReport(29, "aaaaaaaa", "current"), rid)
    assert tracker.read("a")[1]["state"] != "succeeded"
    tracker.observe("a", FirmwareReport(29, "bbbbbbbb", "current"), rid)
    assert tracker.read("a")[1]["state"] == "succeeded"
    tracker.start("b", 30)
    tracker.start("c", 31)
    assert len(tracker.entries) == 2


@pytest.mark.parametrize(
    "last,boot,reason",
    [
        ("retry", "aaaaaaaa", "download_interrupted"),
        ("declined", "aaaaaaaa", "device_declined"),
        ("current", "bbbbbbbb", "rolled_back"),
    ],
)
def test_failures_and_retry(last, boot, reason):
    tracker = FirmwareTracker()
    tracker.observe("a", FirmwareReport(28, "aaaaaaaa", "current"), "")
    rid = tracker.start("a", 29)["request_id"]
    tracker.event("a", "downloading")
    if reason == "rolled_back":
        tracker.observe("a", FirmwareReport(28, "aaaaaaaa", "staged"), rid)
    tracker.observe("a", FirmwareReport(28, boot, last), rid)
    assert tracker.read("a")[1]["failure"] == reason
    assert tracker.start("a", 29)["request_id"] != rid


def test_timeout_and_expiry():
    now = [0]
    tracker = FirmwareTracker(clock=lambda: now[0], ttl=1000)
    tracker.start("a", 29)
    now[0] = 601
    assert tracker.read("a")[1]["failure"] == "timeout"
    now[0] = 1001
    assert tracker.read("a") == (None, None)


def headers(key, path, build=28, boot="aaaaaaaa", last="current", rid=""):
    value = f"build={build};boot={boot};last={last}"
    return {
        **_signed_headers(key, path=path),
        "X-Memoria-Firmware": value,
        "X-Memoria-Firmware-Request": rid,
        "X-Memoria-Firmware-Signature": b64url_encode(key.sign(report_payload(path, value, rid))),
    }


@pytest.mark.asyncio
async def test_http_update_requires_fresh_signed_report(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path, offline_mock=True)
    device_key = Ed25519PrivateKey.generate()
    release_key = Ed25519PrivateKey.generate()
    root = tmp_path / "releases"
    _publish(root, release_key, build=21)
    global_pointer = (root / FIRMWARE_BOARD / "current.json").read_bytes()
    _publish(root, release_key, build=29)
    devices = root / FIRMWARE_BOARD / "devices"
    devices.mkdir()
    pointer = devices / f"{DEVICE_ID}.json"
    pointer.write_bytes((root / FIRMWARE_BOARD / "current.json").read_bytes())
    (root / FIRMWARE_BOARD / "current.json").write_bytes(global_pointer)
    app.state.firmware_releases = _releases(tmp_path / "releases", release_key)
    endpoint = f"/v1/devices/{DEVICE_ID}/firmware-update"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner = await _bound_device(client, app, device_key)
        denied = await client.get(
            endpoint, headers={"Authorization": f"Bearer {owner['access_token']}"}
        )
        assert denied.status_code == 403
        app.state.memory_store.update_subject_profile(
            user_id=owner["user_id"],
            subject_category="adult",
            birth_year_band="adult",
            age_evidence_status="verified",
            now="2026-11-12T00:00:00+00:00",
        )
        login = await client.post(
            "/v1/auth/login", json={"username": "display-owner", "password": "safe-password"}
        )
        assert login.status_code == 200, login.text
        auth = {"Authorization": f"Bearer {login.json()['access_token']}"}
        assert (await client.get(endpoint)).status_code == 401
        foreign = await client.get("/v1/devices/not_yours/firmware-update", headers=auth)
        assert foreign.status_code == 404, foreign.text
        tampered = headers(device_key, PATH)
        tampered["X-Memoria-Firmware"] = "build=999;boot=aaaaaaaa;last=current"
        await client.get(PATH, headers=tampered)
        assert (await client.get(endpoint, headers=auth)).json()["running"]["build"] is None
        await client.get(PATH, headers=headers(device_key, PATH))
        status = await client.get(endpoint, headers=auth)
        assert status.status_code == 200, status.text
        assert status.json()["running"]["build"] == 28
        changed = await client.post(endpoint, headers=auth, json={"target_build": 30})
        assert changed.status_code == 409
        started = await client.post(endpoint, headers=auth, json={"target_build": 29})
        assert started.status_code == 202, started.text
        rid = started.json()["update"]["request_id"]
        repeated = await client.post(endpoint, headers=auth, json={"target_build": 29})
        assert repeated.json()["update"]["request_id"] == rid
        poll = await client.get(PATH, headers=headers(device_key, PATH))
        assert poll.json()["firmware_check"]["request_id"] == rid
        await client.get(
            RELEASE_PATH, headers=headers(device_key, RELEASE_PATH, rid=rid, last="none")
        )
        image = await client.get(
            _image_path(29), headers=headers(device_key, _image_path(29), rid=rid, last="none")
        )
        assert image.status_code == 200
        assert (await client.get(endpoint, headers=auth)).json()["update"]["state"] == "downloading"
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": _image_path(29),
            "query_string": b"",
            "headers": [
                (k.lower().encode(), v.encode())
                for k, v in headers(device_key, _image_path(29), rid=rid, last="none").items()
            ],
            "server": ("test", 80),
            "client": ("127.0.0.1", 1234),
            "root_path": "",
        }

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def disconnect(message):
            if message["type"] == "http.response.body":
                raise BrokenPipeError("test receiver disconnected")

        with pytest.raises(BrokenPipeError):
            await app(scope, receive, disconnect)
        assert (await client.get(endpoint, headers=auth)).json()["update"]["state"] == "downloading"
        # Serving every byte does not prove a device received or installed it.
        await client.get(PATH, headers=headers(device_key, PATH, build=29, boot="bbbbbbbb"))
        assert (await client.get(endpoint, headers=auth)).json()["update"]["state"] != "succeeded"
        await client.get(
            PATH, headers=headers(device_key, PATH, build=29, boot="bbbbbbbb", rid=rid)
        )
        assert (await client.get(endpoint, headers=auth)).json()["update"]["state"] == "succeeded"

        pointer.write_text("{broken")
        assert (await client.get(endpoint, headers=auth)).status_code == 503
        assert (
            await client.get(_image_path(29), headers=headers(device_key, _image_path(29)))
        ).status_code == 503
        pointer.unlink()
        assert (
            await client.get(_image_path(29), headers=headers(device_key, _image_path(29)))
        ).status_code == 404
        assert (root / FIRMWARE_BOARD / "current.json").read_bytes() == global_pointer

        no_release = await client.post(endpoint, headers=auth, json={"target_build": 21})
        assert no_release.status_code == 409  # running 29 is newer than global 21
        await app.state.account_operations.block_account(owner["user_id"])
        blocked = await client.post(endpoint, headers=auth, json={"target_build": 21})
        assert blocked.status_code == 409
        assert blocked.json()["detail"] == "account deletion is in progress"


@pytest.mark.asyncio
async def test_minor_can_read_but_cannot_request_update(tmp_path):
    from services.control_api.tests.test_device_control_api import _profile_app

    app, _ = _profile_app(tmp_path, category="minor")
    app.state.firmware_releases = _releases(tmp_path / "empty", Ed25519PrivateKey.generate())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        endpoint = "/v1/devices/dev_test_01/firmware-update"
        read = await client.get(endpoint)
        assert read.status_code == 200, read.text
        assert read.json()["available"] is None
        denied = await client.post(endpoint, json={"target_build": 29})
        assert denied.status_code == 403, denied.text
        assert denied.json()["detail"]["code"] == "minor_forbidden"


def test_newer_confirmed_build_and_terminal_expiry():
    now = [10.0]
    tracker = FirmwareTracker(clock=lambda: now[0], ttl=1000)
    tracker.observe("a", FirmwareReport(29, "aaaaaaaa", "current"), "")
    rid = tracker.start("a", 30)["request_id"]
    tracker.observe("a", FirmwareReport(31, "bbbbbbbb", "current"), rid)
    assert tracker.read("a")[1]["state"] == "succeeded"
    now[0] = 1100
    tracker.observe("a", FirmwareReport(31, "bbbbbbbb", "current"), rid)
    assert tracker.read("a")[1] is None


def test_legacy_bridge_reports_running_without_claiming_request_success():
    tracker = FirmwareTracker()
    rid = tracker.start("a", 29)["request_id"]
    tracker.observe("a", FirmwareReport(29, "bbbbbbbb", "current"), rid)
    observation, update = tracker.read("a")
    assert observation.report.build == 29
    assert update is None


def test_release_failure_is_not_download_failure():
    tracker = FirmwareTracker()
    tracker.observe("a", FirmwareReport(29, "aaaaaaaa", "current"), "")
    rid = tracker.start("a", 30)["request_id"]
    tracker.event("a", "checking")
    tracker.observe("a", FirmwareReport(29, "aaaaaaaa", "retry"), rid)
    assert tracker.read("a")[1]["failure"] == "check_failed"
