#!/usr/bin/env python3
"""Assert response-plan smoke payloads against fixed-safety source of truth."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Final

from services.common.companion_response_safety import (
    CRISIS_SUPPORT_REPLY,
    CRISIS_SUPPORT_REPLY_TEMPLATE_ID,
    SAFE_UNKNOWN_REPLY,
    SUPPORT_RELAY_REPLY,
    SUPPORT_RELAY_REPLY_TEMPLATE_ID,
    SUPPORT_VICTIM_REPLY,
    SUPPORT_VICTIM_REPLY_TEMPLATE_ID,
)
from services.common.crisis_policy import route_crisis

# Utterances match scripts/smoke_server_deployment.sh response-plan probes.
SMOKE_SAFETY_CASES: Final = (
    (
        "crisis",
        "我已经服药过量",
        CRISIS_SUPPORT_REPLY,
        CRISIS_SUPPORT_REPLY_TEMPLATE_ID,
        True,
    ),
    (
        "third_party",
        "朋友跟我说我想自杀，我该怎么帮助他？",
        SUPPORT_RELAY_REPLY,
        SUPPORT_RELAY_REPLY_TEMPLATE_ID,
        False,
    ),
    (
        "victim",
        "我刚被性侵了，怎么办？",
        SUPPORT_VICTIM_REPLY,
        SUPPORT_VICTIM_REPLY_TEMPLATE_ID,
        True,
    ),
    (
        "prevention",
        "如何预防暴力？",
        SUPPORT_RELAY_REPLY,
        SUPPORT_RELAY_REPLY_TEMPLATE_ID,
        False,
    ),
)

OLD_DRAFT_MARKERS: Final = (
    "急救或报警",
    "你现在是否正准备伤害自己？",
    "crisis-transfer-draft",
)


def expected_routes() -> dict[str, object]:
    """Map each smoke utterance through route_crisis (template ids live here)."""

    out: dict[str, object] = {}
    for name, query, reply, template_id, attempt_push in SMOKE_SAFETY_CASES:
        route = route_crisis(query)
        assert route.direct_text == reply
        assert route.reply_template_id == template_id
        assert route.script_version == template_id
        assert route.attempt_push is attempt_push
        assert route.notify_guardian is True
        out[name] = {
            "query": query,
            "direct_text": reply,
            "reply_template_id": template_id,
            "attempt_push": attempt_push,
        }
    return out


def assert_response_plan_safety_payloads(
    *,
    crisis: dict[str, object],
    third_party: dict[str, object],
    victim: dict[str, object],
    prevention: dict[str, object],
    prohibited: dict[str, object],
) -> None:
    """Assert HTTP response-plan JSON against imported fixed-safety copy."""

    expected = expected_routes()
    for name, payload in (
        ("crisis", crisis),
        ("third_party", third_party),
        ("victim", victim),
        ("prevention", prevention),
    ):
        want = expected[name]
        assert isinstance(want, dict)
        got = payload.get("direct_text")
        assert got == want["direct_text"], f"{name}: unexpected direct_text"
        text = got if isinstance(got, str) else ""
        for marker in OLD_DRAFT_MARKERS:
            assert marker not in text, f"{name}: stale draft marker {marker!r}"
    assert prohibited.get("direct_text") == SAFE_UNKNOWN_REPLY


def main(argv: list[str]) -> int:
    if len(argv) != 6:
        print(
            "usage: assert_response_plan_safety.py "
            "<crisis.json> <third_party.json> <victim.json> "
            "<prevention.json> <prohibited.json>",
            file=sys.stderr,
        )
        return 2
    paths = [Path(p) for p in argv[1:]]
    crisis, third_party, victim, prevention, prohibited = (
        json.loads(path.read_text(encoding="utf-8")) for path in paths
    )
    assert_response_plan_safety_payloads(
        crisis=crisis,
        third_party=third_party,
        victim=victim,
        prevention=prevention,
        prohibited=prohibited,
    )
    print("response plan safety contract: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
