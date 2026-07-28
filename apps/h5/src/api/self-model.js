import { request } from "./client.js";
import { isJsonObject } from "./shared.js";

const selfModelClaimTypes = new Set([
  "belief",
  "preference",
  "value",
  "decision_rule",
  "red_line",
  "uncertainty",
  "conflict",
  "support",
]);
const selfModelItemStatuses = new Set([
  "candidate",
  "confirmed",
  "disputed",
  "retracted",
  "superseded",
]);
const selfModelRelationshipStatuses = new Set([
  "candidate",
  "approved",
  "revoked",
  "superseded",
]);
const selfModelDecisionKinds = new Set(["real", "hypothetical"]);
const selfModelSourceRelations = new Set(["support", "counterexample"]);

function invalidSelfModelResponse() {
  throw new Error("认知与关系模型响应无效");
}

function parseSelfModelDate(value, { nullable = false } = {}) {
  if (nullable && value === null) return null;
  if (typeof value !== "string" || Number.isNaN(new Date(value).getTime())) {
    invalidSelfModelResponse();
  }
  return value;
}

function parseSelfModelSource(value) {
  if (
    !isJsonObject(value) ||
    typeof value.source_event_id !== "string" ||
    !value.source_event_id ||
    !selfModelSourceRelations.has(value.relation) ||
    typeof value.adopted !== "boolean" ||
    typeof value.negative !== "boolean" ||
    value.speaker_class !== "owner" ||
    typeof value.excerpt !== "string"
  ) {
    invalidSelfModelResponse();
  }
  return {
    source_event_id: value.source_event_id,
    relation: value.relation,
    adopted: value.adopted,
    negative: value.negative,
    speaker_class: value.speaker_class,
    occurred_at: parseSelfModelDate(value.occurred_at),
    excerpt: value.excerpt,
  };
}

function parseSelfModelCommon(value, statuses) {
  if (
    !isJsonObject(value) ||
    !statuses.has(value.status) ||
    typeof value.sharing_scope !== "string" ||
    !value.sharing_scope ||
    typeof value.unresolved_conflict !== "boolean" ||
    !Array.isArray(value.sources) ||
    typeof value.step_up_verified !== "boolean" ||
    typeof value.effective !== "boolean" ||
    !Array.isArray(value.effective_reasons) ||
    value.effective_reasons.some((reason) => typeof reason !== "string" || !reason)
  ) {
    invalidSelfModelResponse();
  }
  return {
    status: value.status,
    sharing_scope: value.sharing_scope,
    unresolved_conflict: value.unresolved_conflict,
    sources: value.sources.map(parseSelfModelSource),
    owner_reviewed_at: parseSelfModelDate(value.owner_reviewed_at, {
      nullable: true,
    }),
    step_up_verified: value.step_up_verified,
    effective: value.effective,
    effective_reasons: [...value.effective_reasons],
    created_at: parseSelfModelDate(value.created_at),
  };
}

function parseSelfModelClaim(value) {
  const common = parseSelfModelCommon(value, selfModelItemStatuses);
  if (
    value.kind !== "cognitive_claim" ||
    typeof value.claim_id !== "string" ||
    !value.claim_id ||
    !selfModelClaimTypes.has(value.claim_type) ||
    typeof value.statement !== "string" ||
    !value.statement ||
    typeof value.context !== "string" ||
    typeof value.confidence !== "number" ||
    value.confidence < 0 ||
    value.confidence > 1 ||
    !Number.isInteger(value.version) ||
    value.version < 1
  ) {
    invalidSelfModelResponse();
  }
  return {
    ...value,
    ...common,
    updated_at: parseSelfModelDate(value.updated_at),
  };
}

function parseSelfModelDecisionCase(value) {
  const common = parseSelfModelCommon(value, selfModelItemStatuses);
  if (
    value.kind !== "decision_case" ||
    typeof value.case_id !== "string" ||
    !value.case_id ||
    !selfModelDecisionKinds.has(value.decision_kind) ||
    typeof value.context !== "string" ||
    !value.context ||
    !Array.isArray(value.options) ||
    !Array.isArray(value.constraints) ||
    typeof value.chosen_option !== "string" ||
    !Array.isArray(value.rejected_options) ||
    typeof value.outcome !== "string" ||
    typeof value.reflection !== "string" ||
    typeof value.still_endorsed !== "boolean" ||
    !Number.isInteger(value.version) ||
    value.version < 1
  ) {
    invalidSelfModelResponse();
  }
  return {
    ...value,
    ...common,
    updated_at: parseSelfModelDate(value.updated_at),
  };
}

