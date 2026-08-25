from __future__ import annotations

from services.agent.src.orchestration.cue_audio import pcm_frame_source


async def test_pcm_frame_source_chunks_pcm_and_drops_incomplete_sample() -> None:
    pcm = b"\x01\x00\x02\x00\x03\x00\xff"

    frames = [
        frame
        async for frame in pcm_frame_source(
            pcm,
            sample_rate=1_000,
            frame_ms=2,
        )
    ]

    assert [bytes(frame.data) for frame in frames] == [
        b"\x01\x00\x02\x00",
        b"\x03\x00",
    ]
    assert [frame.samples_per_channel for frame in frames] == [2, 1]
    assert all(frame.sample_rate == 1_000 for frame in frames)
    assert all(frame.num_channels == 1 for frame in frames)

    assert [frame async for frame in pcm_frame_source(b"\xff")] == []
