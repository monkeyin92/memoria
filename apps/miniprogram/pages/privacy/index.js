const api = require("../../utils/api");

Page({
  data: {
    loading: false,
    acting: false,
    error: "",
    consent: null,
  },

  onShow() {
    if (!api.currentAccessToken()) {
      wx.redirectTo({ url: "/pages/auth/index?mode=login" });
      return;
    }
    this.loadConsent();
  },

  async loadConsent() {
    this.setData({ loading: true, error: "" });
    try {
      const result = await api.getRawVoiceConsent();
      this.setData({ consent: result.consent || null });
    } catch (error) {
      this.setData({ error: error?.message || "授权状态无法加载。" });
    } finally {
      this.setData({ loading: false });
    }
  },

  async grant() {
    const confirmed = await new Promise((resolve) => {
      wx.showModal({
        title: "确认原始语音归档授权",
        content: "仅在你明确同意后，服务端才会按授权策略保存原始语音。你可以随时撤回。",
        success: (result) => resolve(result.confirm),
      });
    });
    if (!confirmed) return;
    this.setData({ acting: true, error: "" });
    try {
      const consent = await api.grantRawVoiceConsent();
      this.setData({ consent });
      wx.showToast({ title: "授权已记录", icon: "success" });
    } catch (error) {
      this.setData({ error: error?.message || "授权未完成。" });
    } finally {
      this.setData({ acting: false });
    }
  },

  async revoke() {
    const confirmed = await new Promise((resolve) => {
      wx.showModal({
        title: "撤回原始语音授权",
        content: "撤回会请求服务端删除受该授权保护的原始语音对象，过程可能需要重试。",
        confirmColor: "#ba4255",
        success: (result) => resolve(result.confirm),
      });
    });
    if (!confirmed) return;
    this.setData({ acting: true, error: "" });
    try {
      await api.revokeRawVoiceConsent();
      this.setData({ consent: null });
      wx.showToast({ title: "已撤回授权", icon: "success" });
    } catch (error) {
      this.setData({ error: error?.message || "撤回未完成，请稍后重试。" });
    } finally {
      this.setData({ acting: false });
    }
  },
});
