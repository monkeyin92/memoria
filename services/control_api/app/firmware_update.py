"""Process-local OTA observations. Restart loses history; never implies success."""

from __future__ import annotations

import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TypedDict, cast

from fastapi import Request


class FirmwareUpdateState(TypedDict):
    request_id: str
    target_build: int
    state: str
    failure: str | None
    download_inferred: bool


class RunningFirmware(TypedDict):
    build: int | None
    reported_at: str | None


class AvailableFirmware(TypedDict):
    build: int
    version: str
    size: int


class FirmwareStatus(TypedDict):
    running: RunningFirmware
    available: AvailableFirmware | None
    presence: str
    update: FirmwareUpdateState | None


@dataclass(frozen=True)
class FirmwareReport:
    build: int
    boot: str
    last: str

    @classmethod
    def parse(cls, value: str) -> FirmwareReport:
        match = re.fullmatch(
            r"build=([1-9][0-9]{0,9});boot=([0-9a-f]{8});last=(none|current|declined|retry|staged)",
            value,
        )
        if not match or int(match[1]) >= 2**31:
            raise ValueError("invalid firmware report")
        return cls(int(match[1]), match[2], match[3])


@dataclass(frozen=True)
class FirmwareObservation:
    report: FirmwareReport
    at: float
    request_id: str = ""


@dataclass
class FirmwareUpdateRequest:
    request_id: str
    target_build: int
    created_at: float
    baseline: FirmwareObservation | None
    observations: list[FirmwareObservation] = field(default_factory=list)
    checking: bool = False
    downloading: bool = False


def update_state(req: FirmwareUpdateRequest, now: float) -> FirmwareUpdateState:
    state, failure = "waiting_device", None
    for obs in req.observations:
        report = obs.report
        if (
            report.build >= req.target_build
            and report.last == "current"
            and req.baseline
            and report.boot != req.baseline.report.boot
        ):
            state = "succeeded"
            break
        if (
            req.baseline
            and state == "restarting"
            and report.boot != req.baseline.report.boot
            and report.build < req.target_build
        ):
            state, failure = "failed", "rolled_back"
            break
        if report.last in {"declined", "retry"}:
            state, failure = (
                "failed",
                (
                    "device_declined"
                    if report.last == "declined"
                    else "download_interrupted"
                    if req.downloading
                    else "check_failed"
                ),
            )
            break
        if report.last == "staged":
            state = "restarting"
    if state not in {"failed", "succeeded", "restarting"}:
        state = "downloading" if req.downloading else "checking" if req.checking else state
    if state not in {"failed", "succeeded"} and now - req.created_at > 600:
        state, failure = "failed", "timeout"
    return dict(
        request_id=req.request_id,
        target_build=req.target_build,
        state=state,
        failure=failure,
        download_inferred=state == "downloading",
    )


class FirmwareTracker:
    def __init__(
        self, *, clock: Callable[[], float] = time.time, capacity: int = 4096, ttl: float = 3600
    ) -> None:
        self.clock, self.capacity, self.ttl = clock, capacity, ttl
        self.lock = threading.RLock()
        self.entries: dict[
            str, tuple[float, FirmwareObservation | None, FirmwareUpdateRequest | None]
        ] = {}

    def _entry(
        self, device: str
    ) -> tuple[float, FirmwareObservation | None, FirmwareUpdateRequest | None]:
        now = self.clock()
        for key, entry in list(self.entries.items()):
            if now - entry[0] > self.ttl:
                del self.entries[key]
        if device not in self.entries:
            if len(self.entries) >= self.capacity:
                del self.entries[min(self.entries, key=lambda key: self.entries[key][0])]
            self.entries[device] = (now, None, None)
        return self.entries[device]

    def observe(self, device: str, report: FirmwareReport, request_id: str) -> None:
        with self.lock:
            _, _, req = self._entry(device)
            obs = FirmwareObservation(report, self.clock(), request_id)
            if req and request_id == req.request_id and obs.at >= req.created_at:
                if req.baseline is None and report.build < req.target_build:
                    req.baseline = obs
                # Bound history as well as device count. Terminal evidence is retained.
                if update_state(req, self.clock())["state"] not in {"failed", "succeeded"}:
                    req.observations = (req.observations + [obs])[-64:]
            if req and (
                (self.clock() - req.created_at > 600 and report.build >= req.target_build)
                or self.clock() - req.created_at > self.ttl
                or (req.baseline is None and report.build >= req.target_build)
            ):
                req = None
            self.entries[device] = (self.clock(), obs, req)

    def event(self, device: str, kind: str) -> None:
        with self.lock:
            _, obs, req = self._entry(device)
            if req:
                if kind == "checking":
                    req.checking = True
                if kind == "downloading":
                    req.downloading = True
            self.entries[device] = (self.clock(), obs, req)

    def read(self, device: str) -> tuple[FirmwareObservation | None, FirmwareUpdateState | None]:
        with self.lock:
            _, obs, req = self._entry(device)
            return obs, update_state(req, self.clock()) if req else None

    def start(self, device: str, target: int) -> FirmwareUpdateState:
        with self.lock:
            _, obs, req = self._entry(device)
            if (
                req
                and req.target_build == target
                and update_state(req, self.clock())["state"] not in {"failed", "succeeded"}
            ):
                return update_state(req, self.clock())
            req = FirmwareUpdateRequest(secrets.token_hex(16), target, self.clock(), obs)
            self.entries[device] = (self.clock(), obs, req)
            return update_state(req, self.clock())


def tracker_for(request: Request) -> FirmwareTracker:
    # app state is initialized only on the event-loop thread.
    if not hasattr(request.app.state, "firmware_tracker"):
        request.app.state.firmware_tracker = FirmwareTracker()
    return cast(FirmwareTracker, request.app.state.firmware_tracker)


async def observe_authenticated(request: Request, device_id: str) -> None:
    """Called only after existing device authentication; report has its own signature.

    The unpredictable request id prevents a pre-request report proving success.
    Legacy devices send no report and continue authenticating unchanged.
    """
    import asyncio

    from cryptography.exceptions import InvalidSignature

    from services.device_fleet.bootstrap_domain import (
        OnboardingError,
        b64url_decode,
        public_key_from_bytes,
    )

    value = request.headers.get("X-Memoria-Firmware")
    if not value:
        return
    request_id = request.headers.get("X-Memoria-Firmware-Request", "")
    if request_id and not re.fullmatch(r"[0-9a-f]{1,64}", request_id):
        return
    try:
        report = FirmwareReport.parse(value)
        device = await asyncio.to_thread(
            request.app.state.device_onboarding_service.store.get_device, device_id
        )
        signature = b64url_decode(
            request.headers.get("X-Memoria-Firmware-Signature", ""),
            field="signature",
            exact_length=64,
        )
        public_key_from_bytes(device.public_key).verify(
            signature, report_payload(request.url.path, value, request_id)
        )
    except (ValueError, InvalidSignature, OnboardingError):
        return
    tracker_for(request).observe(device_id, report, request_id)


def report_payload(path: str, report: str, request_id: str) -> bytes:
    return f"memoria-firmware-report-v1\n{path}\n{report}\n{request_id}".encode()
