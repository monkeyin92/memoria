/*
 * 设备在线/固件/网络/激活状态的服务端权威展示（单一决策点）。
 * 首页 Dashboard 与设备页共用同一份标签与状态推导，避免两处文案漂移；
 * 展示消费 Control API 的 activation、diagnostics (live_runtime) 与 runtime profile 响应，
 * 不伪造任何实时媒体/网络本地状态。
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

function devicePlaceName(binding, companionName) {
  const name = String(companionName || "Memoria").trim() || "Memoria";
  const mode = binding?.declared_mode;
  if (mode === "family_shared" || mode === "child_for_parent") return `家人的${name}`;
  if (mode === "parent_for_child") return `孩子的${name}`;
  return `我的${name}`;
}

function presentSpeakerCandidates(candidates) {
  return (candidates || []).map((item) => {
    const confidence = Number(item.confidence) || 0;
    return {
      ...item,
      confidencePercent: Math.round(confidence * 100),
      confidenceLow: confidence < 0.5,
    };
  });
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
  const activationReady = status === "ready_for_conversation";
  const diagnosticsProvided = diagnostics !== undefined;
  const liveRuntime = diagnostics?.live_runtime;
  const hasLiveConnected =
    liveRuntime && typeof liveRuntime.connected === "boolean";

  let online = false;
  let onlineLabel = "状态暂不可用";

  if (diagnosticsProvided) {
    if (hasLiveConnected) {
      if (liveRuntime.connected && activationReady) {
        online = true;
        onlineLabel = "设备在线";
      } else {
        online = false;
        onlineLabel = offlineLabelFor(status, activation, profile);
      }
    } else if (diagnostics === null || liveRuntime === null) {
      online = false;
      onlineLabel = activationReady ? "状态待同步" : offlineLabelFor(status, activation, profile);
    } else {
      online = false;
      onlineLabel = offlineLabelFor(status, activation, profile);
    }
  } else {
    online = activationReady;
    onlineLabel = activationReady
      ? "设备在线"
      : offlineLabelFor(status, activation, profile);
  }

  return {
    online,
    onlineLabel,
    firmwareVersion:
      diagnostics?.live_runtime?.firmware_version ||
      activation?.firmware_version ||
      "未读取",
    networkLabel: activation?.network?.status || "未读取",
    activationLabel: activationLabel(status),
  };
}

module.exports = {
  ACTIVATION_LABELS,
  activationLabel,
  currentUserSummary,
  devicePlaceName,
  presentSpeakerCandidates,
  deviceStatusSummary,
};
