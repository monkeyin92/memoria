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

function text(value) {
  return typeof value === "string" ? value : "";
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
  return alert;
}

async function loadUnreadSafetyAlerts(api) {
  try {
    const payload = await api.getUnreadGuardianNotifications();
    const newest = parseSafetyAlert(payload?.newest);
    if (!newest) return null;
    const others = payload.others_count;
    return { newest, othersCount: Number.isInteger(others) && others > 0 ? others : 0 };
  } catch (_error) {
    return null;
  }
}

async function loadSafetyAlert(api, notificationId) {
  if (!notificationId) return null;
  try {
    return parseSafetyAlert(await api.getGuardianNotification(notificationId));
  } catch (_error) {
    return null;
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
  acknowledgeSafetyAlert,
  attentionUrl,
  loadSafetyAlert,
  loadUnreadSafetyAlerts,
  moreUnreadLabel,
  parseSafetyAlert,
  requireCrisisSubscribeBeforeBind,
  requiresCrisisSubscribeBeforeBind,
  shouldOfferCrisisSubscription,
};
