"""Durable RemoteDeviceCommand transport worker.

Claiming a row is not delivery.  This worker marks ``dispatched`` only after
the injected transport accepts the generated command with its stable
idempotency key.  Exceptions and timeouts deliberately leave the lease in
place so reconciliation can safely reclaim it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from packages.contracts.generated.python.multi_subject_contracts import (
    RemoteDeviceCommand,
)

from services.device_fleet.domain import (
    CommandDispatchReadiness,
    DeviceFleetContext,
    RemoteCommandDispatch,
)
from services.device_fleet.service import DeviceFleetService


class RemoteCommandDispatcherPort(Protocol):
    """Transport boundary implemented by the Agent/device command channel."""

    async def send(
        self,
        command: RemoteDeviceCommand,
        *,
        idempotency_key: str,
    ) -> None:
        """Accept one command for transport, idempotent by the supplied key."""


@dataclass(frozen=True, slots=True)
class DispatchWorkerResult:
    deliveries: tuple[RemoteCommandDispatch, ...]
    claimed: int
    dispatched: int
    failed: int


class RemoteCommandDispatchWorker:
    """Lease, send and commit remote-command transport acceptance."""

    def __init__(
        self,
        service: DeviceFleetService,
        *,
        dispatcher: RemoteCommandDispatcherPort | None,
        dispatcher_id: str,
        send_timeout: float = 10.0,
        lease_ttl: timedelta = timedelta(seconds=20),
        ack_timeout: timedelta = timedelta(seconds=30),
    ) -> None:
        if not dispatcher_id or len(dispatcher_id) > 128:
            raise ValueError("dispatcher_id must be a short non-empty string")
        if send_timeout <= 0:
            raise ValueError("send_timeout must be positive")
        if lease_ttl <= timedelta(0):
            raise ValueError("lease_ttl must be positive")
        self._service = service
        self._dispatcher = dispatcher
        self._dispatcher_id = dispatcher_id
        self._send_timeout = send_timeout
        self._lease_ttl = lease_ttl
        self._ack_timeout = ack_timeout

    async def readiness(
        self,
        context: DeviceFleetContext,
        *,
        now: datetime | None = None,
    ) -> CommandDispatchReadiness:
        current = await self._service.command_dispatch_readiness(context, now=now)
        if self._dispatcher is not None:
            return current
        return CommandDispatchReadiness(
            ready=False,
            accepted_not_dispatched=current.accepted_not_dispatched,
            last_dispatcher_heartbeat_at=None,
        )

    async def dispatch_once(
        self,
        context: DeviceFleetContext,
        *,
        now: datetime | None = None,
        limit: int = 10,
    ) -> DispatchWorkerResult:
        dispatcher = self._dispatcher
        if dispatcher is None:
            return DispatchWorkerResult((), claimed=0, dispatched=0, failed=0)
        deliveries = await self._service.claim_remote_commands(
            context,
            dispatcher_id=self._dispatcher_id,
            now=now,
            limit=limit,
            lease_ttl=self._lease_ttl,
        )
        dispatched = 0
        failed = 0
        for delivery in deliveries:
            try:
                await asyncio.wait_for(
                    dispatcher.send(
                        delivery.command,
                        idempotency_key=delivery.command.idempotency_key,
                    ),
                    timeout=self._send_timeout,
                )
                await self._service.mark_remote_command_dispatched(
                    context,
                    command_id=delivery.command.command_id,
                    lease_token=delivery.lease_token,
                    now=now,
                    ack_timeout=self._ack_timeout,
                )
            except Exception:  # transport/timeout keeps the durable lease intact
                failed += 1
            else:
                dispatched += 1
        return DispatchWorkerResult(
            deliveries=deliveries,
            claimed=len(deliveries),
            dispatched=dispatched,
            failed=failed,
        )


__all__ = [
    "DispatchWorkerResult",
    "RemoteCommandDispatcherPort",
    "RemoteCommandDispatchWorker",
]
