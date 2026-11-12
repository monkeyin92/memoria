from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from services.identity.domain import DeviceBinding


@dataclass(frozen=True, slots=True)
class SubjectRelease:
    subject_id: str
    outcome: Literal["released", "still_served", "adult_kept", "not_attested"]


@dataclass(frozen=True, slots=True)
class BindingRevocation:
    binding: DeviceBinding
    subjects: tuple[SubjectRelease, ...]
