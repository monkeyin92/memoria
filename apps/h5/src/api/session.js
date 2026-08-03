import { request } from "./client.js";
import { isJsonObject, selfPreviewPerspectives } from "./shared.js";

function requireCompanionInteraction(session) {
  const interaction = session?.interaction;
  const voiceProfileId = interaction?.voice_profile_id ?? null;
  const voiceProfileVersion = interaction?.voice_profile_version ?? null;
  const voiceProvider = interaction?.voice_provider ?? null;
  const voiceModel = interaction?.voice_model ?? null;
  const voiceResourceId = interaction?.voice_resource_id ?? null;
  const voiceProviderExpiresAt = interaction?.voice_provider_expires_at ?? null;
  const voiceSpeakerSha256 = interaction?.voice_speaker_sha256 ?? null;
  if (
    interaction?.interaction_mode !== "companion" ||
    typeof interaction.mode_policy_version !== "string" ||
    !interaction.mode_policy_version ||
    typeof interaction.companion_style_id !== "string" ||
    !interaction.companion_style_id ||
    typeof interaction.companion_style_version !== "string" ||
    !interaction.companion_style_version ||
    interaction.digital_self_version_id !== null ||
    (interaction.manifest_sha256 ?? null) !== null ||
    (interaction.preview_grant_id ?? null) !== null ||
    (interaction.perspective ?? null) !== null ||
    interaction.relationship_profile_id !== null ||
    interaction.legacy_grant_id !== null ||
    (interaction.actor_account_id ?? null) !== null ||
    (interaction.resource_owner_account_id ?? null) !== null ||
    (interaction.relationship_profile_version ?? null) !== null ||
    (interaction.legacy_actor_role ?? null) !== null ||
    (interaction.legacy_grantee_account_id ?? null) !== null ||
    (interaction.legacy_shell_id ?? null) !== null ||
    (interaction.legacy_grant_snapshot_sha256 ?? null) !== null ||
    (interaction.legacy_scope_sha256 ?? null) !== null ||
    (interaction.legacy_voice_allowed ?? null) !== null ||
    (interaction.legacy_expires_at ?? null) !== null ||
    voiceProfileId !== null ||
    voiceProfileVersion !== null ||
    voiceProvider !== null ||
    voiceModel !== null ||
    voiceResourceId !== null ||
    voiceProviderExpiresAt !== null ||
    voiceSpeakerSha256 !== null ||
    (interaction.fallback_voice_profile_id ?? null) !== null ||
    (interaction.fallback_voice_provider ?? null) !== null ||
    (interaction.fallback_voice_model ?? null) !== null ||
    (interaction.fallback_voice_resource_id ?? null) !== null
  ) {
    throw new Error("服务端没有返回可验证的陪伴模式，会话已停止");
  }
  return session;
}

function requireMediaRuntime(session) {
  const runtime = session?.media_runtime ?? "livekit";
  if (!["livekit", "streamcore"].includes(runtime)) {
    throw new Error("服务端返回了未知的媒体运行时，会话已停止");
  }
  if (runtime === "livekit") return session;
  const streamcore = session?.streamcore;
  if (
    session?.fallback_runtime !== "livekit" ||
    !streamcore ||
    typeof streamcore.whip_url !== "string" ||
    !(
      /^https:\/\//.test(streamcore.whip_url) ||
      /^http:\/\/(localhost|127\.0\.0\.1)(:\d+)?\//.test(streamcore.whip_url)
    ) ||
    typeof streamcore.token !== "string" ||
    !streamcore.token ||
    !Number.isInteger(streamcore.stream_epoch) ||
    streamcore.stream_epoch < 1 ||
    !Number.isInteger(session.stream_epoch) ||
    session.stream_epoch !== streamcore.stream_epoch ||
    typeof streamcore.expires_at !== "string" ||
    Number.isNaN(new Date(streamcore.expires_at).getTime())
  ) {
    throw new Error("服务端没有返回可验证的 Media Runtime 会话");
  }
  return session;
}

