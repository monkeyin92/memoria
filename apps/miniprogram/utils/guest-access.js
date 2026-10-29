const ACCOUNT_CONSENT_PROMPT = "请先阅读并勾选《用户服务协议》和《隐私政策》";
const LOGIN_REQUIRED_COPY = "这个功能需要先登录，登录后才能绑定和管理你的设备";

const GUEST_HOME = Object.freeze({
  productTitle: "Memoria",
  productBody: "Memoria 是给 3 到 10 岁孩子的陪伴机器人。孩子在设备上使用，家长在手机上查看和管理。",
  features: Object.freeze([
    Object.freeze({ id: "chat", title: "陪聊", icon: "wave" }),
    Object.freeze({ id: "offline-stories", title: "离线故事", icon: "book" }),
    Object.freeze({ id: "time-limits", title: "每次和每天的时长上限", icon: "moon" }),
    Object.freeze({ id: "summaries", title: "家长能查看和删除聊天摘要", icon: "note" }),
  ]),
  agreements: Object.freeze([
    Object.freeze({ id: "terms", title: "用户服务协议", url: "/pages/user-agreement/index" }),
    Object.freeze({ id: "privacy", title: "隐私政策", url: "/pages/privacy-policy/index" }),
  ]),
});

const LOGIN_GATED_ACTIONS = Object.freeze([
  Object.freeze({ id: "bind_device", label: "绑定设备", reason: "bind_device" }),
  Object.freeze({ id: "chat_summaries", label: "聊天摘要", reason: "view_memory" }),
  Object.freeze({ id: "child_profile", label: "孩子资料", reason: "view_child_memory" }),
  Object.freeze({ id: "time_settings", label: "时长设置", reason: "manage_device" }),
  Object.freeze({ id: "pinned_alerts", label: "置顶提醒", reason: "view_attention" }),
  Object.freeze({ id: "help_feedback", label: "帮助与反馈", reason: "help_feedback" }),
]);

function guestAgreement(id) {
  return GUEST_HOME.agreements.find((item) => item.id === id) || null;
}

function loginGatedAction(id) {
  return LOGIN_GATED_ACTIONS.find((item) => item.id === id) || null;
}

module.exports = {
  ACCOUNT_CONSENT_PROMPT,
  LOGIN_REQUIRED_COPY,
  GUEST_HOME,
  LOGIN_GATED_ACTIONS,
  guestAgreement,
  loginGatedAction,
};
