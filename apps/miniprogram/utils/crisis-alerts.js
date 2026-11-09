/*
 * 绑定门禁必须在用户 tap 的同步调用栈里调起 requestSubscribeMessage，
 * 因此配置要在进入确认页时预加载，提交时不能先 await 配置。
 */

const guardianPush = require("./guardian-push");
const { SUBSCRIPTION_PUSH_ENABLED } = require("../config");
const { UNREAD_SAFETY_COPY } = require("./trial-device-card");

const BIND_SUBSCRIBE_UNAVAILABLE =
  "安全提醒订阅尚未开通，暂时不能完成这类绑定。请稍后再试，或联系支持。";
const BIND_SUBSCRIBE_REQUIRED =
  "需要先完成安全提醒订阅，才能完成绑定。提醒不含对话内容。";
const BIND_SUBSCRIBE_REJECTED =
  "未完成安全提醒订阅，绑定未提交。请再次点击并选择允许。";

const SAFETY_ALERT_READ_STATUS = Object.freeze({
  OK: "ok",
  NOT_FOUND: "not_found",
  FAILED: "failed",
});

const SAFETY_ALERT_READ_ERROR_KINDS = Object.freeze({
  AUTH: "auth",
  NETWORK: "network",
  SERVER: "server",
});

function text(value) {
  return typeof value === "string" ? value : "";
}

const DELIVERY_STAGE_LABELS = {
  recorded: "已记录",
  queued: "排队中",
  channel_accepted: "渠道已接受",
  guardian_confirmed: "已查看",
};

const DELIVERY_FAIL_RESULT_LABELS = {
  no_subscription: "无订阅",
  failed: "发送失败",
  guardian_not_eligible: "发送失败",
};

function deliveryStageResult(receipt, currentStage) {
  const stages = Array.isArray(receipt?.stages) ? receipt.stages : [];
  for (let i = stages.length - 1; i >= 0; i -= 1) {
    const event = stages[i];
    if (event && event.stage === currentStage && typeof event.result === "string") {
      return event.result;
    }
  }
  return "";
}

function deliveryStageLabel(receipt) {
  if (!receipt || typeof receipt !== "object") return "";
  const stage = text(receipt.current_stage);
  if (!stage) return "";
  if (stage === "fail_or_auth_insufficient") {
    const result = deliveryStageResult(receipt, stage);
    return DELIVERY_FAIL_RESULT_LABELS[result] || DELIVERY_FAIL_RESULT_LABELS.failed;
  }
  if (stage === "queued" && deliveryStageResult(receipt, stage) === "pending_retry") {
    return "重试";
  }
  return DELIVERY_STAGE_LABELS[stage] || "";
}

function parseDeliveryReceipt(raw) {
  if (!raw || typeof raw !== "object") return null;
  const currentStage = text(raw.current_stage);
  if (!currentStage) return null;
  const receipt = {
    currentStage,
    channelAccepted: raw.channel_accepted === true,
    guardianConfirmed: raw.guardian_confirmed === true,
    attempts: Number.isInteger(raw.attempts) ? raw.attempts : 0,
    lastErrorCode: text(raw.last_error_code) || null,
    stages: Array.isArray(raw.stages) ? raw.stages : [],
  };
  const stageLabel = deliveryStageLabel(raw);
  if (stageLabel) receipt.stageLabel = stageLabel;
  return receipt;
}

function parseSafetyAlert(item) {
  if (!item || typeof item !== "object") return null;
  const notificationId = text(item.notification_id);
  if (!notificationId) return null;
  const alert = {
    notificationId,
    minorUserId: text(item.minor_user_id),
    minorDisplayName: text(item.minor_display_name) || "TA",
    occurredAt: text(item.occurred_at),
    occurredLabel: text(item.occurred_label),
    message: UNREAD_SAFETY_COPY,
    categoryLabel: text(item.category_label),
    summary: text(item.summary),
    robotSaid: text(item.robot_said),
    parentAction: text(item.parent_action),
  };
  if (typeof item.detail === "string" && item.detail) {
    alert.detail = item.detail;
  }
  const deliveryReceipt = parseDeliveryReceipt(item.delivery_receipt);
  if (deliveryReceipt) {
    alert.deliveryReceipt = deliveryReceipt;
    if (deliveryReceipt.stageLabel) alert.stageLabel = deliveryReceipt.stageLabel;
  }
  return alert;
}

function safetyAlertReadErrorKind(error) {
  const status = Number(error?.status || 0);
  if (status === 401 || status === 403) return SAFETY_ALERT_READ_ERROR_KINDS.AUTH;
  if (
    status === 0 &&
    /network|网络|offline|request:fail|timeout|连接/i.test(
      `${error?.message || ""} ${error?.errMsg || ""}`,
    )
  ) {
    return SAFETY_ALERT_READ_ERROR_KINDS.NETWORK;
  }
  return SAFETY_ALERT_READ_ERROR_KINDS.SERVER;
}

