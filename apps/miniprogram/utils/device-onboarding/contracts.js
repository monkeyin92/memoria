"use strict";

/*
 * Device Bootstrap / Claim / Activation response contracts.
 *
 * These are deliberately kept outside api.js so the onboarding controller and
 * the device management page consume the same fail-closed boundary.  A
 * backend response that grows an undocumented field is rejected instead of
 * being partially trusted by the UI.
 */

const BOOTSTRAP_STATES = Object.freeze([
  "issued",
  "scanned",
  "qr_verified",
  "ble_connecting",
  "proximity_verified",
  "wifi_configuring",
  "wifi_connected",
  "device_online",
  "claim_reserved",
  "binding_committing",
  "bound",
  "activating",
  "activated",
  "expired",
  "cancelled",
  "failed",
  "revoked",
  "conflict",
]);

const CLAIM_STATES = Object.freeze([
  "reserved",
  "binding_committing",
  "binding_created",
  "fleet_projecting",
  "committed",
  "released",
  "expired",
  "conflict",
]);

const ACTIVATION_STATES = Object.freeze([
  "pending_manifest",
  "manifest_ready",
  "device_downloading",
  "device_applied",
  "device_acknowledged",
  "ready_for_conversation",
  "failed",
  "expired",
  "unknown",
]);

const ACTIVATION_RANK = Object.freeze({
  unknown: 0,
  pending_manifest: 1,
  manifest_ready: 2,
  device_downloading: 3,
  device_applied: 4,
  device_acknowledged: 5,
  ready_for_conversation: 6,
  failed: -1,
  expired: -1,
});

const QR_PAYLOAD_KEYS = Object.freeze([
  "typ",
  "ver",
  "device_id",
  "bootstrap_nonce",
  "ble_name",
  "ble_service_uuid",
  "certificate_id",
  "provisioning_protocol",
  "firmware_version",
  "pop",
]);

const ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/;
const BASE64URL_PATTERN = /^[A-Za-z0-9_-]+$/;
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const FIRMWARE_PATTERN = /^v?[0-9]+(?:\.[0-9]+){0,2}(?:[-+][A-Za-z0-9.-]+)?$/;
const RFC3339_PATTERN =
  /^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:[Zz]|[+-]\d{2}:\d{2})$/;

function isPlainObject(value) {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function invalid(message, code = "INVALID_RESPONSE") {
  const error = new TypeError(message);
  error.code = code;
  return error;
}

function assertObject(value, field) {
  if (!isPlainObject(value)) throw invalid(`${field} 必须是对象`);
  return value;
}

function assertExactKeys(value, allowed, field) {
  const allowedSet = new Set(allowed);
  for (const key of Object.keys(value)) {
    if (!allowedSet.has(key)) throw invalid(`${field} 包含未声明字段 ${key}`);
  }
}

function assertRequiredKeys(value, required, field) {
  for (const key of required) {
    if (!Object.prototype.hasOwnProperty.call(value, key)) {
      throw invalid(`${field}.${key} 缺失`);
    }
  }
}

function requiredString(value, field, { max = 256, pattern = null } = {}) {
  if (typeof value !== "string" || !value || value.trim() !== value || value.length > max) {
    throw invalid(`${field} 字符串无效`);
  }
  if (pattern && !pattern.test(value)) throw invalid(`${field} 格式无效`);
  return value;
}

function optionalString(value, field, { max = 256, pattern = null } = {}) {
  if (value === undefined || value === null) return null;
  return requiredString(value, field, { max, pattern });
}

function requiredInteger(value, field, { min = 1, max = Number.MAX_SAFE_INTEGER } = {}) {
  if (!Number.isInteger(value) || value < min || value > max) {
    throw invalid(`${field} 必须是有效整数`);
  }
  return value;
}

function optionalInteger(value, field, options = {}) {
  if (value === undefined || value === null) return null;
  return requiredInteger(value, field, options);
}

function requiredTime(value, field) {
  if (typeof value !== "string" || !RFC3339_PATTERN.test(value)) {
    throw invalid(`${field} 必须是带时区的 RFC3339 时间`);
  }
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) throw invalid(`${field} 时间无效`);
  return value;
}

function optionalTime(value, field) {
  if (value === undefined || value === null) return null;
  return requiredTime(value, field);
}

function requiredEnum(value, field, values) {
  if (!values.includes(value)) throw invalid(`${field} 取值无效：${String(value)}`);
  return value;
}

function requiredId(value, field) {
  return requiredString(value, field, { max: 256, pattern: ID_PATTERN });
}

function requiredBase64Url(value, field, { max = 4096 } = {}) {
  return requiredString(value, field, { max, pattern: BASE64URL_PATTERN });
}

