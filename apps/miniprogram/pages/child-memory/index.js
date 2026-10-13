const api = require("../../utils/api");
const { requireLogin } = require("../../utils/auth-gate");
const { readBindingManifest } = require("../../utils/device-binding");
const {
  CHILD_ARCHIVE_CONSENT,
  CLEARED_TOAST,
  CONSENT_OFF_DIALOG,
  DELETE_CONFIRM,
  EMPTY_ARCHIVE_ON,
  EMPTY_RECENT,
  PRIVACY_LINE,
  archiveConsentFromPersonPayload,
  assertsNoTranscriptFields,
  boundChildFromManifest,
  groupArchiveByCategory,
  normalizeChildArchive,
  normalizeSessionSummaries,
} = require("../../utils/child-memory");

const SWIPE_OPEN_PX = 72;

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

function resolveMinor(query) {
  const fromQuery =
    typeof query?.minorId === "string" && query.minorId
      ? decodeURIComponent(query.minorId)
      : "";
  const nameFromQuery =
    typeof query?.name === "string" && query.name.trim()
      ? decodeURIComponent(query.name)
      : "";
  if (fromQuery) {
    return { minorId: fromQuery, childName: nameFromQuery || "孩子" };
  }
  const bound = boundChildFromManifest(readBindingManifest());
  if (!bound) return { minorId: "", childName: "孩子" };
  return { minorId: bound.personId, childName: bound.displayName };
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
    activeTab: "recent",
    privacyLine: PRIVACY_LINE,
    emptyRecent: EMPTY_RECENT,
    emptyArchiveOn: EMPTY_ARCHIVE_ON,
    summaries: [],
    archiveGroups: [],
    archiveFlat: [],
    archiveConsent: {
      ...CHILD_ARCHIVE_CONSENT,
      active: false,
      consentId: "",
      statusLabel: "未开启",
    },
    confirm: null,
    statusBanner: "",
  },

  onLoad(query) {
    const resolved = resolveMinor(query || {});
    this.setData(resolved);
    const app = getApp();
    this._unsubscribeAuthCleared = app?.subscribeAuthCleared?.(() => {
      this.setData({
        authenticated: false,
        summaries: [],
        archiveGroups: [],
        archiveFlat: [],
        loadState: "idle",
        error: "",
        statusBanner: "",
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
    if (!this.data.minorId) {
      const resolved = resolveMinor({});
      this.setData(resolved);
    }
    await this.refresh();
  },

  async onPullDownRefresh() {
    try {
      await this.refresh();
    } finally {
      wx.stopPullDownRefresh();
    }
  },

  showRecent() {
    this._closeAllSwipes();
    this.setData({ activeTab: "recent", statusBanner: "" });
  },

  showArchive() {
    this._closeAllSwipes();
    this.setData({ activeTab: "archive", statusBanner: "" });
  },

  async refresh() {
    const minorId = this.data.minorId;
    if (!minorId) {
      this.setData({
        loadState: "error",
        error: "没有找到孩子。请先完成家长代孩子绑定。",
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
      let archiveFlat = [];
      if (archiveConsent.active) {
        archiveFlat = normalizeChildArchive(await api.getChildArchive(minorId));
      }
      const summaries = normalizeSessionSummaries(summaryPayload);
      assertsNoTranscriptFields(summaries);
      assertsNoTranscriptFields(archiveFlat);
      this.setData({
        summaries,
        archiveFlat,
        archiveGroups: groupArchiveByCategory(archiveFlat),
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

  _closeAllSwipes() {
    const summaries = this.data.summaries.map((item) =>
      item.swipeOpen ? { ...item, swipeOpen: false } : item,
    );
    const archiveFlat = this.data.archiveFlat.map((item) =>
      item.swipeOpen ? { ...item, swipeOpen: false } : item,
    );
    this.setData({
      summaries,
      archiveFlat,
      archiveGroups: groupArchiveByCategory(archiveFlat),
    });
  },

  onSummaryTouchStart(event) {
    const touch = event.touches?.[0];
    if (!touch) return;
    this._swipe = {
      kind: "summary",
      recordId: event.currentTarget.dataset.recordId,
      startX: touch.clientX,
      startY: touch.clientY,
      lock: "",
    };
  },

  onSummaryTouchMove(event) {
    const swipe = this._swipe;
    const touch = event.touches?.[0];
    if (!swipe || swipe.kind !== "summary" || !touch) return;
    const dx = touch.clientX - swipe.startX;
    const dy = touch.clientY - swipe.startY;
    if (!swipe.lock) {
      if (Math.abs(dx) < 8 && Math.abs(dy) < 8) return;
      swipe.lock = Math.abs(dx) > Math.abs(dy) ? "h" : "v";
    }
    if (swipe.lock !== "h") return;
    const open = dx < -SWIPE_OPEN_PX / 2;
    this._setSummarySwipe(swipe.recordId, open);
  },

  onSummaryTouchEnd() {
    this._swipe = null;
  },

  _setSummarySwipe(recordId, open) {
    const summaries = this.data.summaries.map((item) => ({
      ...item,
      swipeOpen: item.recordId === recordId ? open : false,
    }));
    this.setData({ summaries });
  },

  onArchiveTouchStart(event) {
    const touch = event.touches?.[0];
    if (!touch) return;
    this._swipe = {
      kind: "archive",
      recordId: event.currentTarget.dataset.recordId,
      startX: touch.clientX,
      startY: touch.clientY,
      lock: "",
    };
  },

  onArchiveTouchMove(event) {
    const swipe = this._swipe;
    const touch = event.touches?.[0];
    if (!swipe || swipe.kind !== "archive" || !touch) return;
    const dx = touch.clientX - swipe.startX;
    const dy = touch.clientY - swipe.startY;
    if (!swipe.lock) {
      if (Math.abs(dx) < 8 && Math.abs(dy) < 8) return;
      swipe.lock = Math.abs(dx) > Math.abs(dy) ? "h" : "v";
    }
    if (swipe.lock !== "h") return;
    const open = dx < -SWIPE_OPEN_PX / 2;
    this._setArchiveSwipe(swipe.recordId, open);
  },

  onArchiveTouchEnd() {
    this._swipe = null;
  },

  _setArchiveSwipe(recordId, open) {
    const archiveFlat = this.data.archiveFlat.map((item) => ({
      ...item,
      swipeOpen: item.recordId === recordId ? open : false,
    }));
    this.setData({
      archiveFlat,
      archiveGroups: groupArchiveByCategory(archiveFlat),
    });
  },

  askDeleteSummary(event) {
    const recordId = event.currentTarget.dataset.recordId;
    if (!recordId) return;
    this.setData({
      confirm: {
        kind: "delete-summary",
        recordId,
        title: "删除这条摘要？",
        body: DELETE_CONFIRM,
        confirmLabel: "删除",
      },
    });
  },

  askClearSummaries() {
    if (!this.data.summaries.length) return;
    this.setData({
      confirm: {
        kind: "clear-summaries",
        title: "清空全部摘要？",
        body: DELETE_CONFIRM,
        confirmLabel: "清空全部",
      },
    });
  },

  askDeleteArchive(event) {
    const recordId = event.currentTarget.dataset.recordId;
    if (!recordId) return;
    this.setData({
      confirm: {
        kind: "delete-archive",
        recordId,
        title: "删除这条记忆？",
        body: DELETE_CONFIRM,
        confirmLabel: "删除",
      },
    });
  },

  askClearArchive() {
    if (!this.data.archiveFlat.length) return;
    this.setData({
      confirm: {
        kind: "clear-archive",
        title: "清空长期记忆？",
        body: DELETE_CONFIRM,
        confirmLabel: "清空全部",
      },
    });
  },

  async onArchiveConsentChange(event) {
    if (this.data.working) return;
    const wantOn = Boolean(event.detail?.value);
    const { archiveConsent, minorId } = this.data;
    if (!minorId || wantOn === archiveConsent.active) return;

    if (!wantOn) {
      // Revert switch visually until the parent confirms turn-off.
      this.setData({
        "archiveConsent.active": true,
        confirm: {
          kind: "consent-off",
          title: "关闭长期记忆？",
          body: CONSENT_OFF_DIALOG,
          confirmLabel: "关闭",
        },
      });
      return;
    }

    this.setData({ working: true, error: "" });
    try {
      await api.grantPersonConsent({
        personId: minorId,
        consentKind: CHILD_ARCHIVE_CONSENT.kind,
        policyVersion: CHILD_ARCHIVE_CONSENT.policyVersion,
        idempotencyKey: operationKey("child-archive-grant"),
      });
      await this.refresh();
    } catch (error) {
      this.setData({ error: pageError(error, "长期记忆授权未能更新。") });
      await this.refresh();
    } finally {
      this.setData({ working: false });
    }
  },

  closeConfirm() {
    if (this.data.working) return;
    const wasConsentOff = this.data.confirm?.kind === "consent-off";
    this.setData({
      confirm: null,
      ...(wasConsentOff ? { "archiveConsent.active": true } : {}),
    });
  },

  noop() {},

  async runConfirm() {
    const confirm = this.data.confirm;
    const minorId = this.data.minorId;
    if (!confirm || !minorId || this.data.working) return;
    this.setData({ working: true, error: "", statusBanner: "" });
    try {
      if (confirm.kind === "delete-summary") {
        await api.deleteChildSessionSummary(minorId, confirm.recordId);
        this.setData({ confirm: null });
        await this.refresh();
      } else if (confirm.kind === "clear-summaries") {
        await api.clearChildSessionSummaries(minorId);
        this.setData({ confirm: null, statusBanner: CLEARED_TOAST });
        await this.refresh();
      } else if (confirm.kind === "delete-archive") {
        await api.deleteChildArchiveFact(minorId, confirm.recordId);
        this.setData({ confirm: null });
        await this.refresh();
      } else if (confirm.kind === "clear-archive") {
        await api.clearChildArchive(minorId);
        this.setData({ confirm: null, statusBanner: CLEARED_TOAST });
        await this.refresh();
      } else if (confirm.kind === "consent-off") {
        if (!this.data.archiveConsent.consentId) {
          throw new TypeError("找不到可撤回的授权记录。");
        }
        await api.revokePersonConsent({
          personId: minorId,
          consentId: this.data.archiveConsent.consentId,
          idempotencyKey: operationKey("child-archive-revoke"),
        });
        this.setData({ confirm: null });
        await this.refresh();
      }
    } catch (error) {
      this.setData({ error: pageError(error, "操作失败，请稍后再试。") });
    } finally {
      this.setData({ working: false });
    }
  },
});
