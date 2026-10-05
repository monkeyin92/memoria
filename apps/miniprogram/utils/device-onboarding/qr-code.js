"use strict";

const { QR_PAYLOAD_KEYS } = require("./contracts");

const PREFIX = "memoria-bootstrap:v1:";
// The board shows its bootstrap payload inside this link so that WeChat's own
// scanner opens the Mini Program (扫普通链接二维码打开小程序, prefix rule
// pointing at pages/device-onboarding/index). The link carries the signed
// payload unchanged in `b`; only that payload is sent to the server.
const BIND_LINK_PREFIX = "https://aginice.cn/memoria-bind/";
// Scanners are not uniform: WeChat may return the link as printed, the whole
// link percent-encoded, or the page it would open with the link in `q`.
const BIND_LINK = /^https?:\/\/(?:www\.)?aginice\.cn\/memoria-bind\/?\?([^#]*)$/i;
const BIND_PAGE = /^\/?pages\/device-onboarding\/index\?(?:[^#]*&)?q=([^&#]+)/;
const QR_TYPE = "memoria-device-bootstrap";
const QR_VERSION = 1;
const BASE64URL_PATTERN = /^[A-Za-z0-9_-]+$/;
const ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/;
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const FIRMWARE_PATTERN = /^v?[0-9]+(?:\.[0-9]+){0,2}(?:[-+][A-Za-z0-9.-]+)?$/;

function qrError(message, code = "QR_INVALID") {
  const error = new TypeError(message);
  error.code = code;
  return error;
}

function decodeBase64Url(value) {
  if (typeof value !== "string" || !value || !BASE64URL_PATTERN.test(value)) {
    throw qrError("二维码编码段无效");
  }
  const base64 = value.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (value.length % 4)) % 4);
  let binary = "";
  if (typeof atob === "function") {
    try {
      binary = atob(base64);
    } catch {
      throw qrError("二维码编码段无法解码");
    }
  } else {
    const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    for (let index = 0; index < base64.length; index += 4) {
      const a = alphabet.indexOf(base64[index]);
      const b = alphabet.indexOf(base64[index + 1]);
      const c = base64[index + 2] === "=" ? -1 : alphabet.indexOf(base64[index + 2]);
      const d = base64[index + 3] === "=" ? -1 : alphabet.indexOf(base64[index + 3]);
      if (a < 0 || b < 0 || c < -1 || d < -1) throw qrError("二维码编码段无法解码");
      binary += String.fromCharCode((a << 2) | (b >> 4));
      if (c >= 0) binary += String.fromCharCode(((b & 15) << 4) | (c >> 2));
      if (d >= 0) binary += String.fromCharCode(((c & 3) << 6) | d);
    }
  }
  let encoded = "";
  for (let index = 0; index < binary.length; index += 1) {
    encoded += `%${binary.charCodeAt(index).toString(16).padStart(2, "0")}`;
  }
  try {
    return decodeURIComponent(encoded);
  } catch {
    throw qrError("二维码载荷不是有效 UTF-8 文本");
  }
}

function assertString(value, field, { max = 256, pattern = null } = {}) {
  if (typeof value !== "string" || !value || value.trim() !== value || value.length > max) {
    throw qrError(`${field} 无效`);
  }
  if (pattern && !pattern.test(value)) throw qrError(`${field} 格式无效`);
  return value;
}

function assertExactKeys(value) {
  const expected = new Set(QR_PAYLOAD_KEYS);
  for (const key of Object.keys(value)) {
    if (!expected.has(key)) throw qrError(`二维码包含未声明字段 ${key}`);
  }
  for (const key of QR_PAYLOAD_KEYS) {
    if (!Object.prototype.hasOwnProperty.call(value, key)) throw qrError(`二维码缺少字段 ${key}`);
  }
}

function decodeComponent(value) {
  try {
    return decodeURIComponent(value);
  } catch {
    throw qrError("设备绑定链接编码无效");
  }
}

function unwrapBindLink(scanned) {
  if (typeof scanned !== "string") return scanned;
  let text = scanned.trim();
  const page = BIND_PAGE.exec(text);
  if (page) text = decodeComponent(page[1]);
  if (/^https?%3A%2F%2F/i.test(text)) text = decodeComponent(text);
  const link = BIND_LINK.exec(text);
  if (!link) return scanned;
  const values = link[1]
    .split("&")
    .filter((part) => part.startsWith("b="))
    .map((part) => part.slice(2));
  if (values.length !== 1 || !values[0]) throw qrError("设备绑定链接格式无效");
  return decodeComponent(values[0]);
}

// Enough of a foreign code to tell what was scanned, never the query (a
// Memoria link carries the board's proof of possession there).
function describeScanned(text) {
  const head = text.split("?")[0];
  return head.length > 48 ? `${head.slice(0, 48)}…` : head;
}

function parseDeviceQr(scanned) {
  if (typeof scanned !== "string" || !scanned || scanned.length > 4096) {
    throw qrError("二维码内容为空或过长");
  }
  const rawPayload = unwrapBindLink(scanned);
  if (!rawPayload.startsWith(PREFIX)) {
    throw qrError(`不是 Memoria 设备二维码（扫到：${describeScanned(scanned) || "空内容"}）`);
  }
  const encoded = rawPayload.slice(PREFIX.length);
  const separator = encoded.indexOf(".");
  if (separator <= 0 || separator !== encoded.lastIndexOf(".")) {
    throw qrError("二维码签名结构无效");
  }
  const payloadEncoded = encoded.slice(0, separator);
  const signatureEncoded = encoded.slice(separator + 1);
  const payloadText = decodeBase64Url(payloadEncoded);
  assertString(signatureEncoded, "二维码签名", { max: 2048, pattern: BASE64URL_PATTERN });

  let payload;
  try {
    payload = JSON.parse(payloadText);
  } catch {
    throw qrError("二维码载荷不是有效 JSON");
  }
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw qrError("二维码载荷格式无效");
  }
  assertExactKeys(payload);
  if (payload.typ !== QR_TYPE || payload.ver !== QR_VERSION) {
    throw qrError("二维码协议版本不受支持", "PROTOCOL_UNSUPPORTED");
  }
  assertString(payload.device_id, "device_id", { max: 256, pattern: ID_PATTERN });
  assertString(payload.bootstrap_nonce, "bootstrap_nonce", { max: 4096, pattern: BASE64URL_PATTERN });
  assertString(payload.ble_name, "ble_name", { max: 64 });
  assertString(payload.ble_service_uuid, "ble_service_uuid", { max: 64, pattern: UUID_PATTERN });
  assertString(payload.certificate_id, "certificate_id", { max: 256, pattern: ID_PATTERN });
  assertString(payload.provisioning_protocol, "provisioning_protocol", { max: 64 });
  if (payload.provisioning_protocol !== "memoria-provisioning/1") {
    throw qrError("设备配网协议版本不受支持", "PROTOCOL_UNSUPPORTED");
  }
  assertString(payload.firmware_version, "firmware_version", {
    max: 64,
    pattern: FIRMWARE_PATTERN,
  });
  assertString(payload.pop, "pop", { max: 4096, pattern: BASE64URL_PATTERN });

  return Object.freeze({
    raw_payload: rawPayload,
    outer_version: QR_VERSION,
    payload: Object.freeze({ ...payload }),
    signature: signatureEncoded,
    server_signature_verification_required: true,
  });
}

function isMemoriaDeviceQr(rawPayload) {
  try {
    parseDeviceQr(rawPayload);
    return true;
  } catch {
    return false;
  }
}

module.exports = {
  PREFIX,
  BIND_LINK_PREFIX,
  unwrapBindLink,
  QR_TYPE,
  QR_VERSION,
  parseDeviceQr,
  isMemoriaDeviceQr,
};
