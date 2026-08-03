"""Exercise the local media-v1 bridge without external providers or networks."""

from __future__ import annotations

import asyncio
import json

import grpc
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.voice_core.generated.memoria.media.v1 import media_pb2
from services.agent.src.voice_core.grpc_bridge import MediaBridgeGrpcServer
from services.agent.src.voice_core.media_bridge_server import PCMFrame
from services.agent.src.voice_core.media_protocol import SessionIdentity


async def _requests(queue: asyncio.Queue[media_pb2.MediaToCore | None]):
    while True:
        item = await queue.get()
        if item is None:
            return
        yield item


async def smoke() -> dict[str, object]:
    bridge = MediaBridgeGrpcServer()
    port = await bridge.start("127.0.0.1:0")
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
    rpc = channel.stream_stream(
        "/memoria.media.v1.VoiceMediaBridge/Connect",
        request_serializer=media_pb2.MediaToCore.SerializeToString,
        response_deserializer=media_pb2.CoreToMedia.FromString,
    )
    queue: asyncio.Queue[media_pb2.MediaToCore | None] = asyncio.Queue()
    call = rpc(_requests(queue))
    proto_identity = media_pb2.SessionIdentity(
        session_id="media-runtime-smoke",
        account_id="smoke-account",
        participant_id="smoke-participant",
        device_id="smoke-device",
        client_type="h5",
        stream_epoch=1,
    )
    identity = SessionIdentity(
        "media-runtime-smoke",
        account_id="smoke-account",
        participant_id="smoke-participant",
        device_id="smoke-device",
        client_type="h5",
    )
    try:
        await queue.put(
            media_pb2.MediaToCore(
                hello=media_pb2.SessionHello(identity=proto_identity),
            )
        )
        accepted = await call.read()
        if accepted.WhichOneof("event") != "accepted":
            raise RuntimeError("media bridge did not accept the hello")
        fence = GenerationFence("media-runtime-smoke", 1, 1, 0)
        if not await bridge.emit_generation(
            identity.session_id,
            fence,
            action=media_pb2.GENERATION_ACTION_START,
            reason="smoke",
        ):
            raise RuntimeError("media bridge rejected the current generation")
        generation = await call.read()
        if generation.generation.generation_id != 1:
            raise RuntimeError("media bridge returned the wrong generation")
        frame = PCMFrame(
            identity=identity,
            turn_id=1,
            generation_id=1,
            tool_epoch=0,
            sequence=0,
            source_start_sample=0,
            frame_samples=2,
            pcm_s16le=b"\x00\x00\x01\x00",
        )
        if not await bridge.emit_pcm(identity.session_id, frame):
            raise RuntimeError("media bridge rejected current audio")
        audio = await call.read()
        if audio.audio.generation_id != 1:
            raise RuntimeError("media bridge returned stale audio")
        stale = PCMFrame(
            identity=identity,
            turn_id=1,
            generation_id=0,
            tool_epoch=0,
            sequence=1,
            source_start_sample=2,
            frame_samples=2,
            pcm_s16le=b"\x00\x00\x01\x00",
        )
        stale_rejected = not await bridge.emit_pcm(identity.session_id, stale)
        if not stale_rejected:
            raise RuntimeError("media bridge accepted stale audio")
        return {"port": port, "accepted": True, "stale_rejected": stale_rejected}
    finally:
        await queue.put(None)
        await call.read()
        await channel.close()
        await bridge.stop()


def main() -> None:
    print(json.dumps(asyncio.run(smoke()), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