function requireSelfPreviewInteraction(session) {
  const interaction = session?.interaction;
  const capabilities = interaction?.capabilities;
  const voiceProfileId = interaction?.voice_profile_id ?? null;
  const voiceProfileVersion = interaction?.voice_profile_version ?? null;
  const voiceProvider = interaction?.voice_provider ?? null;
  const voiceModel = interaction?.voice_model ?? null;
  const voiceResourceId = interaction?.voice_resource_id ?? null;
  const voiceProviderExpiresAt = interaction?.voice_provider_expires_at ?? null;
  const voiceSpeakerSha256 = interaction?.voice_speaker_sha256 ?? null;
  if (
    session?.voice_backend !== "cascade" ||
    interaction?.interaction_mode !== "self_preview" ||
    typeof interaction.mode_policy_version !== "string" ||
    !interaction.mode_policy_version ||
    typeof interaction.digital_self_version_id !== "string" ||
    !interaction.digital_self_version_id ||
    typeof interaction.manifest_sha256 !== "string" ||
    !/^[a-f0-9]{64}$/i.test(interaction.manifest_sha256) ||
    typeof interaction.preview_grant_id !== "string" ||
    !interaction.preview_grant_id ||
    !selfPreviewPerspectives.has(interaction.perspective) ||
    interaction.simulated_output !== true ||
    interaction.history_eligible !== false ||
    interaction.owner_projection_eligible !== false ||
    interaction.companion_style_id !== null ||
    interaction.companion_style_version !== null ||
    interaction.relationship_profile_id !== null ||
    interaction.legacy_grant_id !== null ||
    (interaction.actor_account_id ?? null) !== null ||
    (interaction.resource_owner_account_id ?? null) !== null ||
    (interaction.relationship_profile_version ?? null) !== null ||
    (interaction.legacy_actor_role ?? null) !== null ||
    (interaction.legacy_grantee_account_id ?? null) !== null ||
    (interaction.legacy_shell_id ?? null) !== null ||
    (interaction.legacy_grant_snapshot_sha256 ?? null) !== null ||
    (interaction.legacy_scope_sha256 ?? null) !== null ||
    (interaction.legacy_voice_allowed ?? null) !== null ||
    (interaction.legacy_expires_at ?? null) !== null ||
    !(
      (
        voiceProfileId === null &&
        voiceProfileVersion === null &&
        voiceProvider === null &&
        voiceModel === null &&
        voiceResourceId === null &&
        voiceProviderExpiresAt === null &&
        voiceSpeakerSha256 === null
      ) ||
      (
        typeof voiceProfileId === "string" &&
        voiceProfileId.trim() &&
        Number.isInteger(voiceProfileVersion) &&
        voiceProfileVersion >= 1 &&
        voiceProvider === "volcengine_doubao" &&
        voiceModel === "seed-icl-2.0" &&
        voiceResourceId === "seed-icl-2.0" &&
        typeof voiceProviderExpiresAt === "string" &&
        !Number.isNaN(new Date(voiceProviderExpiresAt).getTime()) &&
        /(Z|[+-]00:00)$/.test(voiceProviderExpiresAt) &&
        typeof voiceSpeakerSha256 === "string" &&
        /^[a-f0-9]{64}$/.test(voiceSpeakerSha256)
      )
    ) ||
    typeof interaction.fallback_voice_profile_id !== "string" ||
    !interaction.fallback_voice_profile_id.trim() ||
    interaction.fallback_voice_provider !== "volcengine_doubao" ||
    interaction.fallback_voice_model !== "seed-tts-2.0" ||
    interaction.fallback_voice_resource_id !== "seed-tts-2.0" ||
    !isJsonObject(capabilities) ||
    capabilities.conversation !== true ||
    capabilities.private_memory !== false ||
    capabilities.persona !== false ||
    capabilities.persona_low_sensitivity !== false ||
    capabilities.tools !== false ||
    capabilities.history !== false ||
    capabilities.learning !== false ||
    capabilities.voice_profile !== true
  ) {
    throw new Error("服务端没有返回可验证的数字分身预览模式，会话已停止");
  }
  return session;
}

