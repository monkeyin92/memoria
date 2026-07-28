import { request } from "./client.js";
import { isJsonObject } from "./shared.js";

export const digitalSelfStatuses = new Set([
  "draft",
  "testing",
  "approved",
  "frozen",
  "revoked",
]);
const digitalSelfManifestSchemas = new Set([
  "digital-self-manifest-v1",
  "digital-self-manifest-v2",
  "digital-self-manifest-v3",
]);

export function requireDigitalSelfVersionId(versionId) {
  if (typeof versionId !== "string" || !versionId.trim()) {
    throw new Error("数字分身版本标识无效");
  }
  return versionId.trim();
}

export function requireDigitalSelfDigest(digest) {
  if (
    typeof digest !== "string" ||
    !/^[a-f0-9]{64}$/i.test(digest.trim())
  ) {
    throw new Error("数字分身版本摘要无效，请刷新后重试");
  }
  return digest.trim().toLowerCase();
}

function invalidDigitalSelfResponse() {
  throw new Error("数字分身版本响应无效");
}

function parseNullableDigitalSelfId(value) {
  if (value === null) return null;
  if (typeof value !== "string" || !value.trim()) invalidDigitalSelfResponse();
  return value.trim();
}

function parseDigitalSelfSourceSummary(value, schemaVersion) {
  const isV2 = ["digital-self-manifest-v2", "digital-self-manifest-v3"].includes(
    schemaVersion,
  );
  const isV3 = schemaVersion === "digital-self-manifest-v3";
  const voiceProfile = parseDigitalSelfVoiceProfile(value?.voice_profile);
  if (
    !isJsonObject(value) ||
    !Number.isInteger(value.memory_claim_count) ||
    value.memory_claim_count < 0 ||
    !Number.isInteger(value.persona_trait_count) ||
    value.persona_trait_count < 0 ||
    (value.persona_version_id !== null &&
      (typeof value.persona_version_id !== "string" ||
        !value.persona_version_id.trim())) ||
    typeof value.source_summary_sha256 !== "string" ||
    !/^[a-f0-9]{64}$/i.test(value.source_summary_sha256) ||
    (isV2 &&
      (!Number.isInteger(value.cognitive_claim_count) ||
        value.cognitive_claim_count < 0 ||
        !Number.isInteger(value.decision_case_count) ||
        value.decision_case_count < 0 ||
        !Number.isInteger(value.relationship_profile_count) ||
        value.relationship_profile_count < 0))
    ||
    (!isV3 && value.voice_profile !== undefined)
  ) {
    invalidDigitalSelfResponse();
  }
  return {
    ...value,
    ...(isV2
      ? {
          cognitive_claim_count: value.cognitive_claim_count,
          decision_case_count: value.decision_case_count,
          relationship_profile_count: value.relationship_profile_count,
        }
      : {}),
    persona_version_id: value.persona_version_id?.trim() || null,
    source_summary_sha256: value.source_summary_sha256.toLowerCase(),
    ...(isV3 ? { voice_profile: voiceProfile } : {}),
  };
}

function parseDigitalSelfVoiceProfile(value) {
  if (value === undefined || value === null) {
    return null;
  }
  if (
    !isJsonObject(value) ||
    typeof value.profile_id !== "string" ||
    !value.profile_id.trim() ||
    !Number.isInteger(value.version_number) ||
    value.version_number < 1 ||
    value.provider !== "volcengine_doubao" ||
    value.target_model !== "seed-icl-2.0" ||
    value.resource_id !== "seed-icl-2.0" ||
    typeof value.provider_expires_at !== "string" ||
    Number.isNaN(new Date(value.provider_expires_at).getTime()) ||
    !/(Z|[+-]00:00)$/.test(value.provider_expires_at) ||
    typeof value.speaker_sha256 !== "string" ||
    !/^[a-f0-9]{64}$/.test(value.speaker_sha256)
  ) {
    invalidDigitalSelfResponse();
  }
  return {
    ...value,
    profile_id: value.profile_id.trim(),
    provider_expires_at: value.provider_expires_at.trim(),
    speaker_sha256: value.speaker_sha256,
  };
}

function sameDigitalSelfSourceSummary(left, right) {
  return (
    left.memory_claim_count === right.memory_claim_count &&
    left.persona_trait_count === right.persona_trait_count &&
    (left.cognitive_claim_count ?? 0) ===
      (right.cognitive_claim_count ?? 0) &&
    (left.decision_case_count ?? 0) === (right.decision_case_count ?? 0) &&
    (left.relationship_profile_count ?? 0) ===
      (right.relationship_profile_count ?? 0) &&
    left.persona_version_id === right.persona_version_id &&
    left.source_summary_sha256 === right.source_summary_sha256 &&
    JSON.stringify(left.voice_profile ?? null) ===
      JSON.stringify(right.voice_profile ?? null)
  );
}

