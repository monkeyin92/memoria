const api = require("../../utils/api");
const { requireLogin } = require("../../utils/auth-gate");
const {
  CHILD_ARCHIVE_CONSENT,
  archiveConsentFromPersonPayload,
  assertsNoTranscriptFields,
  normalizeChildArchive,
  normalizeSessionSummaries,
} = require("../../utils/child-memory");

function operationKey(prefix) {
  return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

function pageError(error, fallback) {
  if (error?.code === "guardian_memory_review_unavailable" || error?.status === 403) {
    return "只有绑定这台设备的家长可以查看和删除孩子的记忆。";
  }
  if (error?.code === "child_layers_unavailable" || error?.status === 503) {
    return "记忆服务暂时不可用，请稍后再试。";
  }
  return error?.message || fallback;
}

Page({
  data: {
    authenticated: false,
    loading: false,
    working: false,
    error: "",
    minorId: "",
    childName: "孩子",
    loadState: "idle",
    summaries: [],
    archive: [],
    archiveConsent: {
      ...CHILD_ARCHIVE_CONSENT,
      active: false,
      consentId: "",
      statusLabel: "未开启",
    },
    confirm: null,
  },

  onLoad(query) {
    const minorId = typeof query?.minorId === "string" ? decodeURIComponent(query.minorId) : "";
    const childName =
      typeof query?.name === "string" && query.name.trim()
        ? decodeURIComponent(query.name)
        : "孩子";
    this.setData({ minorId, childName });
    const app = getApp();
    this._unsubscribeAuthCleared = app?.subscribeAuthCleared?.(() => {
      this.setData({
        authenticated: false,
        summaries: [],
        archive: [],
        loadState: "idle",
        error: "",
      });
    });
  },

  onUnload() {
    this._unsubscribeAuthCleared?.();
  },

  async onShow() {
    if (!(await requireLogin({ reason: "view_child_memory" }))) {
      this.setData({ authenticated: false });
      return;
    }
    this.setData({ authenticated: true });
    await this.refresh();
  },

  async onPullDownRefresh() {
    try {
      await this.refresh();
    } finally {
      wx.stopPullDownRefresh();
    }
  },

  goBack() {
    wx.navigateBack({ fail: () => wx.navigateTo({ url: "/pages/guardian/index" }) });
  },

  async refresh() {
    const minorId = this.data.minorId;
    if (!minorId) {
      this.setData({
        loadState: "error",
        error: "没有找到孩子。请从「家庭与监护」进入。",
      });
      return;
    }
    const authEpoch = api.currentAuthEpoch();
    this.setData({ loading: true, error: "", loadState: "loading" });
    try {
      api.touchChildActivity(minorId).catch(() => {});
      const [summaryPayload, consentPayload] = await Promise.all([
        api.getChildSessionSummaries(minorId),
        api.getPersonConsents(minorId),
      ]);
      if (!api.isAuthEpochCurrent(authEpoch)) return;
      const archiveConsent = archiveConsentFromPersonPayload(consentPayload);
      let archive = [];
      if (archiveConsent.active) {
        archive = normalizeChildArchive(await api.getChildArchive(minorId));
      }
      const summaries = normalizeSessionSummaries(summaryPayload);
      assertsNoTranscriptFields(summaries);
      assertsNoTranscriptFields(archive);
      this.setData({
        summaries,
        archive,
        archiveConsent,
        loadState: "ready",
        error: "",
      });
    } catch (error) {
      if (api.isAuthEpochCurrent(authEpoch)) {
        this.setData({
          loadState: "error",
          error: pageError(error, "暂时读不到孩子的记忆，请稍后再试。"),
        });
      }
    } finally {
      if (api.isAuthEpochCurrent(authEpoch)) {
        this.setData({ loading: false });
      }
    }
  },

  async onArchiveConsentChange(event) {
    if (this.data.working) return;
    const wantOn = Boolean(event.detail?.value);
    const { archiveConsent, minorId } = this.data;
    if (!minorId || wantOn === archiveConsent.active) return;
    this.setData({ working: true, error: "" });
    try {
      if (wantOn) {
        await api.grantPersonConsent({
          personId: minorId,
          consentKind: CHILD_ARCHIVE_CONSENT.kind,
          policyVersion: CHILD_ARCHIVE_CONSENT.policyVersion,
          idempotencyKey: operationKey("child-archive-grant"),
        });
      } else {
        if (!archiveConsent.consentId) {
          throw new TypeError("找不到可撤回的授权记录。");
        }
        await api.revokePersonConsent({
          personId: minorId,
          consentId: archiveConsent.consentId,
          idempotencyKey: operationKey("child-archive-revoke"),
        });
      }
      await this.refresh();
    } catch (error) {
      this.setData({ error: pageError(error, "长期小档案授权未能更新。") });
      await this.refresh();
    } finally {
      this.setData({ working: false });
    }
  },

  askDeleteSummary(event) {
    const recordId = event.currentTarget.dataset.recordId;
    const item = this.data.summaries.find((row) => row.recordId === recordId);
    if (!item) return;
    this.setData({
      confirm: {
        kind: "delete-summary",
        recordId,
        title: "删除这条会话小结？",
        body: "删除后不可恢复。这里只会删掉家长可见的小结，不会留下孩子的原话。",
        confirmLabel: "删除这条",
      },
    });
  },

  askClearSummaries() {
    if (!this.data.summaries.length) return;
    this.setData({
      confirm: {
        kind: "clear-summaries",
        title: "清空全部会话小结？",
        body: "将永久删除这个孩子的全部会话小结，且不可恢复。",
        confirmLabel: "全部清空",
      },
    });
  },

  askDeleteArchive(event) {
    const recordId = event.currentTarget.dataset.recordId;
    const item = this.data.archive.find((row) => row.recordId === recordId);
    if (!item) return;
    this.setData({
      confirm: {
        kind: "delete-archive",
        recordId,
        title: "删除这条小档案？",
        body: `将忘掉「${item.fieldLabel}：${item.text}」。删除后不可恢复。`,
        confirmLabel: "删除这条",
      },
    });
  },

  askClearArchive() {
    if (!this.data.archive.length) return;
    this.setData({
      confirm: {
        kind: "clear-archive",
        title: "清空长期小档案？",
        body: "将永久删除这个孩子的全部长期小档案条目，且不可恢复。",
        confirmLabel: "全部清空",
      },
    });
  },

  closeConfirm() {
    if (this.data.working) return;
    this.setData({ confirm: null });
  },

  noop() {},

  async runConfirm() {
    const confirm = this.data.confirm;
    const minorId = this.data.minorId;
    if (!confirm || !minorId || this.data.working) return;
    this.setData({ working: true, error: "" });
    try {
      if (confirm.kind === "delete-summary") {
        await api.deleteChildSessionSummary(minorId, confirm.recordId);
      } else if (confirm.kind === "clear-summaries") {
        await api.clearChildSessionSummaries(minorId);
      } else if (confirm.kind === "delete-archive") {
        await api.deleteChildArchiveFact(minorId, confirm.recordId);
      } else if (confirm.kind === "clear-archive") {
        await api.clearChildArchive(minorId);
      }
      this.setData({ confirm: null });
      wx.showToast({ title: "已删除", icon: "none" });
      await this.refresh();
    } catch (error) {
      this.setData({ error: pageError(error, "删除失败，请稍后再试。") });
    } finally {
      this.setData({ working: false });
    }
  },
});
