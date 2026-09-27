"""``GET /v1/devices/{id}/firmware-release`` and its image: device OTA.

① nothing published (or no release directory) answers 204;
② a published, correctly signed release is served verbatim, and only its own
   image can be downloaded, to bound and unbound devices alike;
③ requests authenticate like the activation manifest, bound to the exact
   path, so a display-profile signature never opens the firmware endpoints;
④ a release whose signature, size or image hash is wrong is never offered;
⑤ the signed bytes are exactly the ones the firmware rebuilds.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from httpx import ASGITransport, AsyncClient
from services.control_api.app.device_firmware import (
    FIRMWARE_BOARD,
    FirmwareReleaseDirectory,
    canonical_release_payload,
)
from services.control_api.tests.test_device_display_profile import (
    CERTIFICATE_ID,
    DEVICE_ID,
    _bound_device,
    _signed_headers,
)
from services.control_api.tests.test_device_onboarding_binding_integration import (
    _app,
    _online_claim,
)
from services.device_fleet.bootstrap_domain import b64url_encode

RELEASE_PATH = f"/v1/devices/{DEVICE_ID}/firmware-release"
IMAGE = b"\xe9memoria-test-image" * 64


def _image_path(build: int) -> str:
    return f"{RELEASE_PATH}/{build}/image"


def _publish(
    root: Path,
    key: Ed25519PrivateKey,
    *,
    build: int = 2,
    image: bytes = IMAGE,
    sha256: str | None = None,
) -> dict[str, Any]:
    release: dict[str, Any] = {
        "schema_version": 1,
        "board": FIRMWARE_BOARD,
        "build": build,
        "version": f"2.4.2+m{build}",
        "size": len(image),
        "sha256": sha256 or hashlib.sha256(image).hexdigest(),
    }
    release["signature"] = b64url_encode(key.sign(canonical_release_payload(release)))
    board = root / FIRMWARE_BOARD
    (board / str(build)).mkdir(parents=True, exist_ok=True)
    (board / str(build) / "app.bin").write_bytes(image)
    (board / "current.json").write_text(json.dumps(release))
    return release


def _releases(root: Path, key: Ed25519PrivateKey) -> FirmwareReleaseDirectory:
    public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return FirmwareReleaseDirectory(root, public_key=public)


def test_signed_bytes_match_the_firmware_layout() -> None:
    release = {
        "signature": "ignored",
        "version": "2.4.2+m2",
        "size": 10,
        "sha256": "ab" * 32,
        "schema_version": 1,
        "build": 2,
        "board": FIRMWARE_BOARD,
    }
    assert canonical_release_payload(release) == (
        b"memoria-firmware-release-v1\n"
        b'{"board":"memoria-esp-vocat","build":2,"schema_version":1,'
        b'"sha256":"' + b"ab" * 32 + b'","size":10,"version":"2.4.2+m2"}'
    )


@pytest.mark.asyncio
async def test_nothing_published_answers_no_content(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app = _app(monkeypatch, tmp_path, offline_mock=True)
    device_key = Ed25519PrivateKey.generate()
    _online_claim(app.state.device_onboarding_service, actor_id="someone", device_key=device_key)
    headers = _signed_headers(device_key, path=RELEASE_PATH)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        unconfigured = await client.get(RELEASE_PATH, headers=headers)
        # The default directory next to the Control database does not exist.
        assert app.state.firmware_releases.root == tmp_path / "firmware-releases"
        assert not app.state.firmware_releases.root.exists()
        app.state.firmware_releases = _releases(tmp_path / "empty", Ed25519PrivateKey.generate())
        empty = await client.get(RELEASE_PATH, headers=headers)
    assert unconfigured.status_code == empty.status_code == 204
    assert empty.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_bound_device_reads_the_release_and_downloads_its_image(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app = _app(monkeypatch, tmp_path, offline_mock=True)
    device_key = Ed25519PrivateKey.generate()
    release_key = Ed25519PrivateKey.generate()
    releases = tmp_path / "releases"
    published = _publish(releases, release_key, build=3)
    app.state.firmware_releases = _releases(releases, release_key)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await _bound_device(client, app, device_key)
        release = await client.get(
            RELEASE_PATH, headers=_signed_headers(device_key, path=RELEASE_PATH)
        )
        image = await client.get(
            _image_path(3), headers=_signed_headers(device_key, path=_image_path(3))
        )
        stale = await client.get(
            _image_path(2), headers=_signed_headers(device_key, path=_image_path(2))
        )
    assert release.status_code == 200
    assert release.json() == published
    assert image.status_code == 200
    assert image.content == IMAGE
    assert image.headers["content-type"] == "application/octet-stream"
    assert stale.status_code == 404


@pytest.mark.asyncio
async def test_unbound_device_may_update_too(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app = _app(monkeypatch, tmp_path, offline_mock=True)
    device_key = Ed25519PrivateKey.generate()
    release_key = Ed25519PrivateKey.generate()
    _online_claim(app.state.device_onboarding_service, actor_id="someone", device_key=device_key)
    _publish(tmp_path / "releases", release_key)
    app.state.firmware_releases = _releases(tmp_path / "releases", release_key)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            RELEASE_PATH, headers=_signed_headers(device_key, path=RELEASE_PATH)
        )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_requests_authenticate_on_the_exact_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app = _app(monkeypatch, tmp_path, offline_mock=True)
    device_key = Ed25519PrivateKey.generate()
    release_key = Ed25519PrivateKey.generate()
    service = app.state.device_onboarding_service
    _online_claim(service, actor_id="someone", device_key=device_key)
    service.offline_mock = False  # the mock only registers the lab device
    _publish(tmp_path / "releases", release_key)
    app.state.firmware_releases = _releases(tmp_path / "releases", release_key)
    display_path = f"/v1/devices/{DEVICE_ID}/display-profile"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        no_certificate = await client.get(RELEASE_PATH)
        unsigned = await client.get(
            RELEASE_PATH, headers={"X-Device-Certificate-ID": CERTIFICATE_ID}
        )
        cross_endpoint = await client.get(
            RELEASE_PATH, headers=_signed_headers(device_key, path=display_path)
        )
        image_with_release_signature = await client.get(
            _image_path(2), headers=_signed_headers(device_key, path=RELEASE_PATH)
        )
        foreign = await client.get(
            RELEASE_PATH,
            headers=_signed_headers(Ed25519PrivateKey.generate(), path=RELEASE_PATH),
        )
        wrong_certificate = await client.get(
            RELEASE_PATH,
            headers=_signed_headers(device_key, path=RELEASE_PATH, certificate_id="cert_other"),
        )
        good = await client.get(
            RELEASE_PATH, headers=_signed_headers(device_key, path=RELEASE_PATH)
        )
    assert no_certificate.status_code == 401
    for rejected in (unsigned, cross_endpoint, image_with_release_signature, foreign, wrong_certificate):
        assert rejected.status_code in {401, 403, 422}, rejected.text
    assert good.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["signature", "hash", "size", "image_missing"])
async def test_a_damaged_release_is_never_offered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, damage: str
) -> None:
    app = _app(monkeypatch, tmp_path, offline_mock=True)
    device_key = Ed25519PrivateKey.generate()
    release_key = Ed25519PrivateKey.generate()
    _online_claim(app.state.device_onboarding_service, actor_id="someone", device_key=device_key)
    root = tmp_path / "releases"
    board = root / FIRMWARE_BOARD
    if damage == "signature":
        _publish(root, Ed25519PrivateKey.generate())  # signed by someone else
    elif damage == "hash":
        _publish(root, release_key, sha256="00" * 32)
    elif damage == "size":
        _publish(root, release_key)
        (board / "2" / "app.bin").write_bytes(IMAGE + b"\x00")
    else:
        _publish(root, release_key)
        (board / "2" / "app.bin").unlink()
    app.state.firmware_releases = _releases(root, release_key)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        release = await client.get(
            RELEASE_PATH, headers=_signed_headers(device_key, path=RELEASE_PATH)
        )
        image = await client.get(
            _image_path(2), headers=_signed_headers(device_key, path=_image_path(2))
        )
    assert release.status_code == 204
    assert image.status_code == 404


def test_image_hash_is_checked_again_when_the_file_changes(tmp_path: Path) -> None:
    release_key = Ed25519PrivateKey.generate()
    _publish(tmp_path, release_key)
    releases = _releases(tmp_path, release_key)
    assert releases.current() is not None
    image = tmp_path / FIRMWARE_BOARD / "2" / "app.bin"
    image.write_bytes(b"\x00" * len(IMAGE))  # same size, new content and mtime
    with pytest.raises(ValueError):
        releases.current()
