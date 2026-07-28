import { request } from "./client.js";
import { digitalSelfStatuses, requireDigitalSelfDigest, requireDigitalSelfVersionId } from "./digital-self.js";
import { createClientMessageId, isJsonObject, selfPreviewPerspectives } from "./shared.js";

const previewGrantStatuses = new Set(["active", "revoked", "expired"]);
const fidelityCategories = new Set([
  "fact",
  "decision",
  "relationship",
  "humor",
  "emotion",
  "unknown",
  "privacy",
]);
const fidelityStatuses = new Set(["active", "completed"]);
const fidelityVerdicts = new Set(["approve", "reject"]);

function invalidPreviewResponse(message = "数字分身预览响应无效") {
  throw new Error(message);
}

function previewString(value, message = "数字分身预览字段无效") {
  if (typeof value !== "string" || !value.trim()) {
    throw new Error(message);
  }
  return value.trim();
}

function previewDate(value) {
  const text = previewString(value, "数字分身预览时间无效");
  if (Number.isNaN(new Date(text).getTime())) invalidPreviewResponse();
  return text;
}

function parsePreviewVersionSummary(value) {
  if (
    !isJsonObject(value) ||
    !digitalSelfStatuses.has(value.status) ||
    typeof value.version_stale !== "boolean" ||
    typeof value.fidelity_eligible !== "boolean" ||
    typeof value.preview_eligible !== "boolean" ||
    (value.fidelity_verdict !== null &&
      !fidelityVerdicts.has(value.fidelity_verdict))
  ) {
    invalidPreviewResponse();
  }
  return {
    ...value,
    version_id: requireDigitalSelfVersionId(value.version_id),
    manifest_sha256: requireDigitalSelfDigest(value.manifest_sha256),
    version_stale: value.version_stale === true,
  };
}

function parseSelfPreviewCapability(value) {
  if (
    !isJsonObject(value) ||
    !["available", "blocked"].includes(value.status) ||
    typeof value.registered_owner !== "boolean" ||
    typeof value.active_owner_voice !== "boolean" ||
    !Array.isArray(value.missing) ||
    value.missing.some((item) => typeof item !== "string" || !item)
  ) {
    invalidPreviewResponse("数字分身预览能力响应无效");
  }
  const rawVersions = value.versions || value.available_versions || [];
  if (!Array.isArray(rawVersions)) invalidPreviewResponse();
  return {
    ...value,
    conversational: value.conversational === true,
    missing: [...value.missing],
    versions: rawVersions.map(parsePreviewVersionSummary),
  };
}

export function getSelfPreviewCapability() {
  return request("/v1/digital-self/preview-capability").then(
    parseSelfPreviewCapability,
  );
}

function parsePreviewGrant(value) {
  if (
    !isJsonObject(value) ||
    !previewGrantStatuses.has(value.status) ||
    !selfPreviewPerspectives.has(value.perspective) ||
    value.simulation_only !== true ||
    value.legacy_authority !== false
  ) {
    invalidPreviewResponse("数字分身预览授权响应无效");
  }
  return {
    ...value,
    grant_id: previewString(value.grant_id, "预览授权标识无效"),
    version_id: requireDigitalSelfVersionId(value.version_id),
    manifest_sha256: requireDigitalSelfDigest(value.manifest_sha256),
    expires_at: previewDate(value.expires_at),
    created_at: previewDate(value.created_at),
    used_at: value.used_at === null ? null : previewDate(value.used_at),
    revoked_at:
      value.revoked_at === null ? null : previewDate(value.revoked_at),
  };
}

export function issueSelfPreviewGrant({
  versionId,
  manifestSha256,
  perspective = "owner",
  password,
  idempotencyKey = createClientMessageId(),
}) {
  if (!selfPreviewPerspectives.has(perspective)) {
    throw new Error("数字分身预览视角无效");
  }
  if (typeof password !== "string" || password.length < 8) {
    throw new Error("请输入当前账号密码确认");
  }
  return request("/v1/digital-self/preview-grants", {
    method: "POST",
    body: JSON.stringify({
      version_id: requireDigitalSelfVersionId(versionId),
      manifest_sha256: requireDigitalSelfDigest(manifestSha256),
      perspective,
      password,
      idempotency_key: previewString(idempotencyKey, "预览操作标识无效"),
    }),
  }).then(parsePreviewGrant);
}

