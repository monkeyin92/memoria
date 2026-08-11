"""Local immutable adapter for policy receipts."""

from __future__ import annotations

from threading import Lock

from packages.contracts.generated.python.multi_subject_contracts import PolicyReceiptV2

from services.policy.receipts import PolicyReceiptConflictError


class InMemoryPolicyReceiptWriter:
    def __init__(self) -> None:
        self._lock = Lock()
        self._receipts: dict[str, PolicyReceiptV2] = {}

    def write(self, receipt: PolicyReceiptV2) -> None:
        with self._lock:
            current = self._receipts.get(receipt.receipt_id)
            if current is not None and current != receipt:
                raise PolicyReceiptConflictError("policy receipt id is immutable")
            self._receipts[receipt.receipt_id] = receipt

    def get(self, receipt_id: str) -> PolicyReceiptV2 | None:
        with self._lock:
            return self._receipts.get(receipt_id)


class InMemoryPolicyReceiptRepository:
    """Async append-only receipt repository for tests/local composition.

    ``DecisionService`` performs time-independent read-before-write replay.
    This repository only enforces immutable rows: an identical insert is a
    no-op and different content under one receipt_id fails closed with
    :class:`PolicyReceiptConflictError`.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._receipts: dict[str, PolicyReceiptV2] = {}

    async def insert(self, receipt: PolicyReceiptV2) -> None:
        with self._lock:
            current = self._receipts.get(receipt.receipt_id)
            if current is None:
                self._receipts[receipt.receipt_id] = receipt
                return
            if current != receipt:
                raise PolicyReceiptConflictError(
                    "policy receipt id is immutable and content differs"
                )

    async def get(self, receipt_id: str) -> PolicyReceiptV2 | None:
        with self._lock:
            return self._receipts.get(receipt_id)

    def __len__(self) -> int:
        with self._lock:
            return len(self._receipts)
