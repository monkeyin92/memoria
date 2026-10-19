"""Which child subjects a memory claim is about.

A parent's turn can produce a claim about the child (``我儿子今年八岁``): the
claim's ``subject_key`` names the child while its source evidence names the
parent, or nobody.  The catalog records every such link in
``memory_claim_subjects`` so subject deletion can collect the claim without
touching the parent's evidence or the parent's own claims.

Resolution is deliberately conservative, in this order:

1. Evidence whose ``subject_id`` is one of the account's bound children is
   that child: its speaker is the subject, and only that child is linked.
2. ``subject_key = 'self'`` is never a child; it describes the speaker.
3. Any other key must name a child by an explicit relationship code, either a
   ``son:``/``daughter:`` prefix or an archive person row whose
   ``relationship_to_owner`` is son/daughter.  Names are never matched.
4. Only then, every bound child is linked.  A parent's ``son:``/``daughter:``
   claim can concern any of the bound children, so the link covers all of
   them; one bound child still yields one link.

A subject id outside the bound-child set is treated as a non-child speaker; an
unbound minor in historical evidence therefore falls through to the same
conservative key check.

No archive row carries an identity person id for an extracted person, so a
name alone cannot be resolved reliably.  An unassociated claim survives its
child's deletion by design; a later manual or dedicated reconciliation must
handle it.  A wrong association would delete another child's memory, so the
gap is the safer outcome.

The identity-backed bound-child list is injected as ``BoundChildSubjectIds``;
the archive catalog never opens the identity store itself.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

import asyncpg

from services.archive.domain import EvidenceEvent

__all__ = [
    "BoundChildSubjectIds",
    "CHILD_KEY_PREFIXES",
    "bound_children",
    "claim_about_subject",
    "confirmed_evidence_subject",
]

logger = logging.getLogger(__name__)

#: ``account_id`` -> every child subject bound to that account.
BoundChildSubjectIds = Callable[[str], Awaitable[tuple[str, ...]]]

_CHILD_RELATIONS: tuple[str, ...] = ("son", "daughter")
#: ``subject_key`` prefixes that name a child by an explicit relationship code.
CHILD_KEY_PREFIXES: tuple[str, ...] = ("son:", "daughter:")


def confirmed_evidence_subject(event: EvidenceEvent) -> str | None:
    """The speaker the evidence actually names, or None when it names nobody.

    A blank subject stays unclaimed; a named subject folds only against its own lineage.
    """

    subject_id = event.subject_id.strip() if isinstance(event.subject_id, str) else ""
    return subject_id or None


async def bound_children(
    resolver: BoundChildSubjectIds | None,
    account_id: str,
) -> tuple[str, ...]:
    """Read the bound children once per compiled event; unavailable is empty.

    An identity outage must not fail compilation, so it degrades to "no
    children" and leaves the claim unassociated.  The warning carries only the
    exception type: no account, subject, claim or content identifier.
    """

    if resolver is None:
        return ()
    try:
        resolved = await resolver(account_id)
    except Exception as exc:
        logger.warning("claim subject lookup unavailable error=%s", type(exc).__name__)
        return ()
    return tuple(
        dict.fromkeys(
            value
            for value in resolved
            if isinstance(value, str) and value.strip() and len(value) <= 128
        )
    )


async def claim_about_subject(
    connection: asyncpg.Connection,
    *,
    account_id: str,
    evidence_subject_id: str | None,
    subject_key: str,
    bound_child_ids: tuple[str, ...],
) -> tuple[str, ...]:
    """Every bound child this claim is about; empty when unassociated."""

    if (
        evidence_subject_id is not None
        and evidence_subject_id != account_id
        and evidence_subject_id in bound_child_ids
    ):
        return (evidence_subject_id,)
    if subject_key.strip().casefold() == "self":
        return ()
    if not await _names_a_child(
        connection,
        account_id=account_id,
        subject_key=subject_key,
    ):
        return ()
    return tuple(bound_child_ids)


async def _names_a_child(
    connection: asyncpg.Connection,
    *,
    account_id: str,
    subject_key: str,
) -> bool:
    """Whether ``subject_key`` carries an explicit son/daughter relationship code."""

    normalized = subject_key.strip().casefold()
    if normalized.startswith(CHILD_KEY_PREFIXES):
        return True
    return bool(
        await connection.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM person_entities
                WHERE account_id = $1
                  AND canonical_key = $2
                  AND lower(relationship_to_owner) = ANY($3::text[])
            )
            """,
            account_id,
            subject_key,
            list(_CHILD_RELATIONS),
        )
    )
