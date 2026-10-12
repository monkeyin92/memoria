const api = require("../../utils/api");
const { requireLogin } = require("../../utils/auth-gate");
const contracts = require("../../utils/multi-subject-contracts");
const {
  capabilityGateMessage,
  configActionGate,
  CONFIG_GATE_PENDING_MESSAGE,
} = require("../../utils/device-binding");
const {
  shouldRevokeVoiceConsent,
  voiceSamplesToForget,
} = require("../../utils/voice-enrollment-state");

Page({
  data: {
    configOpen: false,
    configNote: CONFIG_GATE_PENDING_MESSAGE,
    loading: false,
    acting: false,
    error: "",
    consent: null,
    memoryOn: true,
    voiceOn: true,
    voiceConfigOpen: false,
    voiceConfigNote: CONFIG_GATE_PENDING_MESSAGE,
    voiceActing: false,
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
    await this.refreshConfigGate();
    await this.refreshVoiceGate();
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
      configOpen: false,
      configNote: CONFIG_GATE_PENDING_MESSAGE,
      loading: false,
      acting: false,
      error: "",
      consent: null,
      voiceConfigOpen: false,
      voiceConfigNote: CONFIG_GATE_PENDING_MESSAGE,
      voiceActing: false,
    });
  },

  async refreshConfigGate() {
    const seam = await configActionGate("raw_audio_consent");
    this.setData({
      configOpen: seam.allowed,
      configNote: seam.allowed ? "" : seam.message,
    });
    return seam;
  },

  async refreshVoiceGate() {
    const seam = await configActionGate("voice_sample_revoke");
    this.setData({
      voiceConfigOpen: seam.allowed,
      voiceConfigNote: seam.allowed ? "" : seam.message,
    });
    return seam;
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
      this.setData({ consent });
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

  onMemorySwitch(event) {
    this.setData({ memoryOn: !!(event && event.detail && event.detail.value) });
  },

  onVoiceSwitch(event) {
    this.setData({ voiceOn: !!(event && event.detail && event.detail.value) });
  },

  revokeMemoryTip() {
    wx.showModal({
      title: "撤回记忆档案授权",
      content: "撤回后将删除已归档的记忆内容，删除范围与完成时间会以书面回执告知。",
      showCancel: false,
      confirmText: "知道了",
    });
  },

  async revokeVoiceTip() {
    if (!api.hasAuthenticatedSession()) {
      this.setData({
        voiceConfigOpen: false,
        voiceConfigNote: "请先登录，再撤回声音样本。",
        error: "请先登录，再撤回声音样本。",
      });
      return;
    }
    const seam = await this.refreshVoiceGate();
    if (!seam.allowed) {
      this.setData({ error: seam.message });
      return;
    }
    const confirmed = await new Promise((resolve) => {
      wx.showModal({
        title: "撤回声音样本授权",
        content: "撤回会删除已保存的声音样本和克隆音色。删除没完成时会留在这一页，不会显示成已经删掉。",
        confirmColor: "#ba4255",
        success: (result) => resolve(!!result.confirm),
      });
    });
    if (!confirmed) return;
    if (!api.hasAuthenticatedSession()) {
      this.setData({
        voiceConfigOpen: false,
        error: "请先登录，再撤回声音样本。",
      });
      return;
    }
    const again = await this.refreshVoiceGate();
    if (!again.allowed) {
      this.setData({ error: again.message });
      return;
    }
    const authEpoch = api.currentAuthEpoch();
    this.setData({ voiceActing: true, error: "" });
    try {
      const listing = await api.listVoiceProfiles();
      if (!api.isAuthEpochCurrent(authEpoch)) return;
      const targets = voiceSamplesToForget(listing?.items);
      for (const item of targets) {
        await api.revokeVoiceProfile(item.profile_id);
        if (!api.isAuthEpochCurrent(authEpoch)) return;
      }
      if (shouldRevokeVoiceConsent(listing, targets)) {
        await api.revokeVoiceCloneConsent();
        if (!api.isAuthEpochCurrent(authEpoch)) return;
      }
      this.setData({ voiceOn: false });
      if (targets.length === 0 && !shouldRevokeVoiceConsent(listing, targets)) {
        wx.showToast({ title: "没有可撤回的声音样本", icon: "none" });
        return;
      }
      wx.showToast({ title: "声音样本已撤回", icon: "success" });
    } catch (error) {
      if (!api.isAuthEpochCurrent(authEpoch)) return;
      this.setData({ error: error?.message || "声音样本撤回未完成，请稍后重试。" });
    } finally {
      if (api.isAuthEpochCurrent(authEpoch)) this.setData({ voiceActing: false });
    }
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
