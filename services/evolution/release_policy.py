"""Runtime-release policy for learned artifacts.

The evolution store is deliberately able to retain candidates for every update
carrier. Runtime activation is much narrower: V1 may inject only reviewed,
low-risk prompt rules for explicitly configured task families. Account-private
rules still require the resolver's owner/account check. Identity, privacy,
permissions, fences, tools, and executable harnesses stay behind their existing
deterministic release paths.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from services.evolution.domain import CandidateArtifact
from services.evolution.store import EvolutionTransitionError

_DEFAULT_RUNTIME_PROMPT_FAMILIES = frozenset({"weather"})
_PROTECTED_RUNTIME_FAMILIES = frozenset(
    {
        "identity_privacy",
        "privacy",
        "permission",
        "permissions",
        "speaker_authority",
        "tool_permission",
        "voice_control",
    }
)


def parse_runtime_prompt_families(value: str | Iterable[str]) -> frozenset[str]:
    """Parse a bounded task-family allowlist from configuration.

    Empty values intentionally disable learned runtime instructions.  This is a
    safer production default than treating an unset list as a wildcard.
    """

    raw = value.split(",") if isinstance(value, str) else value
    families = frozenset(
        item.strip().casefold() for item in raw if isinstance(item, str) and item.strip()
    )
    if any(
        len(item) > 128
        or any(ord(character) < 32 or ord(character) == 127 for character in item)
        for item in families
    ):
        raise ValueError("evolution runtime prompt families must be <= 128 characters")
    protected = families & _PROTECTED_RUNTIME_FAMILIES
    if protected:
        raise ValueError(
            "deterministic identity, privacy, permission, tool and voice-control families "
            "cannot be enabled as runtime prompts"
        )
    return families


@dataclass(frozen=True, slots=True)
class EvolutionReleasePolicy:
    """The single policy seam for learned runtime prompt activation."""

    runtime_prompt_families: frozenset[str] = _DEFAULT_RUNTIME_PROMPT_FAMILIES

    def __post_init__(self) -> None:
        normalized = parse_runtime_prompt_families(self.runtime_prompt_families)
        object.__setattr__(self, "runtime_prompt_families", normalized)

    def allows_runtime_prompt(self, candidate: CandidateArtifact) -> bool:
        return (
            candidate.kind == "prompt"
            and candidate.risk == "low"
            and (
                (candidate.scope == "global_redacted" and candidate.account_id is None)
                or (candidate.scope == "owner_private" and candidate.account_id is not None)
            )
            and candidate.task_family.casefold() in self.runtime_prompt_families
        )

    def require_runtime_release(self, candidate: CandidateArtifact) -> None:
        if self.allows_runtime_prompt(candidate):
            return
        raise EvolutionTransitionError(
            "runtime evolution release requires a low-risk scope-valid prompt "
            "from the configured task-family allowlist"
        )


__all__ = [
    "EvolutionReleasePolicy",
    "parse_runtime_prompt_families",
]
