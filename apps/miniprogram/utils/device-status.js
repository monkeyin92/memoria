/*
 * Dashboard 仍消费 activation、diagnostics 与 runtime profile 的会话状态。
 * 设备页网络状态单独消费固件 presence 和带时效的上报证据。
 */
const ACTIVATION_LABELS = Object.freeze({
  pending_manifest: "等待配置",
  manifest_ready: "等待机器人拉取",
  device_downloading: "机器人同步中",
  device_applied: "设备已应用",
  device_acknowledged: "设备已确认",
  ready_for_conversation: "可开始对话",
  failed: "激活失败",
  expired: "激活已过期",
  unknown: "状态待同步",
});

function activationLabel(status) {
  return ACTIVATION_LABELS[status] || "状态待同步";
}

function currentUserSummary({ subjectLabel = "" } = {}) {
  const label = String(subjectLabel || "").trim();
  return label || "未设置";
}

function devicePlaceName(binding) {
  const mode = binding?.declared_mode;
  if (mode === "family_shared") return "家里的机器人";
  if (mode === "child_for_parent") return "父母的机器人";
  if (mode === "parent_for_child") return "孩子的机器人";
  return "我的机器人";
}

function offlineLabelFor(status, activation, profile) {
  if (status === "ready_for_conversation") return "暂时离线";
  if (activation?.network?.internet === true) return "已联网，等待激活";
  if (status || activation) return "暂时离线";
  if (profile) return "绑定已确认，设备状态待同步";
  return "状态暂不可用";
}

function deviceStatusSummary(activation, profile, diagnostics) {
  const status = activation?.status || diagnostics?.binding?.activation_status;
  const ready = status === "ready_for_conversation";
  const liveConnected = diagnostics?.live_runtime?.connected;
  const connectionState =
    liveConnected === true ? "online" : liveConnected === false ? "offline" : "unknown";
  const onlineLabel =
    connectionState === "online"
      ? "设备在线"
      : connectionState === "offline"
        ? "暂时离线"
        : ready
          ? "状态待确认"
          : activation?.network?.internet === true
            ? "已联网，等待激活"
            : activation
              ? "暂时离线"
              : profile
                ? "绑定已确认，设备状态待同步"
                : "状态暂不可用";

  return {
    connectionState,
    online: connectionState === "online",
    onlineLabel,
    firmwareVersion:
      diagnostics?.live_runtime?.firmware_version ||
      activation?.firmware_version ||
      "未读取",
    networkLabel: activation?.network?.status || "未读取",
    activationLabel: activationLabel(status),
  };
}

// Firmware presence is server-derived; media disconnection is not network loss.
function firmwareNetworkObservation(payload, requestedAt, now = Date.now()) {
  let state = "unknown";
  let expiresAt = Math.min(requestedAt, now) + 30_000;
  if (payload?.presence === "offline") state = "offline";
  if (payload?.presence === "busy") state = "online";
  if (payload?.presence === "online") {
    const reportedAt = typeof payload?.running?.reported_at === "string"
      ? Date.parse(payload.running.reported_at) : NaN;
    if (Number.isFinite(reportedAt) && reportedAt <= now + 5_000 && now - reportedAt < 90_000) {
      state = "online";
      expiresAt = Math.min(expiresAt, Math.min(reportedAt, now) + 90_000);
    }
  }
  return { state, receivedAt: now, expiresAt };
}

function networkStatusView(observation, now = Date.now()) {
  const state = observation && now >= observation.receivedAt && now < observation.expiresAt
    ? observation.state : "unknown";
  return {
    networkObservation: observation || null,
    deviceConnectionState: state,
    deviceConnectionAction: state === "offline" ? "showConnectionHelp" : "",
    online: state === "online",
    onlineLabel: state === "online" ? "设备在线" : state === "offline" ? "暂时离线" : "状态待确认",
  };
}

module.exports = {
  firmwareNetworkObservation,
  networkStatusView,
  ACTIVATION_LABELS,
  activationLabel,
  currentUserSummary,
  devicePlaceName,
  deviceStatusSummary,
};