function requireLegacyInteraction(session, expectedUserId, expectedGrantId) {
  const interaction = session?.interaction;
  const capabilities = interaction?.capabilities;
  const actorRole = interaction?.legacy_actor_role;
  const actorId = interaction?.actor_account_id;
  const ownerId = interaction?.resource_owner_account_id;
  const granteeId = interaction?.legacy_grantee_account_id;
  const shellId = interaction?.legacy_shell_id;
  const personalVoice = [
    interaction?.voice_profile_id,
    interaction?.voice_profile_version,
    interaction?.voice_provider,
    interaction?.voice_model,
    interaction?.voice_resource_id,
    interaction?.voice_provider_expires_at,
    interaction?.voice_speaker_sha256,
  ];
  const personalVoiceAbsent = personalVoice.every((value) => value === null);
  const personalVoiceComplete =
    typeof personalVoice[0] === "string" &&
    Boolean(personalVoice[0].trim()) &&
    Number.isInteger(personalVoice[1]) &&
    personalVoice[1] >= 1 &&
    personalVoice[2] === "volcengine_doubao" &&
    personalVoice[3] === "seed-icl-2.0" &&
    personalVoice[4] === "seed-icl-2.0" &&
    typeof personalVoice[5] === "string" &&
    !Number.isNaN(new Date(personalVoice[5]).getTime()) &&
    /(Z|[+-]00:00)$/.test(personalVoice[5]) &&
    typeof personalVoice[6] === "string" &&
    /^[a-f0-9]{64}$/.test(personalVoice[6]);
  const ownerPreview =
    actorRole === "owner_preview" &&
    actorId === ownerId &&
    actorId !== granteeId &&
    shellId === null;
  const granteeSession =
    actorRole === "grantee" &&
    actorId === granteeId &&
    actorId !== ownerId &&
    typeof shellId === "string" &&
    Boolean(shellId.trim());
  const voiceAllowed = interaction?.legacy_voice_allowed;
  const expectedVoiceCapability = voiceAllowed === true && personalVoiceComplete;
  if (
    session?.voice_backend !== "cascade" ||
    interaction?.interaction_mode !== "legacy" ||
    interaction.mode_policy_version !== "s9-v1" ||
    interaction.policy_scope !== "session" ||
    actorId !== expectedUserId ||
    typeof ownerId !== "string" ||
    !ownerId.trim() ||
    typeof granteeId !== "string" ||
    !granteeId.trim() ||
    typeof interaction.digital_self_version_id !== "string" ||
    !interaction.digital_self_version_id.trim() ||
    typeof interaction.manifest_sha256 !== "string" ||
    !/^[a-f0-9]{64}$/.test(interaction.manifest_sha256) ||
    interaction.preview_grant_id !== null ||
    interaction.perspective !== null ||
    typeof interaction.relationship_profile_id !== "string" ||
    !interaction.relationship_profile_id.trim() ||
    !Number.isInteger(interaction.relationship_profile_version) ||
    interaction.relationship_profile_version < 1 ||
    interaction.legacy_grant_id !== expectedGrantId ||
    typeof interaction.legacy_grant_snapshot_sha256 !== "string" ||
    !/^[a-f0-9]{64}$/.test(interaction.legacy_grant_snapshot_sha256) ||
    typeof interaction.legacy_scope_sha256 !== "string" ||
    !/^[a-f0-9]{64}$/.test(interaction.legacy_scope_sha256) ||
    typeof voiceAllowed !== "boolean" ||
    typeof interaction.legacy_expires_at !== "string" ||
    Number.isNaN(new Date(interaction.legacy_expires_at).getTime()) ||
    !/(Z|[+-]00:00)$/.test(interaction.legacy_expires_at) ||
    interaction.companion_style_id !== null ||
    interaction.companion_style_version !== null ||
    !(ownerPreview || granteeSession) ||
    !(personalVoiceAbsent || personalVoiceComplete) ||
    (voiceAllowed === false && !personalVoiceAbsent) ||
    typeof interaction.fallback_voice_profile_id !== "string" ||
    !interaction.fallback_voice_profile_id.trim() ||
    interaction.fallback_voice_provider !== "volcengine_doubao" ||
    interaction.fallback_voice_model !== "seed-tts-2.0" ||
    interaction.fallback_voice_resource_id !== "seed-tts-2.0" ||
    interaction.simulated_output !== true ||
    interaction.history_eligible !== false ||
    interaction.owner_projection_eligible !== false ||
    !isJsonObject(capabilities) ||
    capabilities.conversation !== true ||
    capabilities.private_memory !== false ||
    capabilities.persona !== false ||
    capabilities.persona_low_sensitivity !== false ||
    capabilities.tools !== false ||
    capabilities.history !== false ||
    capabilities.learning !== false ||
    capabilities.voice_profile !== expectedVoiceCapability
  ) {
    throw new Error("服务端没有返回可验证的传承模式，会话已停止");
  }
  return session;
}

