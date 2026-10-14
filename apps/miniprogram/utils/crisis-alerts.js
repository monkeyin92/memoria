/*
 * 危机安全提醒：置顶未送达告警，并在绑定前提交一次性订阅（硬门禁）。
 *
 * pinned 由服务端按 outbox 送达状态给出；客户端只过滤展示。
 * 绑定门禁必须在用户 tap 的同步调用栈里调起 requestSubscribeMessage，
 * 因此配置要在进入确认页时预加载，提交时不能先 await 配置。
 */

const guardianPush = require("./guardian-push");

const BIND_SUBSCRIBE_UNAVAILABLE =
  "订阅安全提醒尚未开通，暂时不能完成这类绑定。请稍后再试，或联系支持。";
const BIND_SUBSCRIBE_REQUIRED =
  "需要先完成订阅安全提醒，才能完成绑定。提醒不含对话内容。";
const BIND_SUBSCRIBE_REJECTED =
  "未完成订阅安全提醒，绑定未提交。请再次点击并选择允许。";

function normalizeAlert(item) {
  if (!item || typeof item !== "object") return null;
  const notificationId = typeof item.notification_id === "string" ? item.notification_id : "";
  if (!notificationId) return null;
  return {
    notificationId,
    minorUserId: typeof item.minor_user_id === "string" ? item.minor_user_id : "",
    minorDisplayName:
      typeof item.minor_display_name === "string" && item.minor_display_name
        ? item.minor_display_name
        : "TA",
    occurredAt: typeof item.occurred_at === "string" ? item.occurred_at : "",
    deliveryStatus: typeof item.delivery_status === "string" ? item.delivery_status : "",
    pinned: item.pinned === true,
    message:
      typeof item.message === "string" && item.message
        ? item.message
        : "有一条需要你关注的对话",
  };
}

function pinnedAlerts(items) {
  return (Array.isArray(items) ? items : [])
    .map(normalizeAlert)
    .filter((item) => item && item.pinned);
}

async function loadPinnedCrisisAlerts(api) {
  try {
    const payload = await api.getGuardianNotifications();
    return pinnedAlerts(payload?.items);
  } catch (_error) {
    return [];
  }
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
} = {}) {
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
  loadPinnedCrisisAlerts,
  normalizeAlert,
  pinnedAlerts,
  requireCrisisSubscribeBeforeBind,
  requiresCrisisSubscribeBeforeBind,
  shouldOfferCrisisSubscription,
};