function parseSelfModelRelationshipProfile(value) {
  const common = parseSelfModelCommon(value, selfModelRelationshipStatuses);
  if (
    value.kind !== "relationship_profile" ||
    typeof value.profile_id !== "string" ||
    !value.profile_id ||
    !Number.isInteger(value.version_number) ||
    value.version_number < 1 ||
    typeof value.person_id !== "string" ||
    !value.person_id ||
    typeof value.relationship_id !== "string" ||
    !value.relationship_id ||
    typeof value.salutation !== "string" ||
    typeof value.tone !== "string" ||
    typeof value.advice_style !== "string" ||
    !Array.isArray(value.boundaries)
  ) {
    invalidSelfModelResponse();
  }
  return { ...value, ...common };
}

function parseSelfModel(value) {
  if (
    !isJsonObject(value) ||
    !Array.isArray(value.claims) ||
    !Array.isArray(value.decision_cases) ||
    !Array.isArray(value.relationship_profiles)
  ) {
    invalidSelfModelResponse();
  }
  return {
    claims: value.claims.map(parseSelfModelClaim),
    decision_cases: value.decision_cases.map(parseSelfModelDecisionCase),
    relationship_profiles: value.relationship_profiles.map(
      parseSelfModelRelationshipProfile,
    ),
  };
}

function requireSelfModelId(value, label) {
  if (typeof value !== "string" || !value.trim()) {
    throw new Error(`${label}无效`);
  }
  return value.trim();
}

function selfModelReviewBody(status, expectedVersion, idempotencyKey, password) {
  if (!Number.isInteger(expectedVersion) || expectedVersion < 1) {
    throw new Error("认知材料版本无效");
  }
  const body = {
    status: requireSelfModelId(status, "审核状态"),
    expected_version: expectedVersion,
    idempotency_key: requireSelfModelId(idempotencyKey, "操作标识"),
  };
  if (password) body.password = password;
  return body;
}

export function getSelfModel() {
  return request("/v1/self-model").then(parseSelfModel);
}

export function addSelfModelClaimCounterexample(
  claimId,
  text,
  expectedVersion,
  eventId,
) {
  const id = requireSelfModelId(claimId, "认知主张标识");
  const counterexample = requireSelfModelId(text, "例外说明");
  if (!Number.isInteger(expectedVersion) || expectedVersion < 1) {
    throw new Error("认知材料版本无效");
  }
  return request(
    `/v1/self-model/claims/${encodeURIComponent(id)}/counterexamples`,
    {
      method: "POST",
      body: JSON.stringify({
        event_id: requireSelfModelId(eventId, "操作标识"),
        expected_version: expectedVersion,
        text: counterexample,
      }),
    },
  ).then(parseSelfModelClaim);
}

export function reviewSelfModelClaim(
  claimId,
  status,
  expectedVersion,
  idempotencyKey,
  password = "",
) {
  const id = requireSelfModelId(claimId, "认知主张标识");
  return request(`/v1/self-model/claims/${encodeURIComponent(id)}/review`, {
    method: "POST",
    body: JSON.stringify(
      selfModelReviewBody(status, expectedVersion, idempotencyKey, password),
    ),
  }).then(parseSelfModelClaim);
}

export function reviewSelfModelDecisionCase(
  caseId,
  status,
  expectedVersion,
  idempotencyKey,
) {
  const id = requireSelfModelId(caseId, "决策案例标识");
  return request(
    `/v1/self-model/decision-cases/${encodeURIComponent(id)}/review`,
    {
      method: "POST",
      body: JSON.stringify(
        selfModelReviewBody(status, expectedVersion, idempotencyKey, ""),
      ),
    },
  ).then(parseSelfModelDecisionCase);
}

export function reviewSelfModelRelationshipProfile(
  profileId,
  versionNumber,
  status,
  expectedStatus,
  idempotencyKey,
  password,
) {
  const id = requireSelfModelId(profileId, "关系画像标识");
  if (!Number.isInteger(versionNumber) || versionNumber < 1) {
    throw new Error("关系画像版本无效");
  }
  return request(
    `/v1/self-model/relationship-profiles/${encodeURIComponent(id)}/versions/${versionNumber}/review`,
    {
      method: "POST",
      body: JSON.stringify({
        status: requireSelfModelId(status, "审核状态"),
        expected_status: requireSelfModelId(expectedStatus, "预期状态"),
        idempotency_key: requireSelfModelId(idempotencyKey, "操作标识"),
        password: requireSelfModelId(password, "账号密码"),
      }),
    },
  ).then(parseSelfModelRelationshipProfile);
}
