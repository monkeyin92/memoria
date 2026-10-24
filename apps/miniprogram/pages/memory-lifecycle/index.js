const api = require("../../utils/api");
const { guardianSubjectFromBinding } = require("../../utils/guardian");
const { formatBeijingTime } = require("../../utils/memory-lifecycle");

const MEMORY_REASON_TEXT = Object.freeze({
  pending_confirmation: "确认后才会加入记忆档案。",
  conflicting_values: "和之前记下的内容不一致，请确认以哪个为准。",
});

const MEMORY_STATUS_LABELS = Object.freeze({
  retracted: "已撤回",
});

function normalizeCandidate(item) {
  const claimId = typeof item?.claim_id === "string" ? item.claim_id : "";
  if (!claimId) return null;
  return {
    id: claimId,
    kind: "candidate",
    title: item.value || "待确认的记忆",
    body: MEMORY_REASON_TEXT[item.reason] || "确认后才会加入记忆档案。",
    status: item.status || "candidate",
    statusLabel: "待确认",
    occurredAtLabel: "",
  };
}

function normalizeConfirmed(item) {
  const memoryId = typeof item?.memory_id === "string" ? item.memory_id : "";
  if (!memoryId) return null;
  return {
    id: memoryId,
    kind: "confirmed",
    title: item.title || item.snippet || "已记住的事",
    body: item.snippet || "",
    status: item.status || "confirmed",
    statusLabel: "已记住",
    occurredAtLabel: formatBeijingTime(item.occurred_at),
  };
}

function normalizeRetracted(item) {
  const claimId = typeof item?.claim_id === "string" ? item.claim_id : "";
  if (!claimId) return null;
  const title = item.title || item.value || "已撤回的记忆";
  const value = typeof item.value === "string" ? item.value : "";
  return {
    id: claimId,
    kind: "retracted",
    title,
    body: value && value !== title ? value : "",
    status: "retracted",
    statusLabel: "已撤回",
    retractedAtLabel: formatBeijingTime(item.retracted_at),
  };
}

function friendlyError(error) {
  const status = Number(error?.status || 0);
  const code = error?.detail?.code || error?.code || "";
  if (status === 401) return "登录状态已失效，请重新登录后再试。";
  if (code === "guardian_consent_required") {
    return "孩子的长期记忆还没有开启，暂时不能管理。可以在「成长小结与监护授权」里开启。";
  }
  if (
    status === 403 ||
    code === "guardian_memory_review_unavailable" ||
    code === "guardian_link_required"
  ) {
    return "这个孩子的记忆暂时不能在这里管理。请确认当前账号是绑定设备的监护人。";
  }
  const message = typeof error?.message === "string" ? error.message : "";
  if (message && !message.startsWith("request:fail")) return message;
  return "记忆列表暂时无法加载，请稍后重试。";
}