export function getInteractionCapabilities() {
  return request("/v1/interaction/capabilities");
}

export async function createSession(
  userId,
  voiceBackend = "cascade",
  learningTaskId = null,
  {
    interactionMode = "companion",
    previewGrantId = null,
    legacyGrantId = null,
  } = {},
) {
  if (!["companion", "self_preview", "legacy"].includes(interactionMode)) {
    throw new Error("H5 当前只支持陪伴模式、数字分身预览或传承模式");
  }
  if (
    interactionMode === "self_preview" &&
    (typeof previewGrantId !== "string" || !previewGrantId.trim())
  ) {
    throw new Error("缺少服务端签发的数字分身预览授权");
  }
  if (
    interactionMode === "legacy" &&
    (typeof legacyGrantId !== "string" || !legacyGrantId.trim())
  ) {
    throw new Error("缺少传承授权");
  }
  const session = await request("/v1/sessions", {
    method: "POST",
    body: JSON.stringify({
      user_id: userId,
      voice_backend: interactionMode === "companion" ? voiceBackend : "cascade",
      interaction_mode: interactionMode,
      learning_task_id:
        interactionMode === "companion" ? learningTaskId : null,
      ...(interactionMode === "self_preview"
        ? { preview_grant_id: previewGrantId.trim() }
        : {}),
      ...(interactionMode === "legacy"
        ? { legacy_grant_id: legacyGrantId.trim() }
        : {}),
      locale: "zh-CN",
      client: {
        platform: "h5",
        timezone:
          Intl.DateTimeFormat().resolvedOptions().timeZone || "Asia/Shanghai",
      },
    }),
  });
  if (interactionMode === "self_preview") {
    return requireMediaRuntime(requireSelfPreviewInteraction(session));
  }
  if (interactionMode === "legacy") {
    return requireMediaRuntime(requireLegacyInteraction(session, userId, legacyGrantId.trim()));
  }
  return requireMediaRuntime(requireCompanionInteraction(session));
}

export function exchangeOmniSdp(sessionId, offerSdp) {
  return request(
    `/v1/sessions/${encodeURIComponent(sessionId)}/omni/sdp`,
    {
      method: "POST",
      headers: { "Content-Type": "application/sdp" },
      body: offerSdp,
    },
    { responseType: "text" },
  );
}

export function publishOmniTelemetry(sessionId, event) {
  return request(`/v1/sessions/${encodeURIComponent(sessionId)}/telemetry`, {
    method: "POST",
    body: JSON.stringify(event),
  });
}

export function stopResponse(sessionId, idempotencyKey = null) {
  const key =
    idempotencyKey ||
    globalThis.crypto?.randomUUID?.() ||
    `stop-${Date.now()}-${Math.random().toString(36).slice(2)}`;
  return request(`/v1/sessions/${encodeURIComponent(sessionId)}/stop-response`, {
    method: "POST",
    headers: { "Idempotency-Key": key },
    body: JSON.stringify({ reason: "user_button" }),
  });
}

export function notifyRtcRecovered(sessionId) {
  return request(`/v1/sessions/${encodeURIComponent(sessionId)}/rtc-recovered`, {
    method: "POST",
  });
}

export function reconnectMediaSession(sessionId, streamEpoch) {
  return request(`/v1/sessions/${encodeURIComponent(sessionId)}/media-reconnect`, {
    method: "POST",
    body:
      Number.isInteger(streamEpoch) && streamEpoch > 0
        ? JSON.stringify({ stream_epoch: streamEpoch })
        : undefined,
  });
}

export function fallbackMediaSession(sessionId, streamEpoch) {
  return request(`/v1/sessions/${encodeURIComponent(sessionId)}/media-fallback`, {
    method: "POST",
    body: JSON.stringify({ stream_epoch: streamEpoch }),
  });
}

export function renewMediaSession(sessionId, streamEpoch) {
  return request(`/v1/sessions/${encodeURIComponent(sessionId)}/media-heartbeat`, {
    method: "POST",
    body: JSON.stringify({ stream_epoch: streamEpoch }),
  });
}
