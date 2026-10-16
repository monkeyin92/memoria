const api = require("../../utils/api");
const { statusBarStyle } = require("../../utils/nav-inset");
const { leavePage } = require("../../utils/nav-back");
const contracts = require("../../utils/multi-subject-contracts");
const {
  capabilityGateMessage,
  configActionGate,
  CONFIG_GATE_PENDING_MESSAGE,
} = require("../../utils/device-binding");

function grantedLabel(consent) {
  const at = new Date(typeof consent?.granted_at === "string" ? consent.granted_at : NaN);
  if (Number.isNaN(at.getTime())) return "已授权";
  return `${at.getFullYear()}年${at.getMonth() + 1}月${at.getDate()}日授权`;
}

Page({
  data: {
    statusBarStyle: statusBarStyle(),
    configOpen: false,
    configNote: CONFIG_GATE_PENDING_MESSAGE,
    loading: false,
    acting: false,
    error: "",
    consent: null,
    consentLabel: "",
    guest: false,
  },

  goBack() {
    leavePage("/pages/profile/index");
  },

  onLoad() {
    const app = getApp();
    if (typeof app?.subscribeAuthCleared === "function") {
      this._unsubscribeAuthCleared = app.subscribeAuthCleared(() => this._clearPrivateState());
    }
  },

  async onShow() {
    if (!api.hasAuthenticatedSession()) {
      this._showGuestNotice();
      return;
    }
    this.setData({ guest: false });
    await this.refreshConfigGate();
    this.loadConsent();
  },

  _showGuestNotice() {
    this.setData({
      guest: true,
      configOpen: false,
      configNote: "",
      loading: false,
      acting: false,
      error: "",
      consent: null,
      consentLabel: "",
    });
  },

  onUnload() {
    if (this._unsubscribeAuthCleared) {
      this._unsubscribeAuthCleared();
      this._unsubscribeAuthCleared = null;
    }
  },

  _clearPrivateState() {
    this._showGuestNotice();
  },

  async refreshConfigGate() {
    const seam = await configActionGate("raw_audio_consent");
    this.setData({
      configOpen: seam.allowed,
      configNote: seam.allowed ? "" : seam.message,
    });
    return seam;
  },

  _showConsent(consent) {
    this.setData({ consent: consent || null, consentLabel: consent ? grantedLabel(consent) : "" });
  },

  async loadConsent() {
    if (!api.hasAuthenticatedSession()) {
      this._clearPrivateState();
      return;
    }
    const gate = await api.requireRuntimeCapability(contracts.Capability.RawAudioRetention);
    if (!gate.allowed) {
      this.setData({
        loading: false,
        consent: null,
        consentLabel: "",
        error: capabilityGateMessage(gate, contracts.Capability.RawAudioRetention),
      });
      return;
    }
    const authEpoch = api.currentAuthEpoch();
    this.setData({ loading: true, error: "" });
    try {
      const result = await api.getRawVoiceConsent();
      if (!api.isAuthEpochCurrent(authEpoch)) return;
      this._showConsent(result?.consent);
    } catch (error) {
      if (!api.isAuthEpochCurrent(authEpoch)) return;
      this.setData({ error: error?.message || "授权状态无法加载。" });
    } finally {
      if (api.isAuthEpochCurrent(authEpoch)) this.setData({ loading: false });
    }
  },

  async grant() {
    const seam = await this.refreshConfigGate();
    if (!seam.allowed) {
      this.setData({ error: seam.message });
      return;
    }
    const confirmed = await new Promise((resolve) => {
      wx.showModal({
        title: "确认原始语音归档授权",
        content: "仅在你明确同意后，服务端才会按授权策略保存原始语音。你可以随时撤回。",
        success: (result) => resolve(result.confirm),
      });
    });
    if (!confirmed) return;
    const authEpoch = api.currentAuthEpoch();
    this.setData({ acting: true, error: "" });
    try {
      const consent = await api.grantRawVoiceConsent();
      if (!api.isAuthEpochCurrent(authEpoch)) return;
      this._showConsent(consent);
      wx.showToast({ title: "授权已记录", icon: "success" });
    } catch (error) {
      if (!api.isAuthEpochCurrent(authEpoch)) return;
      this.setData({ error: error?.message || "授权未完成。" });
    } finally {
      if (api.isAuthEpochCurrent(authEpoch)) this.setData({ acting: false });
    }
  },

  onRawAudioSwitch(event) {
    const on = !!(event && event.detail && event.detail.value);
    if (on && !this.data.consent) this.grant();
    if (!on && this.data.consent) this.revoke();
  },

  openChildMemory() {
    wx.navigateTo({ url: "/pages/child-memory/index" });
  },

  async revoke() {
    const seam = await this.refreshConfigGate();
    if (!seam.allowed) {
      this.setData({ error: seam.message });
      return;
    }
    const confirmed = await new Promise((resolve) => {
      wx.showModal({
        title: "撤回原始语音授权",
        content: "撤回会请求服务端删除受该授权保护的原始语音对象，过程可能需要重试。",
        confirmColor: "#9A4732",
        success: (result) => resolve(result.confirm),
      });
    });
    if (!confirmed) return;
    const authEpoch = api.currentAuthEpoch();
    this.setData({ acting: true, error: "" });
    try {
      await api.revokeRawVoiceConsent();
      if (!api.isAuthEpochCurrent(authEpoch)) return;
      this._showConsent(null);
      wx.showToast({ title: "已撤回授权", icon: "success" });
    } catch (error) {
      if (!api.isAuthEpochCurrent(authEpoch)) return;
      this.setData({ error: error?.message || "撤回未完成，请稍后重试。" });
    } finally {
      if (api.isAuthEpochCurrent(authEpoch)) this.setData({ acting: false });
    }
  },
});
