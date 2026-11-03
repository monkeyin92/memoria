from __future__ import annotations

from typing import Any

import pytest
from services.agent.tests.unit.media_session_support import _accept_media_asr_final
from services.common.companion_response_safety import companion_safety_decision


@pytest.mark.asyncio
async def test_media_commits_do_not_replay_a_consumed_asr_tail(
    device_media_session: Any,
) -> None:
    window = await device_media_session("media-consumed-asr-tail", during_playback=False)
    registry = window.registry
    registry.turn_endpoint_grace_s = 60
    registry.turn_endpoint_min_grace_s = 60
    registry.turn_endpoint_max_grace_s = 60
    queries = ("我想伤害自己。", "我爸爸打我，好疼。")

    for turn_id, text in enumerate(queries, start=1):
        start = 40_000 + (turn_id - 1) * 3_200
        await _accept_media_asr_final(
            registry,
            window.identity,
            sentence_id=f"turn-{turn_id}",
            task_epoch=turn_id + 2,
            start_sample=start,
            end_sample=start + 1_600,
            text=text,
        )
        fence, reason = await registry.commit_user_turn(
            window.identity.session_id,
            stream_epoch=window.identity.stream_epoch,
            start_sample=start,
            end_sample=start + 800,
            retire_sample=start + 800,
        )
        assert fence is not None, reason
        assert reason is None
        assert window.context.projection.provisional is None
        assert window.context.runtime.speech_timeline.committed_sample == start + 800

    assert window.provider.prepared == list(queries)
    assert companion_safety_decision(window.provider.prepared[-1]) == "support_victim"
