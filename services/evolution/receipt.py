"""Generation-scoped receipts for reviewed runtime evolution activation.

The response planner and archive writer run in the same control service, but a
voice Agent may hold a plan while a candidate is retired or a worker restarts.
The receipt binds the exact artifact list selected at plan time without storing
the query text or making archive validation depend on the current resolver
state.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from services.evolution.domain import canonical_json

RECEIPT_VERSION: Literal["evolution-resolution-v1"] = "evolution-resolution-v1"
RECEIPT_MAX_AGE_S = 3600.0
RECEIPT_FUTURE_SKEW_S = 60.0


@dataclass(frozen=True, slots=True)
class EvolutionResolutionReceipt:
    """The bounded, non-secret part carried through response provenance."""

    issued_at: datetime
    query_sha256: str
    signature: str

    def __post_init__(self) -> None:
        if self.issued_at.tzinfo is None:
            raise ValueError("evolution receipt timestamp must be timezone-aware")
        if (
            len(self.query_sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.query_sha256)
        ):
            raise ValueError("evolution receipt query digest is invalid")
        if (
            len(self.signature) != 64
            or any(character not in "0123456789abcdef" for character in self.signature)
        ):
            raise ValueError("evolution receipt signature is invalid")

    def to_dict(self) -> dict[str, str]:
        return {
            "version": RECEIPT_VERSION,
            "issued_at": self.issued_at.astimezone(UTC).isoformat(),
            "query_sha256": self.query_sha256,
            "signature": self.signature,
        }

    @classmethod
    def from_mapping(cls, value: object) -> EvolutionResolutionReceipt:
        if not isinstance(value, Mapping) or set(value) != {
            "version",
            "issued_at",
            "query_sha256",
            "signature",
        }:
            raise ValueError("evolution receipt fields are invalid")
        if value.get("version") != RECEIPT_VERSION:
            raise ValueError("evolution receipt version is invalid")
        issued_at_raw = value.get("issued_at")
        if not isinstance(issued_at_raw, str):
            raise ValueError("evolution receipt timestamp is invalid")
        try:
            issued_at = datetime.fromisoformat(issued_at_raw)
        except ValueError as exc:
            raise ValueError("evolution receipt timestamp is invalid") from exc
        query_sha256 = value.get("query_sha256")
        signature = value.get("signature")
        if not isinstance(query_sha256, str) or not isinstance(signature, str):
            raise ValueError("evolution receipt digest is invalid")
        return cls(
            issued_at=issued_at,
            query_sha256=query_sha256,
            signature=signature,
        )


def sign_resolution_receipt(
    secret: str,
    *,
    account_id: str,
    session_id: str,
    turn_id: int,
    generation_id: int,
    tool_epoch: int,
    speaker_class: str,
    query: str,
    artifacts: Sequence[Mapping[str, object]],
    issued_at: datetime | None = None,
) -> dict[str, str]:
    """Sign the exact bounded artifact references selected for one fence."""

    receipt_time = (issued_at or datetime.now(UTC)).astimezone(UTC)
    if receipt_time.tzinfo is None:
        raise ValueError("evolution receipt timestamp must be timezone-aware")
    normalized_artifacts = _artifact_refs(artifacts)
    query_sha256 = _query_digest(query)
    body = _signed_body(
        account_id=account_id,
        session_id=session_id,
        turn_id=turn_id,
        generation_id=generation_id,
        tool_epoch=tool_epoch,
        speaker_class=speaker_class,
        query_sha256=query_sha256,
        artifacts=normalized_artifacts,
        issued_at=receipt_time,
    )
    signature = _signature(secret, body)
    return EvolutionResolutionReceipt(
        issued_at=receipt_time,
        query_sha256=query_sha256,
        signature=signature,
    ).to_dict()


def verify_resolution_receipt(
    secret: str,
    receipt: object,
    *,
    account_id: str,
    session_id: str,
    turn_id: int,
    generation_id: int,
    tool_epoch: int,
    speaker_class: str,
    query: str,
    artifacts: Sequence[Mapping[str, object]],
    now: datetime | None = None,
    max_age_s: float = RECEIPT_MAX_AGE_S,
) -> bool:
    """Verify a receipt and its bounded age without revealing failure details."""

    try:
        if not secret.strip() or max_age_s < 0:
            return False
        parsed = EvolutionResolutionReceipt.from_mapping(receipt)
        current = (now or datetime.now(UTC)).astimezone(UTC)
        issued = parsed.issued_at.astimezone(UTC)
        age = (current - issued).total_seconds()
        if age < -RECEIPT_FUTURE_SKEW_S or age > max_age_s:
            return False
        normalized_artifacts = _artifact_refs(artifacts)
        if not hmac.compare_digest(parsed.query_sha256, _query_digest(query)):
            return False
        body = _signed_body(
            account_id=account_id,
            session_id=session_id,
            turn_id=turn_id,
            generation_id=generation_id,
            tool_epoch=tool_epoch,
            speaker_class=speaker_class,
            query_sha256=parsed.query_sha256,
            artifacts=normalized_artifacts,
            issued_at=issued,
        )
        expected = _signature(secret, body)
        return hmac.compare_digest(parsed.signature, expected)
    except (TypeError, ValueError):
        return False


def _query_digest(query: str) -> str:
    if not isinstance(query, str) or not query.strip():
        raise ValueError("evolution receipt query is invalid")
    return hashlib.sha256(query.strip().encode("utf-8")).hexdigest()


def _artifact_refs(artifacts: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    if len(artifacts) > 8:
        raise ValueError("evolution receipt contains too many artifacts")
    required = {"candidate_id", "version", "kind", "status", "artifact_hash"}
    normalized: list[dict[str, object]] = []
    for artifact in artifacts:
        if not isinstance(artifact, Mapping) or set(artifact) != required:
            raise ValueError("evolution receipt artifact fields are invalid")
        candidate_id = artifact.get("candidate_id")
        version = artifact.get("version")
        kind = artifact.get("kind")
        status = artifact.get("status")
        artifact_hash = artifact.get("artifact_hash")
        if (
            not isinstance(candidate_id, str)
            or not candidate_id.strip()
            or len(candidate_id) > 128
            or isinstance(version, bool)
            or not isinstance(version, int)
            or version < 1
            or kind != "prompt"
            or status not in {"canary", "stable"}
            or not isinstance(artifact_hash, str)
            or len(artifact_hash) != 64
            or any(character not in "0123456789abcdef" for character in artifact_hash)
        ):
            raise ValueError("evolution receipt artifact is invalid")
        normalized.append(
            {
                "candidate_id": candidate_id,
                "version": version,
                "kind": kind,
                "status": status,
                "artifact_hash": artifact_hash,
            }
        )
    if len({str(item["candidate_id"]) for item in normalized}) != len(normalized):
        raise ValueError("evolution receipt artifacts must be unique")
    return normalized


def _signed_body(
    *,
    account_id: str,
    session_id: str,
    turn_id: int,
    generation_id: int,
    tool_epoch: int,
    speaker_class: str,
    query_sha256: str,
    artifacts: list[dict[str, object]],
    issued_at: datetime,
) -> dict[str, object]:
    if (
        not isinstance(account_id, str)
        or not account_id.strip()
        or len(account_id) > 128
        or not isinstance(session_id, str)
        or not session_id.strip()
        or len(session_id) > 128
        or speaker_class not in {"owner", "guest", "uncertain"}
        or any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in (turn_id, generation_id, tool_epoch)
        )
    ):
        raise ValueError("evolution receipt binding is invalid")
    return {
        "version": RECEIPT_VERSION,
        "account_id": account_id.strip(),
        "session_id": session_id.strip(),
        "turn_id": turn_id,
        "generation_id": generation_id,
        "tool_epoch": tool_epoch,
        "speaker_class": speaker_class,
        "query_sha256": query_sha256,
        "artifacts": artifacts,
        "issued_at": issued_at.astimezone(UTC).isoformat(),
    }


def _signature(secret: str, body: Mapping[str, object]) -> str:
    if not isinstance(secret, str) or not secret.strip():
        raise ValueError("evolution receipt signing secret is required")
    return hmac.new(
        secret.encode("utf-8"),
        canonical_json(body).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


__all__ = [
    "EvolutionResolutionReceipt",
    "RECEIPT_MAX_AGE_S",
    "RECEIPT_VERSION",
    "sign_resolution_receipt",
    "verify_resolution_receipt",
]
