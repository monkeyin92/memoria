/*
 * 危机安全提醒：置顶未送达告警。
 *
 * pinned 由服务端按 outbox 送达状态给出；客户端只过滤展示，不重新判定。
 * 订阅入口仍走 utils/guardian-push（一次性微信订阅消息）。
 */

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
        : "TA此刻可能需要可信任的人陪伴，请尽快联系并确认安全；紧急时联系当地急救或报警。",
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

module.exports = {
  loadPinnedCrisisAlerts,
  normalizeAlert,
  pinnedAlerts,
  shouldOfferCrisisSubscription,
};
