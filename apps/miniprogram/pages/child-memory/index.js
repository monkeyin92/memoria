const api = require("../../utils/api");
const { statusBarStyle } = require("../../utils/nav-inset");
const { leavePage } = require("../../utils/nav-back");
const { isAuthError, openLogin, requireLogin } = require("../../utils/auth-gate");
const { companionById, defaultCompanionId } = require("../../utils/companions");
const { readBindingManifest } = require("../../utils/device-binding");
const {
  CHILD_ARCHIVE_CONSENT,
  CLEARED_TOAST,
  CLEAR_CONFIRM,
  CONSENT_OFF_DIALOG,
  DELETE_CONFIRM,
  DELETE_KEEP,
  EMPTY_ARCHIVE_OFF,
  EMPTY_ARCHIVE_OFF_TITLE,
  EMPTY_ARCHIVE_ON,
  EMPTY_RECENT,
  PRIVACY_LINE,
  archiveConsentFromPersonPayload,
  archiveFactCards,
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
  return isAuthError(error);
}

function guestMemoryPatch() {
  return {
    authenticated: false,
    summaries: [],
    archiveGroups: [],
    archiveCards: [],
    archiveFlat: [],
    loadState: "idle",
    loading: false,
    working: false,
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

function archiveFactForRecord(list, recordId) {
  return (list || []).find((item) => item.recordId === recordId) || null;
}

function historyChangeDraft(text) {
  const normalized = typeof text === "string" ? text.trim() : "";
  if (!normalized) return "";
  if (normalized.startsWith("不喜欢")) return normalized;
  if (normalized.startsWith("喜欢")) return `不喜欢${normalized.slice(2)}`;
  return `不喜欢${normalized}`;
}

function polarityForArchiveText(text) {
  const normalized = typeof text === "string" ? text.trim() : "";
  if (normalized.startsWith("不喜欢")) return "negative";
  if (normalized.startsWith("喜欢")) return "positive";
  return undefined;
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
    emptyArchiveOffTitle: EMPTY_ARCHIVE_OFF_TITLE,
    emptyArchiveOff: EMPTY_ARCHIVE_OFF,
    deleteKeepLabel: DELETE_KEEP,
    summaries: [],
    archiveGroups: [],
    archiveCards: [],
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

  goBack() {
    leavePage("/pages/profile/index");
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

  openChildMemoryLogin() {
    openLogin({ reason: "view_child_memory" });
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
      const archiveGroups = groupArchiveByCategory(archiveFlat);
      const companion = companionById(profileResult?.companion_id || defaultCompanionId);
      this.setData({
        summaries,
        archiveFlat,
        archiveGroups,
        archiveCards: archiveFactCards(archiveGroups),
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
        body: CLEAR_CONFIRM,
        quote: "",
        confirmLabel: "清空全部",
      },
    });
  },

  askConfirmArchive(event) {
    const recordId = event.currentTarget.dataset.recordId;
    if (!recordId) return;
    this.setData({
      confirm: {
        kind: "confirm-archive",
        recordId,
        title: "确认这条记忆？",
        body: "确认后，机器人可以在以后的聊天里使用这条记忆。",
        quote: quoteForRecord(this.data.archiveFlat, recordId),
        confirmLabel: "确认",
      },
    });
  },

  askCorrectArchive(event) {
    const recordId = event.currentTarget.dataset.recordId;
    if (!recordId) return;
    const current = quoteForRecord(this.data.archiveFlat, recordId);
    this.setData({
      confirm: {
        kind: "correct-archive",
        recordId,
        title: "更正这条记忆？",
        body: "请输入正确内容。更正会生成新版本，旧版本不再用于展示和召回。",
        quote: current,
        confirmLabel: "更正",
        draftText: current,
        historyChange: false,
        error: "",
      },
    });
  },

  askHistoryChange(event) {
    const recordId = event.currentTarget.dataset.recordId;
    if (!recordId) return;
    const fact = archiveFactForRecord(this.data.archiveFlat, recordId);
    if (!fact || fact.field !== "preference") {
      this.askCorrectArchive(event);
      return;
    }
    const current = fact.text || "";
    this.setData({
      confirm: {
        kind: "correct-archive",
        recordId,
        title: "更新为现在不喜欢？",
        body: "请确认并按需修改正确内容。提交后会撤回旧版本并写入新版本，不会并列两条冲突事实。",
        quote: current,
        confirmLabel: "更新",
        draftText: historyChangeDraft(current),
        historyChange: true,
        error: "",
      },
    });
  },

  askWithdrawArchive(event) {
    const recordId = event.currentTarget.dataset.recordId;
    if (!recordId) return;
    this.setData({
      confirm: {
        kind: "withdraw-archive",
        recordId,
        title: "撤回这条记忆？",
        body: "撤回后不会再出现在默认列表，也不会进入对话召回。新会话与缓存行为由 A05 验收。",
        quote: quoteForRecord(this.data.archiveFlat, recordId),
        confirmLabel: "撤回",
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
        body: CLEAR_CONFIRM,
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

  onConfirmInput(event) {
    if (!this.data.confirm) return;
    this.setData({
      "confirm.draftText": event.detail?.value || "",
      "confirm.error": "",
    });
  },

  async runConfirm() {
    const confirm = this.data.confirm;
    const minorId = this.data.minorId;
    if (!confirm || !minorId || this.data.working) return;
    if (confirm.kind === "correct-archive") {
      const originalText = String(confirm.quote || "").trim();
      const draftText = String(confirm.draftText || "").trim();
      if (!draftText) {
        this.setData({ "confirm.error": "请输入正确内容后再提交。" });
        return;
      }
      if (draftText === originalText) {
        this.setData({ "confirm.error": "请修改内容后再提交。" });
        return;
      }
    }
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
      } else if (confirm.kind === "confirm-archive") {
        const receipt = await api.reviewChildArchiveFact(minorId, confirm.recordId, {
          action: "confirm",
        });
        this.setData({
          confirm: null,
          statusBanner: receipt?.display_label
            ? `已确认（回执 ${receipt.audit_event_id || ""}）`
            : "已确认",
        });
        await this.refresh();
      } else if (confirm.kind === "correct-archive") {
        const draftText = String(confirm.draftText || "").trim();
        const receipt = await api.reviewChildArchiveFact(minorId, confirm.recordId, {
          action: "correct",
          text: draftText,
          polarity: confirm.historyChange
            ? polarityForArchiveText(draftText)
            : confirm.polarity || undefined,
          history_change: Boolean(confirm.historyChange),
        });
        this.setData({
          confirm: null,
          statusBanner: receipt?.new_record_id
            ? `已更新为新版本（回执 ${receipt.audit_event_id || ""}）`
            : "已更正",
        });
        await this.refresh();
      } else if (confirm.kind === "withdraw-archive") {
        const receipt = await api.reviewChildArchiveFact(minorId, confirm.recordId, {
          action: "withdraw",
        });
        this.setData({
          confirm: null,
          statusBanner: receipt?.audit_event_id
            ? `已撤回（回执 ${receipt.audit_event_id}）`
            : "已撤回",
        });
        await this.refresh();
      } else if (confirm.kind === "delete-archive") {
        await api.deleteChildArchiveFact(minorId, confirm.recordId);
        this.setData({ confirm: null, statusBanner: "已删除" });
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
      const message = pageError(error, "操作失败，请稍后再试。");
      if (this.data.confirm) {
        this.setData({ "confirm.error": message });
      } else {
        this.setData({ error: message });
      }
    } finally {
      this.setData({ working: false });
    }
  },
});
