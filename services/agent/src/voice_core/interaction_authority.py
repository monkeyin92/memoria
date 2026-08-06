"""Single-writer policy for continuous interaction state."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class InteractionAuthority(StrEnum):
    PYTHON_AUTHORITATIVE = "python_authoritative"
    GO_SHADOW = "go_shadow"
    GO_AUTHORITATIVE = "go_authoritative"


class InteractionRuntime(StrEnum):
    PYTHON = "python"
    GO = "go"


_PROTO_BY_MODE = {
    InteractionAuthority.PYTHON_AUTHORITATIVE: 1,
    InteractionAuthority.GO_SHADOW: 2,
    InteractionAuthority.GO_AUTHORITATIVE: 3,
}


def interaction_authority_from_proto(value: int) -> InteractionAuthority:
    """Decode the additive enum, failing closed for old or future clients."""

    return {
        1: InteractionAuthority.PYTHON_AUTHORITATIVE,
        2: InteractionAuthority.GO_SHADOW,
        3: InteractionAuthority.GO_AUTHORITATIVE,
    }.get(value, InteractionAuthority.PYTHON_AUTHORITATIVE)


def interaction_authority_to_proto(mode: InteractionAuthority) -> int:
    return _PROTO_BY_MODE[mode]


def realtime_effect_executor(mode: InteractionAuthority) -> InteractionRuntime:
    """Return the only runtime allowed to execute real-time effects."""

    if mode is InteractionAuthority.GO_AUTHORITATIVE:
        return InteractionRuntime.GO
    return InteractionRuntime.PYTHON


def can_execute_realtime_effect(
    mode: InteractionAuthority,
    *,
    producer: InteractionRuntime,
    candidate_only: bool,
) -> bool:
    """Keep every shadow decision observational, even if mislabeled upstream."""

    return not candidate_only and producer is realtime_effect_executor(mode)


@dataclass(frozen=True, slots=True)
class InteractionAuthorityState:
    """Reversible session authority transition with a mandatory shadow gate."""

    mode: InteractionAuthority = InteractionAuthority.PYTHON_AUTHORITATIVE

    def transition(
        self,
        target: InteractionAuthority,
        *,
        shadow_parity_met: bool = False,
    ) -> InteractionAuthorityState:
        if target is self.mode:
            return self
        if target is InteractionAuthority.PYTHON_AUTHORITATIVE:
            return InteractionAuthorityState(target)
        if (
            self.mode is InteractionAuthority.PYTHON_AUTHORITATIVE
            and target is InteractionAuthority.GO_SHADOW
        ):
            return InteractionAuthorityState(target)
        if (
            self.mode is InteractionAuthority.GO_AUTHORITATIVE
            and target is InteractionAuthority.GO_SHADOW
        ):
            return InteractionAuthorityState(target)
        if (
            self.mode is InteractionAuthority.GO_SHADOW
            and target is InteractionAuthority.GO_AUTHORITATIVE
            and shadow_parity_met
        ):
            return InteractionAuthorityState(target)
        raise ValueError("interaction authority transition requires shadow parity")
