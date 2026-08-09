const api = require("../../utils/api");
const { guardianSummaryErrorState } = require("../../utils/guardian");
const { requireLogin } = require("../../utils/auth-gate");

const CONSENT_DEFINITIONS = Object.freeze([
  {
    kind: "minor_voice_session",
    label: "语音陪伴",
    description: "允许孩子进入陪伴和导师语音会话",
    policyVersion: "minor-voice-v1",
  },
  {
    kind: "memory_retention",
    label: "学习与成长记录",
    description: "保存脱敏转写、学习进度与安全的长期记忆；不保存原始音频",
    policyVersion: "minor-memory-v1",
  },
  {
    kind: "weekly_report",
    label: "每周成长小结",
    description: "只分享趋势、学习时长和话题分布，不含对话原文",
    policyVersion: "guardian-weekly-v1",
  },
]);

function operationKey(prefix) {
  return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

function consentRows(payload) {
  const items = Array.isArray(payload?.items) ? payload.items : [];
  return CONSENT_DEFINITIONS.map((definition) => {
    const record = items.find(
      (item) => item?.consent_kind === definition.kind && item?.active === true,
    );
    return {
      ...definition,
      active: Boolean(record),
      consentId: typeof record?.consent_id === "string" ? record.consent_id : "",
    };
  });
}

Page({
  data: {
    authenticated: false,
    loading: false,
    working: false,
    error: "",
    role: "unknown",
    links: [],
    activeLinks: [],
    pendingLinks: [],
    minorUserId: "",
    relation: "parent",
    bindingCode: "",
    createdBindingCode: "",
    createdMinorName: "",
    birthBands: [
      { value: "under_14", label: "14 岁以下" },
      { value: "14_to_17", label: "14 至 17 岁" },
    ],
    birthBandIndex: 0,
    selectedLinkId: "",
    selectedMinorId: "",
    summary: null,
    summaryState: "idle",
    notifications: [],
  },

  onLoad() {
    const app = getApp();
    this._unsubscribeAuthCleared = app?.subscribeAuthCleared?.(() => this._enterGuestState());
  },

  async onShow() {
    if (!(await requireLogin({ reason: "view_guardian_summary" }))) {
      this._enterGuestState();
      return;
    }
    this.setData({ authenticated: true });
    await this.refresh();
  },

  onUnload() {
    this._unsubscribeAuthCleared?.();
    this._unsubscribeAuthCleared = null;
  },

  onPullDownRefresh() {
    this.refresh().finally(() => wx.stopPullDownRefresh());
  },

  _enterGuestState() {
    this.setData({
      authenticated: false,
      loading: false,
      role: "unknown",
      links: [],
      activeLinks: [],
      pendingLinks: [],
      notifications: [],
      summary: null,
    });
  },

  async refresh() {
    const identity = api.currentIdentity();
    if (!identity || this.data.loading) return;
    const authEpoch = api.currentAuthEpoch();
    this.setData({ loading: true, error: "" });
    try {
      const [profile, links] = await Promise.all([
        api.getProfile(identity.user_id),
        api.getGuardianLinks(),
      ]);
      if (!api.isAuthEpochCurrent(authEpoch)) return;
      const role = profile.subject_category === "minor" ? "minor" : "guardian";
      const activeLinks = links.filter((item) => item.status === "active");
      const pendingLinks = links.filter((item) => item.status === "pending");
      this.setData({ role, links, activeLinks, pendingLinks });
      if (role === "guardian") {
        await Promise.all([this._loadLinkConsents(activeLinks), this._loadNotifications()]);
        if (activeLinks.length) await this.selectMinorById(activeLinks[0].minorUserId);
      }
    } catch (error) {
      if (api.isAuthEpochCurrent(authEpoch)) {
        this.setData({ error: error?.message || "监护信息暂时无法加载。" });
      }
    } finally {
      if (api.isAuthEpochCurrent(authEpoch)) this.setData({ loading: false });
    }
  },

  async _loadLinkConsents(links) {
    const withConsents = await Promise.all(
      links.map(async (link) => ({
        ...link,
        consentRows: consentRows(await api.getGuardianConsents(link.linkId)),
      })),
    );
    this.setData({ activeLinks: withConsents });
  },

  async _loadNotifications() {
    const payload = await api.getGuardianNotifications();
    this.setData({ notifications: Array.isArray(payload?.items) ? payload.items : [] });
  },

  onMinorUserId(event) {
    this.setData({ minorUserId: event.detail.value.trim() });
  },

  onBindingCode(event) {
    this.setData({ bindingCode: event.detail.value.replace(/\D/g, "").slice(0, 8) });
  },

  onBirthBand(event) {
    this.setData({ birthBandIndex: Number(event.detail.value) || 0 });
  },

  async createLink() {
    if (!this.data.minorUserId || this.data.working) return;
    this.setData({ working: true, error: "", createdBindingCode: "" });
    try {
      const link = await api.createGuardianLink({
        minorUserId: this.data.minorUserId,
        relation: this.data.relation,
        idempotencyKey: operationKey("guardian-link"),
      });
      this.setData({
        createdBindingCode: link.binding_code || "",
        createdMinorName: link.minor_display_name || "孩子",
        minorUserId: "",
      });
      await this.refresh();
    } catch (error) {
      this.setData({ error: error?.message || "绑定发起失败。" });
    } finally {
      this.setData({ working: false });
    }
  },

  async confirmLink(event) {
    const linkId = event.currentTarget.dataset.linkId;
    const band = this.data.birthBands[this.data.birthBandIndex]?.value || "under_14";
    if (!linkId || this.data.bindingCode.length !== 8 || this.data.working) return;
    this.setData({ working: true, error: "" });
    try {
      await api.confirmGuardianLink({
        linkId,
        bindingCode: this.data.bindingCode,
        birthYearBand: band,
      });
      api.logoutLocal();
      wx.showModal({
        title: "学生模式已开通",
        content: "账号权限已经按学生模式重新计算，请重新使用微信登录。",
        showCancel: false,
        success: () => wx.reLaunch({ url: "/pages/auth/index" }),
      });
    } catch (error) {
      this.setData({ error: error?.message || "绑定确认失败，请检查绑定码。" });
    } finally {
      this.setData({ working: false });
    }
  },

  async toggleConsent(event) {
    if (this.data.working) return;
    const { linkId, kind, policyVersion, consentId, active } = event.currentTarget.dataset;
    this.setData({ working: true, error: "" });
    try {
      if (active) {
        await api.revokeGuardianConsent({
          linkId,
          consentId,
          idempotencyKey: operationKey("guardian-revoke"),
        });
      } else {
        await api.grantGuardianConsent({
          linkId,
          consentKind: kind,
          policyVersion,
          idempotencyKey: operationKey("guardian-grant"),
        });
      }
      await this.refresh();
    } catch (error) {
      this.setData({ error: error?.message || "授权状态更新失败。" });
    } finally {
      this.setData({ working: false });
    }
  },

  selectMinor(event) {
    return this.selectMinorById(event.currentTarget.dataset.minorId);
  },

  async selectMinorById(minorUserId) {
    if (!minorUserId) return;
    this.setData({ selectedMinorId: minorUserId, summaryState: "loading", summary: null });
    try {
      const summary = await api.getGuardianSummary(minorUserId);
      this.setData({ summary, summaryState: summary.hasData ? "ready" : "empty" });
    } catch (error) {
      this.setData({ summaryState: guardianSummaryErrorState(error) });
    }
  },
});