function normalizeDeviceInfo(value, field = "device") {
  const device = assertObject(value, field);
  const keys = [
    "device_id",
    "display_tail",
    "model",
    "firmware_version",
    "claim_status",
  ];
  assertExactKeys(device, keys, field);
  assertRequiredKeys(device, keys, field);
  return {
    device_id: requiredId(device.device_id, `${field}.device_id`),
    display_tail: requiredString(device.display_tail, `${field}.display_tail`, { max: 32 }),
    model: requiredString(device.model, `${field}.model`, { max: 128 }),
    firmware_version: requiredString(device.firmware_version, `${field}.firmware_version`, {
      max: 64,
      pattern: FIRMWARE_PATTERN,
    }),
    claim_status: requiredEnum(device.claim_status, `${field}.claim_status`, [
      "unclaimed",
      "reserved",
      "bound",
      "suspended",
      "revoked",
    ]),
  };
}

function normalizeProvisioning(value) {
  const provisioning = assertObject(value, "provisioning");
  const keys = ["transport", "ble_name", "service_uuid", "protocol_version"];
  assertExactKeys(provisioning, keys, "provisioning");
  assertRequiredKeys(provisioning, keys, "provisioning");
  return {
    transport: requiredEnum(provisioning.transport, "provisioning.transport", ["ble"]),
    ble_name: requiredString(provisioning.ble_name, "provisioning.ble_name", { max: 64 }),
    service_uuid: requiredString(provisioning.service_uuid, "provisioning.service_uuid", {
      max: 64,
    }),
    protocol_version: requiredInteger(provisioning.protocol_version, "provisioning.protocol_version", {
      min: 1,
      max: 255,
    }),
  };
}

function normalizeIntrospectResponse(payload) {
  const value = assertObject(payload, "introspect 响应");
  const keys = [
    "onboarding_session_id",
    "state",
    "state_version",
    "activation_version",
    "expires_at",
    "device",
    "provisioning",
    "mobile_nonce",
    "claim_id",
    "binding_id",
    "activation_status",
  ];
  assertExactKeys(value, keys, "introspect 响应");
  assertRequiredKeys(value, [
    "onboarding_session_id",
    "state",
    "state_version",
    "activation_version",
    "expires_at",
    "device",
    "provisioning",
    "mobile_nonce",
  ], "introspect 响应");
  return {
    onboarding_session_id: requiredId(value.onboarding_session_id, "onboarding_session_id"),
    state: requiredEnum(value.state, "state", BOOTSTRAP_STATES),
    state_version: requiredInteger(value.state_version, "state_version"),
    activation_version: requiredInteger(value.activation_version, "activation_version", { min: 0 }),
    expires_at: requiredTime(value.expires_at, "expires_at"),
    device: normalizeDeviceInfo(value.device),
    provisioning: normalizeProvisioning(value.provisioning),
    mobile_nonce: requiredBase64Url(value.mobile_nonce, "mobile_nonce"),
    claim_id: optionalString(value.claim_id, "claim_id", { max: 256, pattern: ID_PATTERN }),
    binding_id: optionalString(value.binding_id, "binding_id", { max: 256, pattern: ID_PATTERN }),
    activation_status:
      value.activation_status === undefined || value.activation_status === null
        ? null
        : requiredEnum(value.activation_status, "activation_status", ACTIVATION_STATES),
  };
}

function normalizeNetwork(value, field = "network") {
  if (value === undefined || value === null) return null;
  const network = assertObject(value, field);
  const keys = ["status", "ssid_tail", "ip", "internet", "updated_at", "error_code"];
  assertExactKeys(network, keys, field);
  assertRequiredKeys(network, ["status", "internet"], field);
  return {
    status: requiredString(network.status, `${field}.status`, { max: 64 }),
    ssid_tail: optionalString(network.ssid_tail, `${field}.ssid_tail`, { max: 32 }),
    ip: optionalString(network.ip, `${field}.ip`, { max: 64 }),
    internet:
      typeof network.internet === "boolean"
        ? network.internet
        : (() => {
            throw invalid(`${field}.internet 必须是布尔值`);
          })(),
    updated_at: optionalTime(network.updated_at, `${field}.updated_at`),
    error_code: optionalString(network.error_code, `${field}.error_code`, { max: 64 }),
  };
}

