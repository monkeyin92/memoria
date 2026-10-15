const api = require("../../utils/api");
const { statusBarStyle } = require("../../utils/nav-inset");
const { leavePage } = require("../../utils/nav-back");
const { isTabRoute, routeOf } = require("../../utils/tab-routes");
const { openPrivacyContract } = require("../../utils/privacy-contract");

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
  }[reason] || "登录后即可继续刚才的操作。";
}

Page({
  data: {
    statusBarStyle: statusBarStyle(),
    step: "consent",
    nickname: "",
    avatarPath: "",
    consented: false,
    loading: false,
    error: "",
    redirect: "/pages/home/index",
    reasonHint: "",
    needsSalutation: false,
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
      needsSalutation: options.skip_restore === "1",
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
      if (error?.code === "phone_authorization_required") {
        this.setData({ needsSalutation: true });
      } else {
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

  toggleConsent() {
    this.setData({ consented: !this.data.consented, error: "" });
  },

  backToConsent() {
    this._phoneCode = "";
    this.setData({ step: "consent", error: "" });
  },

  openPrivacy() {
    openPrivacyContract();
  },

  openGuardianConsent() {
    wx.navigateTo({ url: "/pages/guardian-consent/index" });
  },

  async onGetPhoneNumber(event) {
    if (!this.data.consented) {
      this.setData({ error: "请先阅读并同意《隐私保护指引（草稿）》和《监护人同意书（草稿）》。" });
      return;
    }
    const phoneCode = event.detail?.code;
    if (!phoneCode) {
      this.setData({ error: "需要手机号授权才能完成首次登录。" });
      return;
    }
    if (this.data.needsSalutation) {
      this._phoneCode = phoneCode;
      this.setData({ step: "profile", error: "" });
      return;
    }
    await this.submitLogin(phoneCode);
  },

  async submitProfile() {
    if (!this.data.nickname.trim()) {
      this.setData({ error: "先告诉我怎么称呼你吧。" });
      return;
    }
    await this.submitLogin(this._phoneCode);
  },

  async submitLogin(phoneCode) {
    this.setData({ loading: true, error: "" });
    try {
      await api.loginWithWechat({
        phoneCode,
        displayName: this.data.nickname.trim(),
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
      // WeChat phone codes are single use, so a failed login needs a fresh authorization.
      this._phoneCode = "";
      this.setData({ step: "consent", error: error?.message || "微信登录未完成，请稍后重试。" });
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
