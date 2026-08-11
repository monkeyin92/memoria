import {
  isAgeBand,
  isDeviceDeclaredMode,
  isRelationshipType,
} from "./contracts.js";
import {
  MODE_AGE_BANDS,
  MODE_PREFERENCE_SCHEMAS,
  OFFER_BY_ID,
  PRIMARY_RELATIONSHIPS,
} from "./modeMeta.js";

/**
 * 构建 POST /v1/device-bindings 请求体（整改文档 §9.1）。
 *
 * fail-closed 边界：
 * - 只接受白名单字段；任何 policy_version / *_accepted / authorized /
 *   consent_granted 之类的“假授权”字段一律抛错，客户端不能提交授权声明；
 * - declared_mode / relationship / age_band 全部走 canonical 谓词校验，
 *   非 canonical 枚举值直接拒绝（防止漂移枚举混入请求）；
 * - requiresParentSelfAcceptance 的 consent offer（如父母本人接受）只能
 *   由父母本人在设备上确认，客户端一律不得代为提交。
 */
const REQUEST_ALLOWED_KEYS = Object.freeze([
  "device_claim_token",
  "declared_mode",
  "account_owner_person_id",
  "primary_subject",
  "persona_selection",
  "service_preferences",
  "consent_offer_ids",
]);

const SUBJECT_ALLOWED_KEYS = Object.freeze(["person_id", "relationship", "subject_draft"]);
const SUBJECT_DRAFT_ALLOWED_KEYS = Object.freeze(["display_name", "age_band"]);

const FORGED_AUTHORIZATION_PATTERN = /accepted|authorized|policy_version|consent_granted/i;

