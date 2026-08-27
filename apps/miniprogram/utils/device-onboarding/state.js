"use strict";

const { BOOTSTRAP_STATES, activationRank, isActivationReady } = require("./contracts");

const CLIENT_STATES = Object.freeze([
  "prepare",
  "scan",
  "device_verified",
  "ble",
  "wifi",
  "progress",
  "claim",
  "initialize",
  "activation",
  "complete",
]);

const STATE_LABELS = Object.freeze({
  prepare: "准备设备",
  scan: "扫描设备",
  device_verified: "设备已确认",
  ble: "连接机器人",
  wifi: "配置家庭 Wi‑Fi",
  progress: "等待机器人联网",
  claim: "认领设备",
  initialize: "初始化机器人",
  activation: "等待设备激活",
  complete: "可以开始对话",
});

const TRANSITIONS = Object.freeze({
  prepare: ["scan"],
  scan: ["device_verified", "prepare"],
  device_verified: ["ble", "scan"],
  ble: ["wifi", "device_verified", "scan"],
  wifi: ["progress", "ble", "device_verified"],
  progress: ["claim", "wifi", "device_verified", "scan"],
  claim: ["initialize", "progress", "scan"],
  initialize: ["activation", "claim", "scan"],
  activation: ["complete", "initialize", "scan"],
  complete: ["activation", "prepare"],
});

const PROGRESS_STEPS = Object.freeze([
  { key: "credentials_received", label: "已安全发送网络信息" },
  { key: "associating", label: "正在连接路由器" },
  { key: "got_ip", label: "已获取网络地址" },
  { key: "internet_ready", label: "正在检查互联网" },
  { key: "cloud_connected", label: "正在连接 Memoria" },
  { key: "device_proof_accepted", label: "设备身份验证成功" },
]);

const ERROR_MESSAGES = Object.freeze({
  QR_INVALID: "无法识别设备码，请重新扫描机器人屏幕上的二维码。",
  QR_SIGNATURE_INVALID: "设备码校验失败，请刷新机器人屏幕上的二维码。",
  QR_SESSION_EXPIRED: "设备码已失效，请让机器人重新进入启用模式后再扫。",
  DEVICE_REVOKED: "设备当前不可启用，请联系设备管理员或客服。",
  DEVICE_ALREADY_BOUND: "设备已经绑定到其他账号，不能在此账号重新认领。",
  PROTOCOL_UNSUPPORTED:
    "当前固件的安全配网协议未完成兼容，已停止发送网络信息。请升级机器人固件后重试。",
  BLUETOOTH_DISABLED: "请打开手机蓝牙，再返回这里重试。",
  BLUETOOTH_PERMISSION_DENIED: "无法使用蓝牙权限，请在系统设置中允许微信使用蓝牙。",
  BLE_DEVICE_NOT_FOUND: "没有找到屏幕上的机器人，请靠近设备并重试。",
  BLE_CONNECTION_FAILED: "机器人连接失败，请确认设备已通电并重试。",
  BLE_SESSION_REJECTED: "机器人安全会话未通过，请刷新二维码后重试。",
  BLE_REAUTH_REQUIRED: "蓝牙安全会话已失效，请重新扫描机器人二维码后连接。",
  BLE_DISCONNECTED: "机器人蓝牙连接中断，可以从当前启用会话继续。",
  WIFI_AUTH_FAILED: "Wi‑Fi 密码可能不正确，请重新输入。",
  WIFI_AP_NOT_FOUND: "没有找到这个 Wi‑Fi，请选择其他网络或检查路由器。",
  WIFI_DHCP_FAILED: "路由器没有给机器人分配地址，请检查路由器后重试。",
  WIFI_NO_INTERNET: "机器人已连接 Wi‑Fi，但暂时无法访问互联网。",
  CLOUD_TLS_FAILED: "机器人无法建立安全云端连接，请检查网络或升级固件。",
  DEVICE_ONLINE_TIMEOUT: "机器人暂未连接到云端，可以稍后继续查看状态。",
  CLAIM_CONFLICT: "设备正在被其他账号设置，请稍后重试。",
  CLAIM_EXPIRED: "本次认领已超时，请重新保留认领，不需要重输 Wi‑Fi。",
  BINDING_FAILED: "机器人初始化没有完成，请从当前启用会话重试。",
  ACTIVATION_ACK_TIMEOUT: "配置已保存，机器人仍在同步；稍后刷新设备状态即可。",
  DEVICE_FIRMWARE_BLOCKED: "当前固件版本不能启用，请先升级机器人固件。",
});

const SERVER_TO_CLIENT_STATE = Object.freeze({
  issued: "scan",
  scanned: "scan",
  qr_verified: "device_verified",
  ble_connecting: "ble",
  proximity_verified: "wifi",
  wifi_configuring: "wifi",
  wifi_connected: "progress",
  device_online: "progress",
  claim_reserved: "claim",
  binding_committing: "initialize",
  bound: "activation",
  activating: "activation",
  activated: "complete",
});

function assertClientState(value) {
  if (!CLIENT_STATES.includes(value)) throw new TypeError(`未知的小程序启用状态：${value}`);
  return value;
}

function canTransition(from, to) {
  assertClientState(from);
  assertClientState(to);
  return from === to || (TRANSITIONS[from] || []).includes(to);
}

function transition(from, to) {
  if (!canTransition(from, to)) {
    throw new Error(`不允许从 ${from} 进入 ${to}`);
  }
  return to;
}

function clientStateForServerState(state) {
  if (!BOOTSTRAP_STATES.includes(state)) return null;
  return SERVER_TO_CLIENT_STATE[state] || null;
}

function isExpired(expiresAt, now = Date.now()) {
  const timestamp = Date.parse(expiresAt || "");
  return !Number.isFinite(timestamp) || timestamp <= now;
}

function errorMessage(error, fallback = "启用流程没有完成，请稍后重试。") {
  const code = error?.code || error?.detail?.code;
  if (code === "QR_INVALID" && typeof error?.clientDetail === "string" && error.clientDetail) {
    return `无法识别设备码：${error.clientDetail}。`;
  }
  return (code && ERROR_MESSAGES[code]) || error?.message || fallback;
}

function errorCode(error) {
  return error?.code || error?.detail?.code || "ONBOARDING_FAILED";
}

function progressIndex({ state, networkStatus, activationStatus } = {}) {
  if (activationStatus === "ready_for_conversation") return PROGRESS_STEPS.length;
  if (activationStatus === "device_acknowledged") return PROGRESS_STEPS.length;
  const status = networkStatus?.status || "";
  const byStatus = {
    credentials_received: 1,
    associating: 2,
    got_ip: 3,
    dns_ready: 3,
    internet_ready: 4,
    cloud_connected: 5,
    device_proof_accepted: 6,
  };
  if (byStatus[status]) return byStatus[status];
  if (state === "device_online") return 6;
  if (state === "wifi_connected") return 3;
  return 0;
}

function activationIsLateOrReady(previous, next) {
  if (!previous) return false;
  if (isActivationReady(previous.status) && !isActivationReady(next.status)) return true;
  return activationRank(next.status) < activationRank(previous.status) && next.status !== "failed";
}

module.exports = {
  CLIENT_STATES,
  STATE_LABELS,
  TRANSITIONS,
  PROGRESS_STEPS,
  ERROR_MESSAGES,
  assertClientState,
  canTransition,
  transition,
  clientStateForServerState,
  isExpired,
  errorMessage,
  errorCode,
  progressIndex,
  activationIsLateOrReady,
};
