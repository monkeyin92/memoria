from types import SimpleNamespace
from typing import Any, cast

import pytest
from services.control_api.app.routes.interaction import _subject_memory_retention_allowed
from services.memory_scope.subject_session_recall import InMemorySubjectSessionRecallStore


class _Layers:
    def __init__(self, open_: bool, fail: bool = False) -> None:
        self.open_, self.fail = open_, fail

    async def memory_purpose_open(self, *, subject_id: str) -> bool:
        if self.fail:
            raise RuntimeError("db down")
        return self.open_


def _request(layers: object | None, store: object | None) -> Any:
    state = SimpleNamespace(child_layer_store=layers, subject_session_recall_store=store)
    return SimpleNamespace(app=SimpleNamespace(state=state))


async def _allowed(request: Any, category: object) -> bool:
    return await _subject_memory_retention_allowed(
        request, subject_person_id="child-1", subject_category=category
    )


@pytest.mark.asyncio
async def test_closed_purpose_denies_memory_and_erases_session_recall() -> None:
    store = InMemorySubjectSessionRecallStore()
    await store.append(
        subject_id="child-1",
        session_id="s",
        session_epoch=1,
        turn_id=1,
        text="我喜欢蓝色",
        actor_subject_id="child-1",
    )
    request = _request(_Layers(False), store)
    assert await _allowed(request, "adult") is False
    assert (
        await store.list_recent(
            subject_id="child-1",
            session_id="s",
            session_epoch=1,
            actor_subject_id="child-1",
            limit=4,
        )
        == ()
    )


@pytest.mark.asyncio
async def test_open_purpose_or_no_store_keeps_legacy_behaviour() -> None:
    assert await _allowed(_request(_Layers(True), None), "adult") is True
    assert await _allowed(_request(None, None), "adult") is True


@pytest.mark.asyncio
async def test_purpose_lookup_failure_fails_closed() -> None:
    assert await _allowed(_request(cast(Any, _Layers(True, fail=True)), None), "adult") is False
