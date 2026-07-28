import { request } from "./client.js";
import { requireDigitalSelfDigest, requireDigitalSelfVersionId } from "./digital-self.js";
import { isJsonObject } from "./shared.js";

const legacyGrantRoles = new Set(["owner", "grantee"]);
const legacyGrantStatuses = new Set(["pending", "active", "expired", "revoked"]);
const legacyItemKinds = new Set([
  "memory_claim",
  "persona_trait",
  "cognitive_claim",
  "decision_case",
  "relationship_profile",
]);
const legacyVisibilities = new Set(["family", "public"]);
const legacyResponseLengths = new Set(["brief", "balanced", "detailed"]);
const legacyQuestionFrequencies = new Set(["rare", "occasional"]);

function invalidLegacyResponse(message = "传承授权响应无效") {
  throw new Error(message);
}

function legacyString(value, message = "传承授权字段无效") {
  if (typeof value !== "string" || !value.trim()) throw new Error(message);
  return value.trim();
}

function legacyDigest(value, message = "传承授权摘要无效") {
  const digest = legacyString(value, message).toLowerCase();
  if (!/^[a-f0-9]{64}$/.test(digest)) throw new Error(message);
  return digest;
}

function legacyDate(value, message = "传承授权时间无效") {
  const text = legacyString(value, message);
  if (Number.isNaN(new Date(text).getTime())) throw new Error(message);
  return text;
}

function parseLegacyAllowedItem(value) {
  if (!isJsonObject(value) || !legacyItemKinds.has(value.kind)) {
    invalidLegacyResponse();
  }
  return {
    kind: legacyString(value.kind, "传承授权范围类型无效"),
    item_id: legacyString(value.item_id, "传承授权范围条目标识无效"),
  };
}

function parseLegacyGrant(value) {
  if (
    !isJsonObject(value) ||
    !legacyGrantStatuses.has(value.status) ||
    !Number.isInteger(value.version_number) ||
    value.version_number < 1 ||
    !Number.isInteger(value.relationship_profile_version) ||
    value.relationship_profile_version < 1 ||
    !Array.isArray(value.allowed_items) ||
    !value.allowed_items.length ||
    !legacyVisibilities.has(value.visibility) ||
    typeof value.voice_allowed !== "boolean" ||
    !Number.isInteger(value.revision) ||
    value.revision < 1 ||
    !Object.prototype.hasOwnProperty.call(value, "activated_at") ||
    !Object.prototype.hasOwnProperty.call(value, "revoked_at")
  ) {
    invalidLegacyResponse();
  }
  const optionalDate = (date) => date === null ? null : legacyDate(date);
  const activatedAt = optionalDate(value.activated_at);
  const revokedAt = optionalDate(value.revoked_at);
  const allowedItems = value.allowed_items.map(parseLegacyAllowedItem);
  const shell = value.shell == null ? null : parseLegacyShellPreferences(value.shell);
  if (
    new Set(allowedItems.map((item) => `${item.kind}:${item.item_id}`)).size !==
      allowedItems.length ||
    (value.status === "pending" && (activatedAt !== null || revokedAt !== null)) ||
    (value.status === "active" && (activatedAt === null || revokedAt !== null)) ||
    (value.status === "expired" && revokedAt !== null) ||
    (value.status === "revoked" && revokedAt === null)
  ) {
    invalidLegacyResponse();
  }
  return {
    ...value,
    grant_id: legacyString(value.grant_id),
    owner_account_id: legacyString(value.owner_account_id),
    owner_username: legacyString(value.owner_username),
    grantee_account_id: legacyString(value.grantee_account_id),
    grantee_username: legacyString(value.grantee_username),
    version_id: requireDigitalSelfVersionId(value.version_id),
    manifest_sha256: requireDigitalSelfDigest(value.manifest_sha256),
    relationship_profile_id: legacyString(value.relationship_profile_id),
    allowed_items: allowedItems,
    shell,
    scope_sha256: legacyDigest(value.scope_sha256, "传承授权范围摘要无效"),
    grant_snapshot_sha256: legacyDigest(value.grant_snapshot_sha256),
    expires_at: legacyDate(value.expires_at),
    activated_at: activatedAt,
    revoked_at: revokedAt,
    created_at: legacyDate(value.created_at),
  };
}

function parseLegacyGrantList(value, expectedRole) {
  if (
    !isJsonObject(value) ||
    !legacyGrantRoles.has(value.role) ||
    value.role !== expectedRole ||
    !Array.isArray(value.items)
  ) {
    invalidLegacyResponse("传承授权列表响应无效");
  }
  return { ...value, items: value.items.map(parseLegacyGrant) };
}

