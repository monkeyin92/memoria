const api = require("../../utils/api");
const { statusBarStyle } = require("../../utils/nav-inset");
const { requireLogin } = require("../../utils/auth-gate");
const { companionById, defaultCompanionId } = require("../../utils/companions");
const { readBindingManifest } = require("../../utils/device-binding");
const {
  CHILD_ARCHIVE_CONSENT,
  CLEARED_TOAST,
  CONSENT_OFF_DIALOG,
  DELETE_CONFIRM,
  DELETE_KEEP,
  EMPTY_ARCHIVE_ON,
  EMPTY_RECENT,
  PRIVACY_LINE,
  archiveConsentFromPersonPayload,
  assertsNoTranscriptFields,
  boundChildFromManifest,
  groupArchiveByCategory,
  memoryEmptyTitle,
  memoryHeaderTitle,
  normalizeChildArchive,
  normalizeSessionSummaries,
} = require("../../utils/child-memory");

function operationKey(prefix) {
  return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

function pageError(error, fallback) {
  if (error?.code === "guardian_memory_review_unavailable" || error?.status === 403) {
    return "只有绑定这台设备的家长可以打开孩子的记忆档案。";
  }
  if (error?.code === "child_layers_unavailable" || error?.status === 503) {
    return "记忆服务暂时不可用，请稍后再试。";
  }
  return error?.message || fallback;
}

function loginRequired(error) {
  return error?.message === "请先使用微信登录。";
}

function guestMemoryPatch() {
  return {
    authenticated: false,
    summaries: [],
    archiveGroups: [],
    archiveFlat: [],
    loadState: "idle",
    loading: false,
    error: "",
    statusBanner: "",
    confirm: null,
  };
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

function quoteForRecord(list, recordId) {
  const hit = (list || []).find((item) => item.recordId === recordId);
  return hit?.text || "";
}

Page({
  data: {
    statusBarStyle: statusBarStyle(),
    authenticated: false,
    loading: false,
    working: false,
    error: "",
    minorId: "",
    childName: "孩子",
    companionId: defaultCompanionId,
    companionName: companionById(defaultCompanionId).name,
    headerTitle: memoryHeaderTitle(companionById(defaultCompanionId).name, "孩子"),
    emptyTitle: memoryEmptyTitle(companionById(defaultCompanionId).name, "孩子"),
    loadState: "idle",
    privacyLine: PRIVACY_LINE,
    emptyRecent: EMPTY_RECENT,
    emptyArchiveOn: EMPTY_ARCHIVE_ON,
    deleteKeepLabel: DELETE_KEEP,
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
    const companion = companionById(defaultCompanionId);
    this._scrollToRecent = query?.section === "recent";
    this.setData({
      ...resolved,
      companionId: companion.id,
      companionName: companion.name,
      headerTitle: memoryHeaderTitle(companion.name, resolved.childName),
      emptyTitle: memoryEmptyTitle(companion.name, resolved.childName),
    });
    const app = getApp();
    this._unsubscribeAuthCleared = app?.subscribeAuthCleared?.(() => {
      this.setData(guestMemoryPatch());
    });
  },

  onUnload() {
    this._unsubscribeAuthCleared?.();
  },

  async onShow() {
    if (!(await requireLogin({ reason: "view_child_memory" }))) {
      this.setData(guestMemoryPatch());
      return;
    }
    this.setData({ authenticated: true });
    if (!this.data.minorId) {
      const resolved = resolveMinor({});
      this.setData({
        ...resolved,
        headerTitle: memoryHeaderTitle(this.data.companionName, resolved.childName),
        emptyTitle: memoryEmptyTitle(this.data.companionName, resolved.childName),
      });
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
      const [summaryPayload, consentPayload, profileResult] = await Promise.all([
        api.getChildSessionSummaries(minorId),
        api.getPersonConsents(minorId),
        (async () => {
          try {
            const identity = api.currentIdentity();
            if (!identity) return null;
            return await api.getProfile(identity.user_id);
          } catch {
            return null;
          }
        })(),
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
      const companion = companionById(profileResult?.companion_id || defaultCompanionId);
      this.setData({
        summaries,
        archiveFlat,
        archiveGroups: groupArchiveByCategory(archiveFlat),
        archiveConsent,
        companionId: companion.id,
        companionName: companion.name,
        headerTitle: memoryHeaderTitle(companion.name, this.data.childName),
        emptyTitle: memoryEmptyTitle(companion.name, this.data.childName),
        loadState: "ready",
        error: "",
      });
    } catch (error) {
      if (loginRequired(error)) {
        this.setData(guestMemoryPatch());
        return;
      }
      if (api.isAuthEpochCurrent(authEpoch)) {
        this.setData({
          loadState: "error",
          error: pageError(error, "暂时读不到孩子的记忆，请稍后再试。"),
        });
      }
    } finally {
      if (api.isAuthEpochCurrent(authEpoch)) {
        this.setData({ loading: false }, () => this._revealRecent());
      }
    }
  },

  _revealRecent() {
    if (!this._scrollToRecent || this.data.loadState !== "ready") return;
    this._scrollToRecent = false;
    wx.pageScrollTo({ selector: "#cm-recent", duration: 0 });
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
        quote: quoteForRecord(this.data.summaries, recordId),
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
        quote: "",
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
        quote: quoteForRecord(this.data.archiveFlat, recordId),
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
        quote: "",
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
      this.setData({
        "archiveConsent.active": true,
        confirm: {
          kind: "consent-off",
          title: "关闭长期记忆？",
          body: CONSENT_OFF_DIALOG,
          quote: "",
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
      if (loginRequired(error)) {
        this.setData(guestMemoryPatch());
        return;
      }
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
      if (loginRequired(error)) {
        this.setData(guestMemoryPatch());
        return;
      }
      this.setData({ error: pageError(error, "操作失败，请稍后再试。") });
    } finally {
      this.setData({ working: false });
    }
  },
});
