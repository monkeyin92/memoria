const api = require("../../utils/api");
const { statusBarStyle } = require("../../utils/nav-inset");
const { leavePage } = require("../../utils/nav-back");
const { isTabRoute, routeOf } = require("../../utils/tab-routes");
const { openPrivacyContract } = require("../../utils/privacy-contract");
const legalRelease = require("../../utils/legal-release");
const { CONSENT_AGREEMENT_HINT, loadUserAgreement } = require("../../utils/legal-doc");

const CONSENT_FACTS = [
  { icon: "server", title: "对话经过我们的服务器", subtitle: "孩子说的话会经服务器处理后再回复" },
  { icon: "note", title: "保存对话摘要", subtitle: "原话和录音只用来当场回应，不保存" },
  { icon: "star", title: "长期只存三项", subtitle: "孩子的称呼、喜好、宠物名" },
  { icon: "info", title: "回应由 AI 生成", subtitle: "会明确标识；模型训练默认关闭" },
  { icon: "cam", title: "设备没有摄像头", subtitle: "不拍照，也不录像" },
];

const CONSENT_REQUIRED = "请先阅读并同意《隐私保护指引》和《监护人同意书》。";

function agreementFields() {
  const locked = legalRelease.consentLocked(loadUserAgreement().pending, legalRelease.EFFECTIVE_ON);
  return {
    agreementPending: locked,
    agreementHint: locked ? CONSENT_AGREEMENT_HINT : "",
    releaseChip: legalRelease.releaseChip(),
  };
}

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
    facts: CONSENT_FACTS,
    ...agreementFields(),
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
    const gate = agreementFields();
    this.setData(gate.agreementPending ? { ...gate, consented: false } : gate);
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
    if (this.data.agreementPending) return;
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
      this.setData({ error: CONSENT_REQUIRED });
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
