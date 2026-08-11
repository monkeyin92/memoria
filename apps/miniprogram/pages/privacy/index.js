const api = require("../../utils/api");
const { requireLogin } = require("../../utils/auth-gate");
const contracts = require("../../utils/multi-subject-contracts");
const { capabilityGateMessage, configActionGate } = require("../../utils/device-binding");

Page({
  data: {
    loading: false,
    acting: false,
    error: "",
    consent: null,
  },

  onLoad() {
    const app = getApp();
    if (typeof app?.subscribeAuthCleared === "function") {
      this._unsubscribeAuthCleared = app.subscribeAuthCleared(() => this._clearPrivateState());
    }
  },

  async onShow() {
    if (
      !(await requireLogin({
        reason: "manage_privacy",
        redirect: "/pages/privacy/index",
      }))
    ) return;
    this.loadConsent();
  },

  onUnload() {
    if (this._unsubscribeAuthCleared) {
      this._unsubscribeAuthCleared();
      this._unsubscribeAuthCleared = null;
    }
  },

  _clearPrivateState() {
    this.setData({
      loading: false,
      acting: false,
      error: "",
      consent: null,
    });
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
        error: capabilityGateMessage(gate, contracts.Capability.RawAudioRetention),
      });
      return;
    }
    const authEpoch = api.currentAuthEpoch();
    this.setData({ loading: true, error: "" });
    try {
      const result = await api.getRawVoiceConsent();
      if (!api.isAuthEpochCurrent(authEpoch)) return;
      this.setData({ consent: result.consent || null });
    } catch (error) {
      if (!api.isAuthEpochCurrent(authEpoch)) return;
      this.setData({ error: error?.message || "授权状态无法加载。" });
    } finally {
      if (api.isAuthEpochCurrent(authEpoch)) this.setData({ loading: false });
    }
  },

  async grant() {
    // 配置动作：consent 决策接口接入前由 config seam fail-closed（P1 边界）。
    const seam = configActionGate("raw_audio_consent");
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
      this.setData({ consent });
      wx.showToast({ title: "授权已记录", icon: "success" });
    } catch (error) {
      if (!api.isAuthEpochCurrent(authEpoch)) return;
      this.setData({ error: error?.message || "授权未完成。" });
    } finally {
      if (api.isAuthEpochCurrent(authEpoch)) this.setData({ acting: false });
    }
  },

  async revoke() {
    // 配置动作：同上，fail-closed 且不按本地年龄放开。
    const seam = configActionGate("raw_audio_consent");
    if (!seam.allowed) {
      this.setData({ error: seam.message });
      return;
    }
    const confirmed = await new Promise((resolve) => {
      wx.showModal({
        title: "撤回原始语音授权",
        content: "撤回会请求服务端删除受该授权保护的原始语音对象，过程可能需要重试。",
        confirmColor: "#ba4255",
        success: (result) => resolve(result.confirm),
      });
    });
    if (!confirmed) return;
    const authEpoch = api.currentAuthEpoch();
    this.setData({ acting: true, error: "" });
    try {
      await api.revokeRawVoiceConsent();
      if (!api.isAuthEpochCurrent(authEpoch)) return;
      this.setData({ consent: null });
      wx.showToast({ title: "已撤回授权", icon: "success" });
    } catch (error) {
      if (!api.isAuthEpochCurrent(authEpoch)) return;
      this.setData({ error: error?.message || "撤回未完成，请稍后重试。" });
    } finally {
      if (api.isAuthEpochCurrent(authEpoch)) this.setData({ acting: false });
    }
  },
});