export function revokeSelfPreviewGrant(grantId) {
  const id = previewString(grantId, "预览授权标识无效");
  return request(
    `/v1/digital-self/preview-grants/${encodeURIComponent(id)}/revoke`,
    { method: "POST" },
  ).then(parsePreviewGrant);
}

function parsePreviewSource(value) {
  if (
    !isJsonObject(value) ||
    typeof value.excerpt !== "string" ||
    value.excerpt.length > 400
  ) {
    invalidPreviewResponse("回答来源响应无效");
  }
  return {
    kind: previewString(value.kind, "回答来源类型无效"),
    item_id: previewString(value.item_id, "回答来源条目标识无效"),
    source_event_id: previewString(
      value.source_event_id,
      "回答来源事件标识无效",
    ),
    excerpt: value.excerpt,
  };
}

export function getSelfPreviewSources({
  sessionId,
  turnId,
  generationId,
  toolEpoch,
}) {
  const session = previewString(sessionId, "预览会话标识无效");
  for (const [label, value] of [
    ["话轮", turnId],
    ["生成", generationId],
    ["工具纪元", toolEpoch],
  ]) {
    if (!Number.isInteger(value) || value < 0) {
      throw new Error(`${label}标识无效`);
    }
  }
  return request(
    `/v1/digital-self/preview-sessions/${encodeURIComponent(session)}` +
      `/turns/${turnId}/generations/${generationId}/sources` +
      `?tool_epoch=${toolEpoch}`,
  ).then((value) => {
    if (
      !isJsonObject(value) ||
      value.session_id !== session ||
      value.turn_id !== turnId ||
      value.generation_id !== generationId ||
      value.tool_epoch !== toolEpoch ||
      !Array.isArray(value.items) ||
      value.items.length > 12
    ) {
      invalidPreviewResponse("回答来源响应无效");
    }
    return {
      ...value,
      items: value.items.map(parsePreviewSource),
    };
  });
}

export function submitSelfPreviewFeedback({
  sessionId,
  turnId,
  generationId,
  toolEpoch,
  versionId,
  manifestSha256,
  action,
  targetSourceEventIds,
  correctionText = null,
  eventId = createClientMessageId(),
  idempotencyKey = eventId,
}) {
  if (!["not_like_me", "correction"].includes(action)) {
    throw new Error("数字分身纠错动作无效");
  }
  if (
    !Array.isArray(targetSourceEventIds) ||
    !targetSourceEventIds.length ||
    targetSourceEventIds.length > 12
  ) {
    throw new Error("数字分身纠错来源无效");
  }
  if (
    action === "correction" &&
    (typeof correctionText !== "string" || !correctionText.trim())
  ) {
    throw new Error("请输入你的纠正内容");
  }
  return request("/v1/digital-self/preview-feedback", {
    method: "POST",
    body: JSON.stringify({
      event_id: previewString(eventId, "反馈事件标识无效"),
      idempotency_key: previewString(idempotencyKey, "反馈操作标识无效"),
      session_id: previewString(sessionId, "预览会话标识无效"),
      turn_id: turnId,
      generation_id: generationId,
      tool_epoch: toolEpoch,
      version_id: requireDigitalSelfVersionId(versionId),
      manifest_sha256: requireDigitalSelfDigest(manifestSha256),
      action,
      target_source_event_ids: targetSourceEventIds.map((value) =>
        previewString(value, "反馈来源事件标识无效"),
      ),
      correction_text:
        action === "correction" ? correctionText.trim() : null,
    }),
  }).then((value) => {
    if (
      !isJsonObject(value) ||
      value.version_stale !== true ||
      value.rebuild_required !== true
    ) {
      invalidPreviewResponse("数字分身纠错响应无效");
    }
    return value;
  });
}

