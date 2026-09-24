const api = require("../../utils/api");
const { guardianSummaryErrorState } = require("../../utils/guardian");
const { requireLogin } = require("../../utils/auth-gate");
const contracts = require("../../utils/multi-subject-contracts");
const { capabilityGateMessage, configActionGate } = require("../../utils/device-binding");

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

function consentTime(item) {
  const stamp = Date.parse(item?.revoked_at || item?.granted_at || "");
  return Number.isFinite(stamp) ? stamp : 0;
}

function consentRows(payload) {
  const items = Array.isArray(payload?.items) ? payload.items : [];
  return CONSENT_DEFINITIONS.map((definition) => {
    const matches = items
      .filter((item) => item?.consent_kind === definition.kind)
      .sort((left, right) => consentTime(right) - consentTime(left));
    const record = matches.find((item) => item?.active === true) || null;
    const latest = matches[0] || null;
    let statusLabel = "未授权";
    if (record) statusLabel = "已授权";
    else if (latest && latest.active === false && latest.expires_at && !latest.revoked_at) {
      statusLabel = "已过期";
    }
    return {
      ...definition,
      active: Boolean(record),
      consentId: typeof record?.consent_id === "string" ? record.consent_id : "",
      statusLabel,
    };
  });
}

function minorDataError(error, fallback) {
  if (error?.status === 403) return "只有监护人或绑定发起人可以处理 TA 的数据。";
  return error?.message || fallback;
}