Page({
  data: {
    minorUserId: "",
    guardianMode: false,
    loading: false,
    error: "",
    unavailable: "",
    pendingItems: [],
    confirmedItems: [],
    retractedItems: [],
    reviewingId: "",
    isEmpty: false,
  },

  onLoad() {
    const app = getApp();
    if (app && typeof app.subscribeAuthCleared === "function") {
      this._unsubscribeAuthCleared = app.subscribeAuthCleared(() => this.loadMemories());
    }
  },

  onShow() {
    this.loadMemories();
  },

  onUnload() {
    this._loadSeq = (this._loadSeq || 0) + 1;
    if (this._unsubscribeAuthCleared) {
      this._unsubscribeAuthCleared();
      this._unsubscribeAuthCleared = null;
    }
  },

  _activeItems() {
    return this.data.pendingItems.concat(this.data.confirmedItems);
  },

  _clearActive(message) {
    this.setData({
      pendingItems: [],
      confirmedItems: [],
      retractedItems: [],
      reviewingId: "",
      isEmpty: false,
      error: "",
      unavailable: message,
    });
  },

  async loadMemories() {
    const seq = (this._loadSeq = (this._loadSeq || 0) + 1);
    const minorUserId = guardianSubjectFromBinding(api.readBindingManifest?.() || null);
    if (!minorUserId) {
      this.setData({
        minorUserId: "",
        guardianMode: false,
        loading: false,
      });
      this._clearActive("绑定设备后可以在这里管理孩子的记忆");
      return "unavailable";
    }
    if (!api.hasAuthenticatedSession()) {
      this.setData({
        minorUserId,
        guardianMode: true,
        loading: false,
      });
      this._clearActive("登录后可以在这里管理孩子的记忆。");
      return "unavailable";
    }

    const authEpoch = api.currentAuthEpoch();
    this.setData({
      minorUserId,
      guardianMode: true,
      loading: true,
      error: "",
      unavailable: "",
    });
    try {
      const [payload, retractedPayload] = await Promise.all([
        api.getGuardianChildMemories(minorUserId, 50),
        api.getGuardianChildRetractedMemories(minorUserId, 20),
      ]);
      if (seq !== this._loadSeq || !api.isAuthEpochCurrent(authEpoch)) return "stale";
      const retractedItems = (retractedPayload?.items || [])
        .map(normalizeRetracted)
        .filter(Boolean);
      /*
       * The retracted endpoint is the source of truth across page loads, so a
       * claim still listed as active is filtered out until the next read.
       */
      const retractedIds = new Set(retractedItems.map((item) => item.id));
      const pendingItems = (payload?.candidates || [])
        .map(normalizeCandidate)
        .filter(Boolean)
        .filter((item) => !retractedIds.has(item.id));
      const confirmedItems = (payload?.confirmed || [])
        .map(normalizeConfirmed)
        .filter(Boolean)
        .filter((item) => !retractedIds.has(item.id));
      this.setData({
        pendingItems,
        confirmedItems,
        retractedItems,
        loading: false,
        isEmpty:
          pendingItems.length === 0 &&
          confirmedItems.length === 0 &&
          retractedItems.length === 0,
      });
      return "ok";
    } catch (error) {
      if (seq !== this._loadSeq || !api.isAuthEpochCurrent(authEpoch)) return "stale";
      this.setData({
        pendingItems: [],
        confirmedItems: [],
        retractedItems: [],
        loading: false,
        isEmpty: false,
        error: friendlyError(error),
      });
      return "error";
    }
  },

  async confirmMemory(event) {
    const dataset = event?.currentTarget?.dataset || {};
    const id = typeof dataset.id === "string" ? dataset.id : "";
    if (!this.data.guardianMode || !this.data.minorUserId || !id || this.data.reviewingId) return;
    const authEpoch = api.currentAuthEpoch();
    this.setData({ reviewingId: id, error: "" });
    try {
      const result = await api.reviewGuardianChildMemory(
        this.data.minorUserId,
        id,
        "confirm",
      );
      if (!api.isAuthEpochCurrent(authEpoch)) return;
      if (result?.status !== "confirmed") {
        this.setData({ error: "确认没有完成，请稍后重试。" });
        return;
      }
      const refreshed = await this.loadMemories();
      if (refreshed === "ok") wx.showToast({ title: "已确认，会记住", icon: "success" });
    } catch (error) {
      if (!api.isAuthEpochCurrent(authEpoch)) return;
      this.setData({ error: friendlyError(error) });
    } finally {
      if (api.isAuthEpochCurrent(authEpoch)) this.setData({ reviewingId: "" });
    }
  },

  async retractMemory(event) {
    const dataset = event?.currentTarget?.dataset || {};
    const id = typeof dataset.id === "string" ? dataset.id : "";
    if (!this.data.guardianMode || !this.data.minorUserId || !id || this.data.reviewingId) return;
    const item = this._activeItems().find((entry) => entry.id === id);
    if (!item) return;

    const agreed = await new Promise((resolve) => {
      wx.showModal({
        title: "撤回这条记忆？",
        content: "撤回后机器人不会用到这条。",
        confirmText: "撤回",
        cancelText: "取消",
        success: (result) => resolve(Boolean(result?.confirm)),
        fail: () => resolve(false),
      });
    });
    if (!agreed) return;

    const authEpoch = api.currentAuthEpoch();
    this.setData({ reviewingId: id, error: "" });
    try {
      const result = await api.reviewGuardianChildMemory(
        this.data.minorUserId,
        id,
        "retract",
      );
      if (!api.isAuthEpochCurrent(authEpoch)) return;
      const statusLabel = MEMORY_STATUS_LABELS[result?.status];
      if (!statusLabel) {
        this.setData({ error: "撤回没有完成，请稍后重试。" });
        return;
      }
      const refreshed = await this.loadMemories();
      if (refreshed === "ok") wx.showToast({ title: "已撤回", icon: "success" });
    } catch (error) {
      if (!api.isAuthEpochCurrent(authEpoch)) return;
      this.setData({ error: friendlyError(error) });
    } finally {
      if (api.isAuthEpochCurrent(authEpoch)) this.setData({ reviewingId: "" });
    }
  },

  async restoreMemory(event) {
    const dataset = event?.currentTarget?.dataset || {};
    const id = typeof dataset.id === "string" ? dataset.id : "";
    if (!this.data.guardianMode || !this.data.minorUserId || !id || this.data.reviewingId) return;
    if (!this.data.retractedItems.some((entry) => entry.id === id)) return;

    const agreed = await new Promise((resolve) => {
      wx.showModal({
        title: "恢复这条记忆？",
        content: "恢复后机器人又可以用到这条。",
        confirmText: "恢复",
        cancelText: "取消",
        success: (result) => resolve(Boolean(result?.confirm)),
        fail: () => resolve(false),
      });
    });
    if (!agreed) return;

    const authEpoch = api.currentAuthEpoch();
    this.setData({ reviewingId: id, error: "" });
    try {
      const result = await api.reviewGuardianChildMemory(
        this.data.minorUserId,
        id,
        "confirm",
      );
      if (!api.isAuthEpochCurrent(authEpoch)) return;
      if (result?.status !== "confirmed") {
        this.setData({ error: "恢复没有完成，请稍后重试。" });
        return;
      }
      const refreshed = await this.loadMemories();
      if (refreshed === "ok") wx.showToast({ title: "已恢复", icon: "success" });
    } catch (error) {
      if (!api.isAuthEpochCurrent(authEpoch)) return;
      this.setData({ error: friendlyError(error) });
    } finally {
      if (api.isAuthEpochCurrent(authEpoch)) this.setData({ reviewingId: "" });
    }
  },

  openUnbindDeletePath() {
    const app = getApp();
    if (app && app.globalData) app.globalData.openUnbindSheetOnDevice = true;
    // The device page is a tab, so it opens with switchTab. It reads this
    // one-shot flag on show and opens the unbind sheet that offers the
    // subject-data deletion choice.
    wx.switchTab({ url: "/pages/device/index" });
  },
});