function parseFidelityTrial(value) {
  if (
    !isJsonObject(value) ||
    !fidelityCategories.has(value.category) ||
    typeof value.prompt !== "string" ||
    typeof value.slot_a !== "string" ||
    typeof value.slot_b !== "string" ||
    typeof value.available !== "boolean" ||
    (value.preferred_slot !== null &&
      !["a", "b"].includes(value.preferred_slot))
  ) {
    invalidPreviewResponse("忠实度保留题响应无效");
  }
  return {
    ...value,
    trial_id: previewString(value.trial_id, "忠实度保留题标识无效"),
    coverage_gap:
      value.coverage_gap === null
        ? null
        : previewString(value.coverage_gap, "忠实度覆盖缺口无效"),
  };
}

function parseFidelityEvaluation(value) {
  if (
    !isJsonObject(value) ||
    !fidelityStatuses.has(value.status) ||
    (value.verdict !== null && !fidelityVerdicts.has(value.verdict)) ||
    (value.status === "active" && value.verdict !== null) ||
    (value.status === "completed" && value.verdict === null) ||
    !Array.isArray(value.trials) ||
    !isJsonObject(value.summary) ||
    value.mapping_hidden !== true
  ) {
    invalidPreviewResponse("忠实度评测响应无效");
  }
  return {
    ...value,
    evaluation_id: previewString(value.evaluation_id, "忠实度评测标识无效"),
    version_id: requireDigitalSelfVersionId(value.version_id),
    manifest_sha256: requireDigitalSelfDigest(value.manifest_sha256),
    created_at: previewDate(value.created_at),
    completed_at:
      value.completed_at === null ? null : previewDate(value.completed_at),
    trials: value.trials.map(parseFidelityTrial),
  };
}

export function getFidelityEvaluations() {
  return request("/v1/digital-self/fidelity-evaluations").then((value) => {
    if (!isJsonObject(value) || !Array.isArray(value.items)) {
      invalidPreviewResponse("忠实度评测列表响应无效");
    }
    return { ...value, items: value.items.map(parseFidelityEvaluation) };
  });
}

export function getFidelityEvaluation(evaluationId) {
  const id = previewString(evaluationId, "忠实度评测标识无效");
  return request(
    `/v1/digital-self/fidelity-evaluations/${encodeURIComponent(id)}`,
  ).then(parseFidelityEvaluation);
}

export function startFidelityEvaluation({
  versionId,
  manifestSha256,
  password = null,
  previewGrantId = null,
  idempotencyKey = createClientMessageId(),
}) {
  if ((password === null) === (previewGrantId === null)) {
    throw new Error("忠实度评测需要密码确认或有效预览授权");
  }
  return request("/v1/digital-self/fidelity-evaluations", {
    method: "POST",
    body: JSON.stringify({
      version_id: requireDigitalSelfVersionId(versionId),
      manifest_sha256: requireDigitalSelfDigest(manifestSha256),
      idempotency_key: previewString(idempotencyKey, "忠实度操作标识无效"),
      password,
      preview_grant_id: previewGrantId,
    }),
  }).then(parseFidelityEvaluation);
}

export function chooseFidelityTrial({
  evaluationId,
  trialId,
  preferredSlot,
  rationale = null,
}) {
  if (!["a", "b"].includes(preferredSlot)) {
    throw new Error("请选择回答 A 或 B");
  }
  const evaluation = previewString(evaluationId, "忠实度评测标识无效");
  const trial = previewString(trialId, "忠实度保留题标识无效");
  return request(
    `/v1/digital-self/fidelity-evaluations/${encodeURIComponent(evaluation)}` +
      `/trials/${encodeURIComponent(trial)}/choice`,
    {
      method: "POST",
      body: JSON.stringify({
        preferred_slot: preferredSlot,
        rationale,
      }),
    },
  ).then(parseFidelityEvaluation);
}

export function completeFidelityEvaluation({
  evaluationId,
  verdict,
  rationale = null,
}) {
  if (!fidelityVerdicts.has(verdict)) {
    throw new Error("忠实度评测结论无效");
  }
  const id = previewString(evaluationId, "忠实度评测标识无效");
  return request(
    `/v1/digital-self/fidelity-evaluations/${encodeURIComponent(id)}/verdict`,
    {
      method: "POST",
      body: JSON.stringify({ verdict, rationale }),
    },
  ).then(parseFidelityEvaluation);
}
