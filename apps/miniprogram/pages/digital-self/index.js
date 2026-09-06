const api = require("../../utils/api");
const { requireLogin } = require("../../utils/auth-gate");
const contracts = require("../../utils/multi-subject-contracts");
const { capabilityGateMessage } = require("../../utils/device-binding");

function dimensionsOf(result) {
  return Array.isArray(result?.dimensions) ? result.dimensions : [];
}

function versionsOf(result) {
  return Array.isArray(result?.items) ? result.items : [];
}

Page({
  data: {
    loading: false,
    error: "",
    persona: null,
    dimensions: [],
    versions: [],
    showEngineering: false,
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
        reason: "view_digital_self",
        redirect: "/pages/digital-self/index",
      }))
    ) return;
    const gate = await api.requireRuntimeCapability(contracts.Capability.DigitalSelfPreview);
    if (!gate.allowed) {
      this.setData({
        loading: false,
        error: capabilityGateMessage(gate, contracts.Capability.DigitalSelfPreview),
      });
      return;
    }
    this.loadOverview();
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
      error: "",
      persona: null,
      dimensions: [],
      versions: [],
      showEngineering: false,
    });
  },

  toggleEngineering() {
    this.setData({ showEngineering: !this.data.showEngineering });
  },

  onPullDownRefresh() {
    if (!api.hasAuthenticatedSession()) {
      wx.stopPullDownRefresh();
      return;
    }
    this.loadOverview().finally(() => wx.stopPullDownRefresh());
  },

  async loadOverview() {
    if (!api.hasAuthenticatedSession()) {
      this._clearPrivateState();
      return;
    }
    const gate = await api.requireRuntimeCapability(contracts.Capability.DigitalSelfPreview);
    if (!gate.allowed) {
      this.setData({
        loading: false,
        error: capabilityGateMessage(gate, contracts.Capability.DigitalSelfPreview),
      });
      return;
    }
    const authEpoch = api.currentAuthEpoch();
    this.setData({ loading: true, error: "" });
    const [growth, persona, versions] = await Promise.allSettled([
      api.getGrowthOverview(),
      api.getPersonaStatus(),
      api.getDigitalSelfVersions(),
    ]);
    if (!api.isAuthEpochCurrent(authEpoch)) return;
    const errors = [growth, persona, versions]
      .filter((result) => result.status === "rejected")
      .map((result) => result.reason?.message)
      .filter(Boolean);
    this.setData({
      dimensions: growth.status === "fulfilled" ? dimensionsOf(growth.value) : [],
      persona: persona.status === "fulfilled" ? persona.value : null,
      versions: versions.status === "fulfilled" ? versionsOf(versions.value) : [],
      error: errors[0] || "",
      loading: false,
    });
  },
});
