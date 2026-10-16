const api = require("../../utils/api");
const { statusBarStyle } = require("../../utils/nav-inset");
const { leavePage } = require("../../utils/nav-back");
const { isTabRoute, routeOf } = require("../../utils/tab-routes");
const { ACCOUNT_CONSENT_PROMPT } = require("../../utils/guest-access");

function reasonText(reason) {
  return {
    view_dashboard: "登录后即可查看设备和今天记下的内容。",
    generate_review: "登录后才能生成并查看你的专属回顾。",
    edit_profile: "登录后才能保存你的资料与陪伴偏好。",
    view_memory: "登录后即可查看你的专属回顾。",
    view_profile: "登录后即可管理你的资料与陪伴偏好。",
    view_digital_self: "登录后即可查看你的记忆档案。",
    manage_privacy: "登录后即可查看和管理你的语音授权。",
    scan_device: "已识别到你的机器人。请先用微信登录，登录后会自动回到配网，继续连接和绑定。",
    manage_device: "登录后即可添加和管理你的机器人。",
    bind_device: "登录后继续绑定这台机器人。",
    view_child_memory: "登录后即可查看孩子的资料和聊天摘要。",
    view_attention: "登录后即可查看置顶提醒。",
    help_feedback: "登录后即可使用帮助与反馈。",
  }[reason] || "登录后即可继续刚才的操作。";
}

Page({
  data: {
    statusBarStyle: statusBarStyle(),
    accountAgreed: false,
    loading: false,
    error: "",
    redirect: "/pages/home/index",
    reasonHint: "",
  },

  goBack() {
    leavePage("/pages/home/index");
  },

  onLoad(options) {
    const redirect =
      typeof options.redirect === "string" && options.redirect.startsWith("/")
        ? options.redirect
        : "/pages/home/index";
    this.setData({
      redirect,
      reasonHint: reasonText(options.reason),
    });
  },

  onShow() {
    if (api.hasAuthenticatedSession()) this.finishLogin();
  },

  toggleAccountConsent() {
    this.setData({ accountAgreed: !this.data.accountAgreed, error: "" });
  },

  openUserAgreement() {
    wx.navigateTo({ url: "/pages/user-agreement/index" });
  },

  openPrivacyPolicy() {
    wx.navigateTo({ url: "/pages/privacy-policy/index" });
  },

  onLoginWithoutConsent() {
    this.setData({ error: ACCOUNT_CONSENT_PROMPT });
    wx.showToast({ title: ACCOUNT_CONSENT_PROMPT, icon: "none" });
  },

  async onGetPhoneNumber(event) {
    if (!this.data.accountAgreed) {
      this.onLoginWithoutConsent();
      return;
    }
    const phoneCode = event.detail?.code;
    if (!phoneCode) {
      this.setData({ error: "需要手机号授权才能完成登录。" });
      return;
    }
    this.setData({ loading: true, error: "" });
    try {
      await api.loginWithWechat({ phoneCode });
      this.finishLogin();
    } catch (error) {
      this.setData({ error: error?.message || "微信登录未完成，请稍后重试。" });
    } finally {
      this.setData({ loading: false });
    }
  },

  finishLogin() {
    const redirect = this.data.redirect || "/pages/home/index";
    if (isTabRoute(redirect)) {
      wx.switchTab({ url: routeOf(redirect) });
      return;
    }
    wx.redirectTo({ url: redirect });
  },
});
