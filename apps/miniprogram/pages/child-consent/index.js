const { statusBarStyle } = require("../../utils/nav-inset");
const { isTabRoute, routeOf } = require("../../utils/tab-routes");
const { openPrivacyContract } = require("../../utils/privacy-contract");
const legalRelease = require("../../utils/legal-release");
const { CONSENT_AGREEMENT_HINT, loadUserAgreement } = require("../../utils/legal-doc");
const { grantChildInfoConsent } = require("../../utils/child-consent");

const CONSENT_FACTS = [
  { icon: "server", title: "对话经过我们的服务器", subtitle: "孩子说的话会经服务器处理后再回复" },
  { icon: "note", title: "保存对话摘要", subtitle: "原话和录音只用来当场回应，不保存" },
  { icon: "star", title: "长期只存三项", subtitle: "孩子的称呼、喜好、宠物名" },
  { icon: "info", title: "回应由 AI 生成", subtitle: "会明确标识；模型训练默认关闭" },
  { icon: "cam", title: "设备没有摄像头", subtitle: "不拍照，也不录像" },
];

function agreementFields() {
  const locked = legalRelease.consentLocked(loadUserAgreement().pending, legalRelease.EFFECTIVE_ON);
  return {
    agreementPending: locked,
    agreementHint: locked ? CONSENT_AGREEMENT_HINT : "",
    releaseChip: legalRelease.releaseChip(),
  };
}

Page({
  data: {
    statusBarStyle: statusBarStyle(),
    consented: false,
    redirect: "",
    facts: CONSENT_FACTS,
    ...agreementFields(),
  },

  onLoad(options) {
    const redirect = typeof options.redirect === "string" ? options.redirect : "";
    this.setData({ redirect });
  },

  onShow() {
    const gate = agreementFields();
    this.setData(gate.agreementPending ? { ...gate, consented: false } : gate);
  },

  toggleConsent() {
    if (this.data.agreementPending) return;
    this.setData({ consented: !this.data.consented });
  },

  openPrivacy() {
    openPrivacyContract();
  },

  openGuardianConsent() {
    wx.navigateTo({ url: "/pages/guardian-consent/index" });
  },

  confirmChildConsent() {
    if (this.data.agreementPending || !this.data.consented) return;
    grantChildInfoConsent();
    const redirect = this.data.redirect;
    const pages = getCurrentPages();
    if (pages.length > 1) {
      wx.navigateBack();
      return;
    }
    if (redirect && isTabRoute(redirect)) {
      wx.switchTab({ url: routeOf(redirect) });
      return;
    }
    if (redirect.startsWith("/")) {
      wx.redirectTo({ url: redirect });
      return;
    }
    wx.switchTab({ url: "/pages/home/index" });
  },

  decline() {
    wx.switchTab({ url: "/pages/home/index" });
  },
});
