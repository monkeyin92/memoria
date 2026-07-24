const api = require("../../utils/api");

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
  },

  onShow() {
    if (!api.currentAccessToken()) {
      wx.navigateTo({ url: "/pages/auth/index?mode=login" });
      return;
    }
    this.loadDays();
  },

  onPullDownRefresh() {
    this.loadDays().finally(() => wx.stopPullDownRefresh());
  },

  async loadDays() {
    const identity = api.currentIdentity();
    if (!identity) return;
    this.setData({ loading: true, error: "" });
    try {
      const result = await api.getMemoryDays(identity.user_id, 30);
      const days = (result.items || []).map(normalizeDay);
      this.setData({ days });
    } catch (error) {
      this.setData({ error: error?.message || "回顾暂时无法加载。" });
    } finally {
      this.setData({ loading: false });
    }
  },

  chooseDay(event) {
    this.setData({ selectedDate: event.currentTarget.dataset.date });
  },

  async summarizeSelectedDay() {
    const identity = api.currentIdentity();
    if (!identity || this.data.summarizing) return;
    this.setData({ summarizing: true, error: "" });
    try {
      await api.summarizeDay(identity.user_id, this.data.selectedDate);
      await this.loadDays();
      wx.showToast({ title: "回顾已更新", icon: "success" });
    } catch (error) {
      this.setData({ error: error?.message || "生成回顾失败。" });
    } finally {
      this.setData({ summarizing: false });
    }
  },
});
