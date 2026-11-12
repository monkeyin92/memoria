from __future__ import annotations

from typing import TYPE_CHECKING

from services.identity.relationships import REDACTED_DISPLAY_NAME

if TYPE_CHECKING:
    from services.identity.repository import IdentityStore


class IdentityAccountLifecycleMixin:
    if TYPE_CHECKING:
        _store: IdentityStore

    async def prepare_account_subject_erasure(self, account_id: str) -> tuple[str, ...]:
        return await self._store.prepare_account_subject_erasure(account_id)

    async def complete_subject_release(self, account_id: str, subject_id: str) -> None:
        await self._store.complete_subject_release(account_id, subject_id)

    async def delete_account_identity(
        self, *, account_person_id: str
    ) -> dict[str, int]:
        """Erase the account's identity rows (issue #283).

        The account-deletion pipeline is the only caller. The SQL port
        re-checks the actor context and the transaction-local gate, so this
        method carries the account itself as the actor.
        """
        return await self._store.delete_account_identity(
            account_person_id=account_person_id,
            display_name=REDACTED_DISPLAY_NAME,
            actor_person_id=account_person_id,
        )

    async def remaining_account_identity(
        self, *, account_person_id: str
    ) -> dict[str, int]:
        return await self._store.remaining_account_identity(
            account_person_id=account_person_id
        )

    # ------------------------------------------------------------------
    # Bindings
    # ------------------------------------------------------------------