function parseDigitalSelfVersion(value) {
  if (
    !isJsonObject(value) ||
    typeof value.version_id !== "string" ||
    !value.version_id ||
    !Number.isInteger(value.version_number) ||
    value.version_number < 1 ||
    !digitalSelfStatuses.has(value.status) ||
    typeof value.manifest_sha256 !== "string" ||
    !/^[a-f0-9]{64}$/i.test(value.manifest_sha256) ||
    !isJsonObject(value.manifest) ||
    !digitalSelfManifestSchemas.has(value.manifest.schema_version) ||
    typeof value.manifest.compiler_version !== "string" ||
    !value.manifest.compiler_version ||
    typeof value.manifest.policy_version !== "string" ||
    !value.manifest.policy_version ||
    !Array.isArray(value.manifest.entries) ||
    typeof value.created_at !== "string" ||
    !value.created_at ||
    Number.isNaN(new Date(value.created_at).getTime())
  ) {
    invalidDigitalSelfResponse();
  }
  const parentVersionId = parseNullableDigitalSelfId(value.parent_version_id);
  const rollbackTargetVersionId = parseNullableDigitalSelfId(
    value.rollback_target_version_id,
  );
  const manifestParentVersionId = parseNullableDigitalSelfId(
    value.manifest.parent_version_id,
  );
  const manifestRollbackTargetVersionId = parseNullableDigitalSelfId(
    value.manifest.rollback_target_version_id,
  );
  const sourceSummary = parseDigitalSelfSourceSummary(
    value.source_summary,
    value.manifest.schema_version,
  );
  const manifestSourceSummary = parseDigitalSelfSourceSummary(
    value.manifest.source_summary,
    value.manifest.schema_version,
  );
  if (
    parentVersionId !== manifestParentVersionId ||
    rollbackTargetVersionId !== manifestRollbackTargetVersionId ||
    !sameDigitalSelfSourceSummary(sourceSummary, manifestSourceSummary)
  ) {
    invalidDigitalSelfResponse();
  }
  return {
    ...value,
    manifest_sha256: value.manifest_sha256.toLowerCase(),
    manifest: {
      ...value.manifest,
      parent_version_id: manifestParentVersionId,
      rollback_target_version_id: manifestRollbackTargetVersionId,
      source_summary: manifestSourceSummary,
    },
    source_summary: sourceSummary,
    parent_version_id: parentVersionId,
    rollback_target_version_id: rollbackTargetVersionId,
  };
}

function parseDigitalSelfVersionList(value) {
  if (!isJsonObject(value) || !Array.isArray(value.items)) {
    throw new Error("数字分身版本列表响应无效");
  }
  return {
    ...value,
    items: value.items.map(parseDigitalSelfVersion),
  };
}

export function getDigitalSelfVersions() {
  return request("/v1/digital-self/versions").then(parseDigitalSelfVersionList);
}

export function getDigitalSelfVersion(versionId) {
  const id = requireDigitalSelfVersionId(versionId);
  return request(`/v1/digital-self/versions/${encodeURIComponent(id)}`).then(
    parseDigitalSelfVersion,
  );
}

export function buildDigitalSelfVersion() {
  return request("/v1/digital-self/versions", {
    method: "POST",
  }).then(parseDigitalSelfVersion);
}

function transitionDigitalSelfVersion(
  versionId,
  action,
  { password = null, expectedManifestSha256 },
) {
  const id = requireDigitalSelfVersionId(versionId);
  const digest = requireDigitalSelfDigest(expectedManifestSha256);
  const body = { expected_manifest_sha256: digest };
  if (password !== null) {
    if (typeof password !== "string" || !password) {
      throw new Error("请输入当前账号密码确认");
    }
    body.password = password;
  }
  return request(`/v1/digital-self/versions/${encodeURIComponent(id)}/${action}`, {
    method: "POST",
    body: JSON.stringify(body),
  }).then(parseDigitalSelfVersion);
}

export function beginDigitalSelfTesting(versionId, expectedManifestSha256) {
  return transitionDigitalSelfVersion(versionId, "testing", {
    expectedManifestSha256,
  });
}

export function approveDigitalSelfVersion(
  versionId,
  password,
  expectedManifestSha256,
) {
  return transitionDigitalSelfVersion(versionId, "approve", {
    password,
    expectedManifestSha256,
  });
}

export function freezeDigitalSelfVersion(
  versionId,
  password,
  expectedManifestSha256,
) {
  return transitionDigitalSelfVersion(versionId, "freeze", {
    password,
    expectedManifestSha256,
  });
}

export function revokeDigitalSelfVersion(
  versionId,
  password,
  expectedManifestSha256,
) {
  return transitionDigitalSelfVersion(versionId, "revoke", {
    password,
    expectedManifestSha256,
  });
}

export function rollbackDigitalSelfVersion(
  versionId,
  password,
  expectedManifestSha256,
) {
  return transitionDigitalSelfVersion(versionId, "rollback", {
    password,
    expectedManifestSha256,
  });
}
