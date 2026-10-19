"""Spies that prove identity-recovery paths never erase child memory layers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from services.memory_scope.child_layers import ChildLayerStore


def record_layer_erasures(
    monkeypatch: pytest.MonkeyPatch, store: ChildLayerStore
) -> list[str]:
    """Record every erasing call on the real store, then delegate it.

    A test asserts the list stays empty for paths that must not delete.  The
    delegate keeps the ordinary purge/retention tests working unchanged.
    """

    calls: list[str] = []
    original_purge = store.purge_both_layers
    original_erase = store.erase_kind
    original_delete = store.delete_record

    async def purge_both_layers(
        **kwargs: Any,
    ) -> dict[str, Mapping[str, Any]]:
        calls.append("purge_both_layers")
        return await original_purge(**kwargs)

    async def erase_kind(**kwargs: Any) -> Mapping[str, Any]:
        calls.append("erase_kind")
        return await original_erase(**kwargs)

    async def delete_record(**kwargs: Any) -> Mapping[str, Any]:
        calls.append("delete_record")
        return await original_delete(**kwargs)

    monkeypatch.setattr(store, "purge_both_layers", purge_both_layers)
    monkeypatch.setattr(store, "erase_kind", erase_kind)
    monkeypatch.setattr(store, "delete_record", delete_record)
    return calls


class FailOnSubjectDelete:
    """Stands in for the deletion saga: any call is a test failure."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def delete_subject(self, *_args: Any, **_kwargs: Any) -> None:
        self.calls.append("delete_subject")
        raise AssertionError("subject deletion must not run on an identity-recovery path")
