const api = require("../../utils/api");
const { requireLogin } = require("../../utils/auth-gate");

function today() {
  const date = new Date();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${date.getFullYear()}-${month}-${day}`;
}

function normalizeDay(item) {
  const summary = item.summary || {};
  return {
    date: item.day || item.date || "",
    count: item.message_count || 0,
    title: summary.title || item.title || "这一天的回顾",
    overview: summary.overview || item.overview || item.summary_text || "还没有生成摘要。",
    highlights: summary.highlights || item.highlights || [],
    suggestion: summary.suggestion || item.suggestion || "",
  };
}

Page({
  data: {
    days: [],
    loading: false,
    summarizing: false,
    error: "",
    selectedDate: today(),
    authenticated: false,
  },

  onLoad() {
    const app = getApp();
    if (typeof app?.subscribeAuthCleared === "function") {
      this._unsubscribeAuthCleared = app.subscribeAuthCleared(() => this._enterGuestState());
    }
  },

  onShow() {
    const authenticated = api.hasAuthenticatedSession();
    this.setData({ authenticated });
    if (!authenticated) {
      this._enterGuestState();
      return;
    }
    this.loadDays();
  },

  onUnload() {
    if (this._unsubscribeAuthCleared) {
      this._unsubscribeAuthCleared();
      this._unsubscribeAuthCleared = null;
    }
  },

  _enterGuestState() {
    this.setData({
      authenticated: false,
      days: [],
      loading: false,
      summarizing: false,
      error: "",
    });
  },

  onPullDownRefresh() {
    if (!api.hasAuthenticatedSession()) {
      wx.stopPullDownRefresh();
      return;
    }
    this.loadDays().finally(() => wx.stopPullDownRefresh());
  },

  async loadDays() {
    const identity = api.currentIdentity();
    if (!identity) return;
    const authEpoch = api.currentAuthEpoch();
    this.setData({ loading: true, error: "" });
    try {
      const result = await api.getMemoryDays(identity.user_id, 30);
      if (!api.isAuthEpochCurrent(authEpoch)) return;
      const days = (result.items || []).map(normalizeDay);
      this.setData({ days });
    } catch (error) {
      if (!api.isAuthEpochCurrent(authEpoch)) return;
      this.setData({ error: error?.message || "回顾暂时无法加载。" });
    } finally {
      if (api.isAuthEpochCurrent(authEpoch)) this.setData({ loading: false });
    }
  },

  chooseDay(event) {
    this.setData({ selectedDate: event.currentTarget.dataset.date });
  },

  async loginForReview() {
    if (!(await requireLogin({ reason: "view_memory" }))) return;
    this.setData({ authenticated: true });
    await this.loadDays();
  },

  async summarizeSelectedDay() {
    if (!(await requireLogin({ reason: "generate_review" }))) return;
    this.setData({ authenticated: true });
    const identity = api.currentIdentity();
    if (!identity || this.data.summarizing) return;
    const authEpoch = api.currentAuthEpoch();
    this.setData({ summarizing: true, error: "" });
    try {
      await api.summarizeDay(identity.user_id, this.data.selectedDate);
      if (!api.isAuthEpochCurrent(authEpoch)) return;
      await this.loadDays();
      if (!api.isAuthEpochCurrent(authEpoch)) return;
      wx.showToast({ title: "回顾已更新", icon: "success" });
    } catch (error) {
      if (!api.isAuthEpochCurrent(authEpoch)) return;
      this.setData({ error: error?.message || "生成回顾失败。" });
    } finally {
      if (api.isAuthEpochCurrent(authEpoch)) this.setData({ summarizing: false });
    }
  },
});