function isPlainObject(value) {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function assertNonEmptyString(value, field) {
  if (typeof value !== "string" || !value.trim()) {
    throw new TypeError(`${field} 不能为空`);
  }
}

function validateQuietHours(value) {
  if (value === null || value === undefined) return null;
  if (!isPlainObject(value)) throw new TypeError("quiet_hours 格式无效");
  const { start, end } = value;
  if (
    typeof start !== "string" ||
    typeof end !== "string" ||
    !/^\d{2}:\d{2}$/.test(start) ||
    !/^\d{2}:\d{2}$/.test(end)
  ) {
    throw new TypeError("quiet_hours 需要 HH:MM 格式的 start 与 end");
  }
  return { start, end };
}

function validatePreferenceValue(schema, value, key) {
  if (schema === "boolean") {
    if (typeof value !== "boolean") throw new TypeError(`${key} 必须是布尔值`);
    return value;
  }
  if (schema === "minutes") {
    const isValidMinutes =
      typeof value === "number" &&
      Number.isFinite(value) &&
      value >= 5 &&
      value <= 120 &&
      Math.floor(value) === value;
    if (!isValidMinutes) {
      throw new TypeError(`${key} 必须是 5 到 120 的整数分钟`);
    }
    return value;
  }
  if (schema === "quiet_hours") return validateQuietHours(value);
  if (isPlainObject(schema) && Array.isArray(schema.enum)) {
    if (!schema.enum.includes(value)) {
      throw new TypeError(`${key} 取值无效：${value}`);
    }
    return value;
  }
  throw new TypeError(`${key} 缺少校验规则`);
}

function validateServicePreferences(mode, preferences) {
  const schema = MODE_PREFERENCE_SCHEMAS[mode];
  if (preferences === undefined || preferences === null) return {};
  if (!isPlainObject(preferences)) throw new TypeError("service_preferences 格式无效");
  const normalized = {};
  for (const [key, value] of Object.entries(preferences)) {
    if (!Object.prototype.hasOwnProperty.call(schema, key)) {
      throw new TypeError(`service_preferences 不允许字段 ${key}`);
    }
    normalized[key] = validatePreferenceValue(schema[key], value, key);
  }
  return normalized;
}

function validateConsentOffers(mode, offerIds) {
  if (offerIds === undefined || offerIds === null) return [];
  if (!Array.isArray(offerIds)) throw new TypeError("consent_offer_ids 必须是数组");
  const unique = new Set();
  for (const offerId of offerIds) {
    if (typeof offerId !== "string" || !offerId) {
      throw new TypeError("consent_offer_ids 包含非法项");
    }
    const offer = OFFER_BY_ID[offerId];
    if (!offer) throw new TypeError(`未知的 consent offer：${offerId}`);
    if (!offer.modes.includes(mode)) {
      throw new TypeError(`consent offer ${offerId} 不适用于 ${mode}`);
    }
    if (offer.requiresParentSelfAcceptance) {
      throw new TypeError(
        `consent offer ${offerId} 需要父母本人确认，客户端不能代为提交`,
      );
    }
    unique.add(offerId);
  }
  return [...unique];
}

function validatePrimarySubject(mode, subject) {
  if (!isPlainObject(subject)) throw new TypeError("primary_subject 格式无效");
  for (const key of Object.keys(subject)) {
    if (!SUBJECT_ALLOWED_KEYS.includes(key)) {
      throw new TypeError(`primary_subject 不允许字段 ${key}`);
    }
  }
  assertNonEmptyString(subject.person_id, "primary_subject.person_id");
  const expectedRelationship = PRIMARY_RELATIONSHIPS[mode];
  if (!isRelationshipType(subject.relationship)) {
    throw new TypeError(`primary_subject.relationship 取值无效：${subject.relationship}`);
  }
  if (subject.relationship !== expectedRelationship) {
    throw new TypeError(
      `primary_subject.relationship 必须是 ${expectedRelationship}（canonical）`,
    );
  }
  const normalized = {
    person_id: subject.person_id.trim(),
    relationship: subject.relationship,
  };
  if (subject.subject_draft !== undefined) {
    if (subject.person_id !== "new") {
      throw new TypeError("只有 person_id=new 时才能携带 subject_draft");
    }
    if (!isPlainObject(subject.subject_draft)) {
      throw new TypeError("subject_draft 格式无效");
    }
    for (const key of Object.keys(subject.subject_draft)) {
      if (!SUBJECT_DRAFT_ALLOWED_KEYS.includes(key)) {
        throw new TypeError(`subject_draft 不允许字段 ${key}`);
      }
    }
    assertNonEmptyString(
      subject.subject_draft.display_name,
      "subject_draft.display_name",
    );
    const ageBand = subject.subject_draft.age_band;
    if (!isAgeBand(ageBand)) {
      throw new TypeError(`subject_draft.age_band 取值无效：${ageBand}`);
    }
    const allowedBands = MODE_AGE_BANDS[mode];
    if (allowedBands.length > 0 && !allowedBands.includes(ageBand)) {
      throw new TypeError(`age_band ${ageBand} 不适用于 ${mode}`);
    }
    normalized.subject_draft = {
      display_name: subject.subject_draft.display_name.trim(),
      age_band: ageBand,
    };
  }
  return normalized;
}

/**
 * 绑定请求序列化。任何未知键、任何授权声明字段、任何非 canonical 枚举
 * 都会抛 TypeError；返回值只含 §9.1 白名单字段。
 */
export function buildBindingRequest(request) {
  if (!isPlainObject(request)) throw new TypeError("绑定请求格式无效");
  for (const key of Object.keys(request)) {
    if (!REQUEST_ALLOWED_KEYS.includes(key)) {
      if (FORGED_AUTHORIZATION_PATTERN.test(key)) {
        throw new TypeError(`禁止客户端提交授权声明字段 ${key}`);
      }
      throw new TypeError(`绑定请求不允许字段 ${key}`);
    }
  }
  assertNonEmptyString(request.device_claim_token, "device_claim_token");
  if (!isDeviceDeclaredMode(request.declared_mode)) {
    throw new TypeError(`declared_mode 取值无效：${request.declared_mode}`);
  }
  assertNonEmptyString(
    request.account_owner_person_id,
    "account_owner_person_id",
  );
  assertNonEmptyString(request.persona_selection, "persona_selection");
  if (request.persona_selection.length > 64) {
    throw new TypeError("persona_selection 过长");
  }
  return {
    device_claim_token: request.device_claim_token.trim(),
    declared_mode: request.declared_mode,
    account_owner_person_id: request.account_owner_person_id.trim(),
    primary_subject: validatePrimarySubject(
      request.declared_mode,
      request.primary_subject,
    ),
    persona_selection: request.persona_selection.trim(),
    service_preferences: validateServicePreferences(
      request.declared_mode,
      request.service_preferences,
    ),
    consent_offer_ids: validateConsentOffers(
      request.declared_mode,
      request.consent_offer_ids,
    ),
  };
}