function personConsentError(error) {
  if (error?.code === "guardian_binding_owner_required" || error?.status === 403) {
    return "只有监护绑定发起人可以修改这项授权。";
  }
  return error?.message || "授权状态更新失败。";
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
      { value: "14_17", label: "14 至 17 岁" },
    ],
    birthBandIndex: 0,
    selectedLinkId: "",
    boundSubjects: [],
    selectedMinorId: "",
    summary: null,
    summaryState: "idle",
    notifications: [],
    minorExport: null,
    showMinorDelete: false,
    minorDeletePersonId: "",
    minorDeleteConfirmation: "",
    minorDeleteConfirmText: api.GUARDIAN_MINOR_DELETE_CONFIRMATION,
    minorDeleting: false,
    minorDeleteError: "",
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

  /* 纯展示：非 tab 页经导航返回。 */
  goBack() {
    wx.navigateBack({ delta: 1 });
  },

  _enterGuestState() {
    this.setData({
      authenticated: false,
      loading: false,
      role: "unknown",
      links: [],
      activeLinks: [],
      pendingLinks: [],
      boundSubjects: [],
      notifications: [],
      summary: null,
      minorExport: null,
      showMinorDelete: false,
      minorDeletePersonId: "",
      minorDeleteConfirmation: "",
      minorDeleting: false,
      minorDeleteError: "",
    });
  },

  /* 每次敏感读/写动作都重新走 Runtime Profile 能力门禁（D-07）。 */
  async _gateGuardian() {
    const gate = await api.requireRuntimeCapability(contracts.Capability.GuardianSummaryView);
    if (!gate.allowed) {
      this.setData({ error: capabilityGateMessage(gate, contracts.Capability.GuardianSummaryView) });
    }
    return gate.allowed;
  },

  /* 创建关系/修改授权属于配置动作：走独立 config seam（P1 边界）。 */
  _gateGuardianConfig() {
    const gate = configActionGate("guardian_manage");
    this.setData({ error: gate.message });
    return gate.allowed;
  },

  async refresh() {
    const identity = api.currentIdentity();
    if (!identity || this.data.loading) return;
    if (!(await this._gateGuardian())) return;
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
        await Promise.all([
          this._loadLinkConsents(activeLinks),
          this._loadBoundSubjectConsents(),
          this._loadNotifications(),
        ]);
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

  /*
   * 无账号孩子没有 guardian link。只读当前 ACTIVE parent_for_child 绑定里
   * 非账号持有人的主要使用者，再按 person 端点取同意。错绑不按登录账号查。
   */
  _boundChildSubjects() {
    const binding = api.readBindingManifest?.() || null;
    if (!binding || binding.declared_mode !== "parent_for_child" || binding.status !== "active") {
      return [];
    }
    const ownerId = typeof binding.account_owner_id === "string" ? binding.account_owner_id : "";
    const subjects = Array.isArray(binding.primary_subject_ids) ? binding.primary_subject_ids : [];
    return subjects.filter(
      (personId) => typeof personId === "string" && personId && personId !== ownerId,
    );
  },

  async _loadBoundSubjectConsents() {
    const subjects = this._boundChildSubjects();
    const boundSubjects = await Promise.all(
      subjects.map(async (personId) => {
        try {
          return {
            personId,
            displayName: "绑定中的孩子",
            consentRows: consentRows(await api.getPersonConsents(personId)),
            loadError: "",
          };
        } catch (error) {
          return {
            personId,
            displayName: "绑定中的孩子",
            consentRows: consentRows(null),
            loadError: personConsentError(error),
          };
        }
      }),
    );
    this.setData({ boundSubjects });
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
    if (!this._gateGuardianConfig()) return;
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
    if (!this._gateGuardianConfig()) return;
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
    if (!this._gateGuardianConfig()) return;
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

  /*
   * person consent 复用已有 grant/revoke 语义，不走尚未接入的决策接口。
   * 授权人校验、幂等和过期都由服务端 person 端点决定。
   */
  async togglePersonConsent(event) {
    if (this.data.working) return;
    const { personId, kind, policyVersion, consentId, active } = event.currentTarget.dataset;
    if (!personId || !kind) return;
    this.setData({ working: true, error: "" });
    try {
      if (active) {
        await api.revokePersonConsent({
          personId,
          consentId,
          idempotencyKey: operationKey("person-revoke"),
        });
      } else {
        await api.grantPersonConsent({
          personId,
          consentKind: kind,
          policyVersion,
          idempotencyKey: operationKey("person-grant"),
        });
      }
      await this._loadBoundSubjectConsents();
    } catch (error) {
      this.setData({ error: personConsentError(error) });
    } finally {
      this.setData({ working: false });
    }
  },

  /*
   * 无账号孩子的数据导出 / 删除。导出是服务端的监护人视图（治理元数据），
   * 不含对话原文，页面也不展示导出内容，只写成文件供发送到微信聊天保存。
   */
  async exportMinorData(event) {
    const personId = event.currentTarget.dataset.personId;
    if (!personId || this.data.working) return;
    this.setData({ working: true, error: "", minorExport: null });
    try {
      const exported = await api.exportGuardianMinorData(personId);
      const fileName = `memoria-child-export-${Date.now()}.json`;
      const filePath = `${wx.env.USER_DATA_PATH}/${fileName}`;
      await new Promise((resolve, reject) => {
        wx.getFileSystemManager().writeFile({
          filePath,
          data: JSON.stringify(exported ?? {}, null, 2),
          encoding: "utf8",
          success: resolve,
          fail: reject,
        });
      });
      this.setData({ minorExport: { personId, filePath, fileName } });
      wx.showToast({ title: "导出文件已生成", icon: "success" });
    } catch (error) {
      this.setData({ error: minorDataError(error, "导出失败，请稍后重试。") });
    } finally {
      this.setData({ working: false });
    }
  },

  /* 分享文件必须由点击触发，所以导出完成后单独给一个发送按钮。 */
  shareMinorExport() {
    const exported = this.data.minorExport;
    if (!exported?.filePath) return;
    wx.shareFileMessage({
      filePath: exported.filePath,
      fileName: exported.fileName,
      fail: () => wx.showToast({ title: "发送未完成", icon: "none" }),
    });
  },

  openMinorDelete(event) {
    const personId = event.currentTarget.dataset.personId;
    if (!personId || this.data.working) return;
    this.setData({
      showMinorDelete: true,
      minorDeletePersonId: personId,
      minorDeleteConfirmation: "",
      minorDeleteError: "",
    });
  },

  closeMinorDelete() {
    if (this.data.minorDeleting) return;
    this.setData({ showMinorDelete: false, minorDeletePersonId: "", minorDeleteConfirmation: "" });
  },

  noop() {},

  onMinorDeleteConfirmation(event) {
    this.setData({ minorDeleteConfirmation: event.detail.value, minorDeleteError: "" });
  },

  async confirmMinorDelete() {
    const { minorDeletePersonId, minorDeleteConfirmation, minorDeleting } = this.data;
    if (minorDeleting || !minorDeletePersonId) return;
    if (minorDeleteConfirmation !== api.GUARDIAN_MINOR_DELETE_CONFIRMATION) {
      this.setData({
        minorDeleteError: `请完整输入「${api.GUARDIAN_MINOR_DELETE_CONFIRMATION}」。`,
      });
      return;
    }
    this.setData({ minorDeleting: true, minorDeleteError: "" });
    try {
      await api.deleteGuardianMinorData(minorDeletePersonId, {
        confirmation: minorDeleteConfirmation,
      });
      this.setData({
        showMinorDelete: false,
        minorDeletePersonId: "",
        minorDeleteConfirmation: "",
        minorExport: null,
      });
      wx.showToast({ title: "已删除 TA 的数据", icon: "success" });
      await this._loadBoundSubjectConsents();
    } catch (error) {
      this.setData({ minorDeleteError: minorDataError(error, "删除失败，请稍后重试。") });
    } finally {
      this.setData({ minorDeleting: false });
    }
  },

  selectMinor(event) {
    return this.selectMinorById(event.currentTarget.dataset.minorId);
  },

  async selectMinorById(minorUserId) {
    if (!minorUserId) return;
    if (!(await this._gateGuardian())) return;
    this.setData({ selectedMinorId: minorUserId, summaryState: "loading", summary: null });
    try {
      const summary = await api.getGuardianSummary(minorUserId);
      this.setData({ summary, summaryState: summary.hasData ? "ready" : "empty" });
    } catch (error) {
      this.setData({ summaryState: guardianSummaryErrorState(error) });
    }
  },
});