function normalizeOnboardingSession(payload) {
  const value = assertObject(payload, "onboarding session 响应");
  const keys = [
    "onboarding_session_id",
    "state",
    "expires_at",
    "device",
    "provisioning",
    "mobile_nonce",
    "state_version",
    "activation_version",
    "last_error_code",
    "last_error_message",
    "claim_id",
    "binding_id",
    "activation_status",
    "network_status",
    "updated_at",
  ];
  assertExactKeys(value, keys, "onboarding session 响应");
  assertRequiredKeys(value, [
    "onboarding_session_id",
    "state",
    "state_version",
    "activation_version",
    "expires_at",
    "device",
    "provisioning",
  ], "onboarding session 响应");
  return {
    onboarding_session_id: requiredId(value.onboarding_session_id, "onboarding_session_id"),
    state: requiredEnum(value.state, "state", BOOTSTRAP_STATES),
    expires_at: requiredTime(value.expires_at, "expires_at"),
    device: normalizeDeviceInfo(value.device),
    provisioning: normalizeProvisioning(value.provisioning),
    mobile_nonce: optionalString(value.mobile_nonce, "mobile_nonce", { max: 4096 }),
    state_version: requiredInteger(value.state_version, "state_version"),
    activation_version: requiredInteger(value.activation_version, "activation_version", { min: 0 }),
    last_error_code: optionalString(value.last_error_code, "last_error_code", { max: 64 }),
    last_error_message: optionalString(value.last_error_message, "last_error_message", { max: 512 }),
    claim_id: optionalString(value.claim_id, "claim_id", { max: 256, pattern: ID_PATTERN }),
    binding_id: optionalString(value.binding_id, "binding_id", { max: 256, pattern: ID_PATTERN }),
    activation_status:
      value.activation_status === undefined || value.activation_status === null
        ? null
        : requiredEnum(value.activation_status, "activation_status", ACTIVATION_STATES),
    network_status: normalizeNetwork(value.network_status, "network_status"),
    updated_at: optionalTime(value.updated_at, "updated_at"),
  };
}

function normalizeClaimResponse(payload) {
  const value = assertObject(payload, "claim 响应");
  const keys = [
    "claim_id",
    "device_id",
    "onboarding_session_id",
    "status",
    "expires_at",
    "binding_id",
    "device",
  ];
  assertExactKeys(value, keys, "claim 响应");
  assertRequiredKeys(value, ["claim_id", "device_id", "onboarding_session_id", "status", "expires_at"], "claim 响应");
  return {
    claim_id: requiredId(value.claim_id, "claim_id"),
    device_id: requiredId(value.device_id, "device_id"),
    onboarding_session_id: requiredId(value.onboarding_session_id, "onboarding_session_id"),
    status: requiredEnum(value.status, "status", CLAIM_STATES),
    expires_at: requiredTime(value.expires_at, "expires_at"),
    binding_id: optionalString(value.binding_id, "binding_id", { max: 256, pattern: ID_PATTERN }),
    device: value.device === undefined || value.device === null ? null : normalizeDeviceInfo(value.device),
  };
}

function normalizeActivationResponse(payload) {
  const value = assertObject(payload, "activation 响应");
  const keys = [
    "device_id",
    "status",
    "activation_id",
    "activation_version",
    "binding_id",
    "binding_version",
    "config_hash",
    "firmware_version",
    "network",
    "network_status",
    "ready_for_conversation",
    "acknowledged_at",
    "updated_at",
    "error_code",
  ];
  assertExactKeys(value, keys, "activation 响应");
  assertRequiredKeys(value, ["device_id", "status"], "activation 响应");
  if (value.network !== undefined && value.network_status !== undefined) {
    throw invalid("activation 响应不能同时包含 network 与 network_status");
  }
  const status = requiredEnum(value.status, "status", ACTIVATION_STATES);
  const ready =
    typeof value.ready_for_conversation === "boolean"
      ? value.ready_for_conversation
      : status === "ready_for_conversation";
  return {
    device_id: requiredId(value.device_id, "device_id"),
    status,
    activation_id: optionalString(value.activation_id, "activation_id", {
      max: 256,
      pattern: ID_PATTERN,
    }),
    activation_version: optionalInteger(value.activation_version, "activation_version"),
    binding_id: optionalString(value.binding_id, "binding_id", { max: 256, pattern: ID_PATTERN }),
    binding_version: optionalInteger(value.binding_version, "binding_version"),
    config_hash: optionalString(value.config_hash, "config_hash", { max: 256 }),
    firmware_version: optionalString(value.firmware_version, "firmware_version", {
      max: 64,
      pattern: FIRMWARE_PATTERN,
    }),
    network: normalizeNetwork(value.network ?? value.network_status),
    ready_for_conversation: ready,
    acknowledged_at: optionalTime(value.acknowledged_at, "acknowledged_at"),
    updated_at: optionalTime(value.updated_at, "updated_at"),
    error_code: optionalString(value.error_code, "error_code", { max: 64 }),
  };
}

function activationRank(status) {
  return ACTIVATION_RANK[status] ?? -1;
}

function isActivationReady(status) {
  return status === "device_acknowledged" || status === "ready_for_conversation";
}

module.exports = {
  BOOTSTRAP_STATES,
  CLAIM_STATES,
  ACTIVATION_STATES,
  QR_PAYLOAD_KEYS,
  activationRank,
  isActivationReady,
  parseRequiredId: requiredId,
  parseRequiredTime: requiredTime,
  normalizeIntrospectResponse,
  normalizeOnboardingSession,
  normalizeClaimResponse,
  normalizeActivationResponse,
};
