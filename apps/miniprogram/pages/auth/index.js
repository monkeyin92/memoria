const api = require("../../utils/api");

const TAB_ROUTES = new Set([
  "/pages/home/index",
  "/pages/memory/index",
  "/pages/profile/index",
]);

function reasonText(reason) {
  return {
    start_voice: "登录后即可开始语音陪伴。",
    generate_review: "登录后才能生成并查看你的专属回顾。",
    edit_profile: "登录后才能保存你的资料与陪伴偏好。",
    view_memory: "登录后即可查看你的专属回顾。",
    view_profile: "登录后即可管理你的资料与陪伴偏好。",
    view_digital_self: "登录后即可查看你的数字分身成长状态。",
    manage_privacy: "登录后即可查看和管理你的语音授权。",
  }[reason] || "登录后即可继续刚才的操作。";
}

Page({
  data: {
    nickname: "",
    avatarPath: "",
    consented: false,
    loading: false,
    error: "",
    redirect: "/pages/home/index",
    reasonHint: "",
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
    this._restoreAttempted = options.skip_restore === "1";
  },

  onShow() {
    if (api.hasAuthenticatedSession()) {
      this.finishLogin();
      return;
    }
    if (this._restoreAttempted) return;
    this._restoreAttempted = true;
    this.trySilentRestore();
  },

  async trySilentRestore() {
    try {
      await api.restoreWechatIdentity();
      this.finishLogin();
    } catch (error) {
      if (error?.code !== "phone_authorization_required") {
        this.setData({ error: error?.message || "暂时无法恢复微信登录。" });
      }
    }
  },

  onNicknameInput(event) {
    this.setData({ nickname: event.detail.value, error: "" });
  },

  onChooseAvatar(event) {
    const avatarPath = event.detail?.avatarUrl || "";
    if (avatarPath) this.setData({ avatarPath, error: "" });
  },

  onConsentChange(event) {
    this.setData({
      consented: (event.detail.value || []).includes("accepted"),
      error: "",
    });
  },

  openPrivacy() {
    if (typeof wx.openPrivacyContract === "function") {
      wx.openPrivacyContract({
        fail: () => this.showPrivacySummary(),
      });
      return;
    }
    this.showPrivacySummary();
  },

  showPrivacySummary() {
    wx.showModal({
      title: "隐私说明",
      content:
        "首次登录仅使用微信提供的登录凭证、手机号授权结果，以及你主动选择的昵称和头像来建立账号。未登录浏览不会读取个人回顾或资料。",
      showCancel: false,
      confirmText: "知道了",
    });
  },

  async onGetPhoneNumber(event) {
    if (!this.data.consented) {
      this.setData({ error: "请先阅读并同意《隐私保护指引》。" });
      return;
    }
    const phoneCode = event.detail?.code;
    if (!phoneCode) {
      this.setData({ error: "需要手机号授权才能完成首次登录。" });
      return;
    }
    this.setData({ loading: true, error: "" });
    try {
      await api.loginWithWechat({
        phoneCode,
        displayName: this.data.nickname,
      });
      if (this.data.avatarPath) {
        try {
          await api.uploadWechatAvatar(this.data.avatarPath);
        } catch {
          wx.showToast({ title: "已登录，头像稍后可再设置", icon: "none" });
        }
      }
      this.finishLogin();
    } catch (error) {
      this.setData({ error: error?.message || "微信登录未完成，请稍后重试。" });
    } finally {
      this.setData({ loading: false });
    }
  },

  finishLogin() {
    const redirect = this.data.redirect || "/pages/home/index";
    const route = redirect.split("?", 1)[0];
    if (TAB_ROUTES.has(route)) {
      wx.switchTab({ url: route });
      return;
    }
    wx.redirectTo({ url: redirect });
  },
});