function safetyAlertReadFailure(error) {
  if (Number(error?.status || 0) === 404) {
    return { status: SAFETY_ALERT_READ_STATUS.NOT_FOUND };
  }
  return {
    status: SAFETY_ALERT_READ_STATUS.FAILED,
    errorKind: safetyAlertReadErrorKind(error),
  };
}

async function loadUnreadSafetyAlerts(api) {
  try {
    const payload = await api.getUnreadGuardianNotifications();
    const newest = parseSafetyAlert(payload?.newest);
    if (!newest) return { status: SAFETY_ALERT_READ_STATUS.NOT_FOUND };
    const others = payload.others_count;
    return {
      status: SAFETY_ALERT_READ_STATUS.OK,
      newest,
      othersCount: Number.isInteger(others) && others > 0 ? others : 0,
    };
  } catch (error) {
    return safetyAlertReadFailure(error);
  }
}

async function loadSafetyAlert(api, notificationId) {
  if (!notificationId) return { status: SAFETY_ALERT_READ_STATUS.NOT_FOUND };
  try {
    const alert = parseSafetyAlert(await api.getGuardianNotification(notificationId));
    return alert
      ? { status: SAFETY_ALERT_READ_STATUS.OK, alert }
      : { status: SAFETY_ALERT_READ_STATUS.NOT_FOUND };
  } catch (error) {
    return safetyAlertReadFailure(error);
  }
}

async function acknowledgeSafetyAlert(api, notificationId) {
  try {
    await api.markGuardianNotificationRead(notificationId);
    return "saved";
  } catch (error) {
    return error?.status === 404 ? "gone" : "failed";
  }
}

function moreUnreadLabel(othersCount) {
  return Number.isInteger(othersCount) && othersCount > 0 ? `还有 ${othersCount} 条` : "";
}

function attentionUrl(notificationId) {
  return `/pages/attention/index?id=${encodeURIComponent(notificationId || "")}`;
}

function shouldOfferCrisisSubscription(binding) {
  if (!binding || (binding.status && binding.status !== "active")) return false;
  return (
    binding.declared_mode === "parent_for_child" ||
    binding.declared_mode === "child_for_parent"
  );
}

function requiresCrisisSubscribeBeforeBind(declaredMode) {
  return declaredMode === "parent_for_child" || declaredMode === "child_for_parent";
}

/*
 * 绑定硬门禁。调用方必须在 tap 处理函数里、在任何 await 之前已经持有 config，
 * 并同步进入本函数，以便微信允许弹出订阅框。
 */
async function requireCrisisSubscribeBeforeBind({
  declaredMode,
  config,
  api,
  wxApi = globalThis.wx,
  subscriptionPushEnabled = SUBSCRIPTION_PUSH_ENABLED,
} = {}) {
  if (!subscriptionPushEnabled) {
    return { ok: true, kind: "paused" };
  }
  if (!requiresCrisisSubscribeBeforeBind(declaredMode)) {
    return { ok: true, kind: "not_required" };
  }
  if (!config?.enabled || !config.templateId) {
    return { ok: false, kind: "unavailable", message: BIND_SUBSCRIBE_UNAVAILABLE };
  }
  if (typeof wxApi?.requestSubscribeMessage !== "function") {
    return { ok: false, kind: "unavailable", message: BIND_SUBSCRIBE_UNAVAILABLE };
  }
  const outcome = await guardianPush.subscribeCrisisAlerts({ config, api, wxApi });
  if (outcome.result === "accept" && outcome.recorded) {
    return { ok: true, kind: "accepted", outcome };
  }
  if (!outcome.prompted) {
    return { ok: false, kind: "unavailable", message: BIND_SUBSCRIBE_REQUIRED, outcome };
  }
  return { ok: false, kind: "not_accepted", message: BIND_SUBSCRIBE_REJECTED, outcome };
}

module.exports = {
  BIND_SUBSCRIBE_REJECTED,
  BIND_SUBSCRIBE_REQUIRED,
  BIND_SUBSCRIBE_UNAVAILABLE,
  SAFETY_ALERT_READ_ERROR_KINDS,
  SAFETY_ALERT_READ_STATUS,
  acknowledgeSafetyAlert,
  attentionUrl,
  deliveryStageLabel,
  loadSafetyAlert,
  loadUnreadSafetyAlerts,
  moreUnreadLabel,
  parseDeliveryReceipt,
  parseSafetyAlert,
  requireCrisisSubscribeBeforeBind,
  requiresCrisisSubscribeBeforeBind,
  safetyAlertReadErrorKind,
  shouldOfferCrisisSubscription,
};
