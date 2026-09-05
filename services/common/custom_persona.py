"""Account-profile encoding for a user-written companion persona.

The Mini Program stores this in ``profiles.bio``. Catalog ``companion_id`` stays
as the designed-voice fallback and is never a custom persona id.
"""

from __future__ import annotations

from dataclasses import dataclass

MARKER = "[memoria.custom_persona.v1]"
MAX_NAME = 16
MAX_TEXT = 420


@dataclass(frozen=True, slots=True)
class CustomPersona:
    active: bool
    name: str = ""
    text: str = ""


def parse_custom_persona(bio: object) -> CustomPersona:
    raw = str(bio or "")
    if not raw.startswith(MARKER):
        return CustomPersona(active=False)
    rest = raw[len(MARKER) :].removeprefix("\n")
    header, separator, body = rest.partition("\n---\n")
    if not separator:
        return CustomPersona(active=False)
    name = ""
    for line in header.splitlines():
        if line.startswith("name:"):
            name = line[len("name:") :].strip()[:MAX_NAME]
            break
    text = body.strip()[:MAX_TEXT]
    if not name or not text:
        return CustomPersona(active=False)
    return CustomPersona(active=True, name=name, text=text)
