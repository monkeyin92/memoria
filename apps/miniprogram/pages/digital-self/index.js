const api = require("../../utils/api");

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
  },

  onShow() {
    if (!api.currentAccessToken()) {
      wx.redirectTo({ url: "/pages/auth/index?mode=login" });
      return;
    }
    this.loadOverview();
  },

  onPullDownRefresh() {
    this.loadOverview().finally(() => wx.stopPullDownRefresh());
  },

  async loadOverview() {
    this.setData({ loading: true, error: "" });
    const [growth, persona, versions] = await Promise.allSettled([
      api.getGrowthOverview(),
      api.getPersonaStatus(),
      api.getDigitalSelfVersions(),
    ]);
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
