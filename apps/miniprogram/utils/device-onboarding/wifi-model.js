"use strict";

function wifiError(message, code = "WIFI_INVALID") {
  const error = new TypeError(message);
  error.code = code;
  return error;
}

function normalizeWifiNetwork(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw wifiError("设备返回的 Wi‑Fi 网络无效");
  }
  const allowed = new Set(["ssid", "rssi", "security", "channel", "saved"]);
  for (const key of Object.keys(value)) {
    if (!allowed.has(key)) throw wifiError(`设备返回未知 Wi‑Fi 字段 ${key}`);
  }
  if (typeof value.ssid !== "string" || !value.ssid || value.ssid.length > 128) {
    throw wifiError("Wi‑Fi 名称无效");
  }
  if (!Number.isFinite(value.rssi) || value.rssi < -120 || value.rssi > 20) {
    throw wifiError("Wi‑Fi 信号无效");
  }
  if (typeof value.security !== "string" || value.security.length > 32) {
    throw wifiError("Wi‑Fi 安全类型无效");
  }
  if (!Number.isInteger(value.channel) || value.channel < 1 || value.channel > 196) {
    throw wifiError("Wi‑Fi 信道无效");
  }
  if (value.saved !== undefined && typeof value.saved !== "boolean") {
    throw wifiError("Wi‑Fi saved 字段无效");
  }
  return {
    ssid: value.ssid,
    rssi: value.rssi,
    security: value.security,
    channel: value.channel,
    saved: value.saved === true,
  };
}

function normalizeWifiNetworks(value) {
  if (!Array.isArray(value)) throw wifiError("设备返回的 Wi‑Fi 列表无效");
  const bySsid = new Map();
  for (const item of value) {
    const network = normalizeWifiNetwork(item);
    const previous = bySsid.get(network.ssid);
    if (!previous || network.rssi > previous.rssi) bySsid.set(network.ssid, network);
  }
  return [...bySsid.values()].sort((left, right) => right.rssi - left.rssi);
}

function getConnectedWifi(wxApi = globalThis.wx) {
  return new Promise((resolve) => {
    if (!wxApi || typeof wxApi.getConnectedWifi !== "function") {
      resolve(null);
      return;
    }
    wxApi.getConnectedWifi({
      success(result) {
        const ssid = result?.wifi?.SSID || result?.wifi?.ssid;
        resolve(typeof ssid === "string" && ssid ? { ssid } : null);
      },
      fail() {
        resolve(null);
      },
    });
  });
}

module.exports = {
  normalizeWifiNetwork,
  normalizeWifiNetworks,
  getConnectedWifi,
};
