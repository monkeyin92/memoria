"""Test-only session composition with real authority and production runtime gates."""

import logging
import math
import os
import struct
from pathlib import Path

from services.agent.src.media_agent_factory import ProductionMediaSessionFactory
from services.agent.src.runtime_profile import parse_runtime_profile
from services.agent.src.voice_core.media_session import MediaSessionResources
from services.agent.src.voice_core.media_session_types import MediaReplyChunk

from scripts.software_soak_r4_provider import Provider as ASRProvider


class Provider(ASRProvider):
    def __init__(self, client, owner):
        super().__init__()
        self.client = client
        self.owner = owner

    async def close(self, identity):
        await super().close(identity)
        await self.client.aclose()
        self.owner.clients.discard(self.client)

    def generate_reply(self, identity, text, fence):
        return self.chunks(0)

    def generate_output(self, identity, intent, fence, *, work_id, source_start_sample):
        return self.chunks(source_start_sample)

    async def chunks(self, start):
        count = 1200 if (Path(os.environ["MEMORIA_R5_OUTPUT"]) / "slow").exists() else 10
        pcm = b"".join(struct.pack("<h", int(6000 * math.sin(i * 0.173))) for i in range(480))
        for index in range(count):
            yield MediaReplyChunk(
                pcm_s16le=pcm,
                source_start_sample=start + index * 480,
                text="测试回复。" if index == 0 else "",
                first=index == 0,
                final=index == count - 1,
            )


class Factory:
    def __init__(self, settings):
        self.production = ProductionMediaSessionFactory(settings, None)
        self.clients = set()

    async def __call__(self, identity):
        runtime = self.production._new_runtime(
            identity.session_id, None, device_id=identity.device_id, identity=identity
        )
        try:
            client = await self.production._bind_mode_policy(runtime)
            self.clients.add(client)
            profile = runtime.mode_policy.runtime_profile
            if profile is None or runtime.fence.session_epoch != profile.profile.session_epoch:
                raise RuntimeError("signed profile was not admitted by production runtime gates")
            if identity.session_id == "session_1":
                import json

                retired = parse_runtime_profile(
                    json.loads(
                        (Path(os.environ["MEMORIA_R5_OUTPUT"]) / "retired-profile.json").read_text()
                    ),
                    verify_key="postgres-session-runtime-signing-key",
                )
                assert retired is not None
                assert runtime.apply_runtime_profile(retired) is None
                assert runtime.fence.session_epoch == profile.profile.session_epoch
                logging.getLogger(__name__).info(
                    "old issued profile rejected session=session_1 old_epoch=%s current_epoch=%s",
                    retired.profile.session_epoch,
                    runtime.fence.session_epoch,
                )
            bad = await client.fetch(session_id="missing-session")
            assert not bad.available and bad.runtime_profile is None
            runtime.set_runtime_profile_refresher(lambda: self.refresh(client, identity.session_id))
            logging.getLogger(__name__).info(
                "verified profile session=%s device=%s stream=%s profile=%s epoch=%s actor=%s binding=%s subject=%s",
                identity.session_id,
                identity.device_id,
                identity.stream_epoch,
                profile.profile.runtime_profile_id,
                runtime.fence.session_epoch,
                profile.profile.actor_id,
                profile.profile.binding_id,
                profile.profile.active_subject_id,
            )
            return MediaSessionResources(runtime, Provider(client, self))
        except BaseException:
            await runtime.close()
            raise

    @staticmethod
    async def refresh(client, session_id):
        return (await client.fetch(session_id=session_id)).runtime_profile

    async def aclose(self):
        logging.getLogger(__name__).info(
            "fixture authority clients after registry drain remaining=%s", len(self.clients)
        )
        for client in tuple(self.clients):
            await client.aclose()


def build(settings):
    return Factory(settings)