function validateLegacyAllowedItems(items) {
  if (!Array.isArray(items) || !items.length) {
    throw new Error("请至少选择一项传承授权范围");
  }
  const parsed = items.map(parseLegacyAllowedItem);
  if (new Set(parsed.map((item) => `${item.kind}:${item.item_id}`)).size !== parsed.length) {
    throw new Error("传承授权范围包含重复条目");
  }
  return parsed;
}

function validateLegacyPassword(value) {
  if (typeof value !== "string" || value.length < 8) {
    throw new Error("请输入当前账号密码确认");
  }
  return value;
}

export function getLegacyGrants(role) {
  if (!legacyGrantRoles.has(role)) throw new Error("传承授权视角无效");
  return request(`/v1/legacy/grants?role=${role}`).then((value) =>
    parseLegacyGrantList(value, role),
  );
}

export function createLegacyGrant(input) {
  const body = {
    grantee_username: legacyString(input?.grantee_username, "接收人用户名无效"),
    version_id: requireDigitalSelfVersionId(input?.version_id),
    relationship_profile_id: legacyString(
      input?.relationship_profile_id,
      "关系画像标识无效",
    ),
    allowed_items: validateLegacyAllowedItems(input?.allowed_items),
    voice_allowed: input?.voice_allowed,
    expires_at: legacyDate(input?.expires_at),
    password: validateLegacyPassword(input?.password),
    idempotency_key: legacyString(input?.idempotency_key, "传承授权操作标识无效"),
  };
  if (typeof body.voice_allowed !== "boolean") {
    throw new Error("个人声音授权选项无效");
  }
  return request("/v1/legacy/grants", {
    method: "POST",
    body: JSON.stringify(body),
  }).then(parseLegacyGrant);
}

function transitionLegacyGrant(grantId, action, input) {
  const id = legacyString(grantId, "传承授权标识无效");
  const body = {
    expected_grant_snapshot_sha256: legacyDigest(
      input?.expected_grant_snapshot_sha256,
    ),
    password: validateLegacyPassword(input?.password),
    idempotency_key: legacyString(input?.idempotency_key, "传承授权操作标识无效"),
  };
  return request(`/v1/legacy/grants/${encodeURIComponent(id)}/${action}`, {
    method: "POST",
    body: JSON.stringify(body),
  }).then(parseLegacyGrant);
}

export function activateLegacyGrant(grantId, input) {
  return transitionLegacyGrant(grantId, "activate", input);
}

export function revokeLegacyGrant(grantId, input) {
  return transitionLegacyGrant(grantId, "revoke", input);
}

function parseLegacyShellPreferences(value) {
  const preferences = value?.preferences;
  if (
    !isJsonObject(value) ||
    !isJsonObject(preferences) ||
    !Number.isInteger(value.revision) ||
    value.revision < 1 ||
    !legacyResponseLengths.has(preferences.preferred_response_length) ||
    !legacyQuestionFrequencies.has(preferences.question_frequency)
  ) {
    invalidLegacyResponse("关系外壳偏好响应无效");
  }
  return {
    ...value,
    shell_id: legacyString(value.shell_id, "关系外壳标识无效"),
    grant_id: legacyString(value.grant_id, "传承授权标识无效"),
    owner_account_id: legacyString(value.owner_account_id, "关系外壳账号无效"),
    grantee_account_id: legacyString(value.grantee_account_id, "关系外壳账号无效"),
    preferred_response_length: preferences.preferred_response_length,
    question_frequency: preferences.question_frequency,
    created_at: legacyDate(value.created_at, "关系外壳时间无效"),
    updated_at: legacyDate(value.updated_at, "关系外壳时间无效"),
  };
}

export function getLegacyShellPreferences(shellId) {
  const id = legacyString(shellId, "关系外壳标识无效");
  return request(
    `/v1/legacy/shells/${encodeURIComponent(id)}/preferences`,
  ).then(parseLegacyShellPreferences);
}

export function updateLegacyShellPreferences(shellId, input) {
  const id = legacyString(shellId, "关系外壳标识无效");
  if (!Number.isInteger(input?.expected_revision) || input.expected_revision < 1) {
    throw new Error("关系外壳版本无效");
  }
  if (!legacyResponseLengths.has(input?.preferred_response_length)) {
    throw new Error("回答长度偏好无效");
  }
  if (!legacyQuestionFrequencies.has(input?.question_frequency)) {
    throw new Error("提问频率偏好无效");
  }
  const body = {
    expected_revision: input.expected_revision,
    preferred_response_length: input.preferred_response_length,
    question_frequency: input.question_frequency,
    idempotency_key: legacyString(input?.idempotency_key, "关系外壳操作标识无效"),
  };
  return request(`/v1/legacy/shells/${encodeURIComponent(id)}/preferences`, {
    method: "PATCH",
    body: JSON.stringify(body),
  }).then(parseLegacyShellPreferences);
}
