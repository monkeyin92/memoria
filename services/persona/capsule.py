"""Persona trait identity and capsule composition shared by the persona engine.

The capsule composer renders a frozen persona version snapshot; the account-keyed
learner and the subject projection both use it so no second read path can grow
a divergent capsule algorithm.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, cast

from services.persona.domain import PersonaCapsule, PersonaCapsuleEntry, PersonaTraitCategory
from services.persona.rules import CATEGORY_ORDER, safe_confirmed_style_description


def persona_trait_key(
    account_id: str,
    subject_id: str,
    category: str,
    normalized_key: str,
) -> tuple[str, tuple[str, ...]]:
    """The stable-id inputs of one trait.

    The account holder keeps the pre-subject formula so existing trait ids do
    not move; any other subject gets its own namespace under the account.
    """

    if subject_id == account_id:
        return "persona-trait", (account_id, category, normalized_key)
    return "persona-subject-trait", (account_id, subject_id, category, normalized_key)


def _capsule_topic_tokens(topic: str) -> tuple[str, ...]:
    compact = re.sub(r"\s+", "", topic)
    tokens = {compact}
    tokens.update(compact[index : index + 2] for index in range(max(0, len(compact) - 1)))
    return tuple(token for token in tokens if token)


def _capsule_rank(item: Mapping[str, Any], topic: str) -> tuple[int, int, str]:
    description = str(item["description"])
    context = str(item["context"])
    relevant = bool(
        topic
        and any(
            token in description or token in context
            for token in _capsule_topic_tokens(topic)
        )
    )
    category = str(item["category"])
    return (0 if relevant else 1, CATEGORY_ORDER[category], str(item["trait_id"]))


def _capsule_delivery_rate(entries: Sequence[PersonaCapsuleEntry]) -> float:
    descriptions = " ".join(
        item.description for item in entries if item.category == "speech_rate"
    )
    if "偏从容" in descriptions:
        return 0.95
    if "偏快" in descriptions:
        return 1.05
    return 1.0


def persona_capsule_from_snapshot(
    snapshot: Sequence[Mapping[str, Any]],
    *,
    version_id: str,
    version_number: int,
    topic: str,
    max_chars: int,
    confirmed_style_only: bool = False,
) -> PersonaCapsule:
    """Compose one capsule from a frozen persona version snapshot.

    Both account-keyed engines and the subject projection share this composer:
    a projected subject must render exactly like the account-keyed learner, so
    a second read path cannot grow a divergent capsule algorithm.
    """

    items = [dict(item) for item in snapshot]
    if confirmed_style_only:
        style_items: list[dict[str, Any]] = []
        for item in items:
            description = safe_confirmed_style_description(
                str(item.get("description") or "")
            )
            if description is None:
                continue
            style_items.append(
                {
                    **item,
                    "description": description,
                    "context": "",
                    "counterexample": "",
                    "source_event_ids": [],
                }
            )
        items = style_items
    ranked = sorted(items, key=lambda item: _capsule_rank(item, topic))
    prefix = (
        f"[已确认表达风格 v{version_number}] "
        "仅调整表达方式，不推断或透露账户主人的身份、经历、价值观和决定。"
        if confirmed_style_only
        else f"[人格胶囊 v{version_number}] "
        "仅在自然且相关时参考，不机械复读口头禅；不得声称你就是账户主人。"
    )
    lines = [prefix]
    entries: list[PersonaCapsuleEntry] = []
    for item in ranked:
        line = f"- {item['description']}"
        if item["context"] and item["context"] != "conversation":
            line += f"（适用：{item['context']}）"
        if item["counterexample"]:
            line += f"（例外：{item['counterexample']}）"
        if len("\n".join((*lines, line))) > max_chars:
            continue
        lines.append(line)
        entries.append(
            PersonaCapsuleEntry(
                trait_id=str(item["trait_id"]),
                category=cast(PersonaTraitCategory, item["category"]),
                description=str(item["description"]),
                context=str(item["context"]),
                counterexample=str(item["counterexample"]),
                confidence=float(item["confidence"]),
                source_event_ids=tuple(str(value) for value in item["source_event_ids"]),
            )
        )
    if not entries:
        return PersonaCapsule()
    return PersonaCapsule(
        version_id=version_id,
        version_number=version_number,
        entries=tuple(entries),
        prompt_fragment="\n".join(lines),
        delivery_rate=_capsule_delivery_rate(entries),
    )
