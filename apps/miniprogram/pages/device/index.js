const api = require("../../utils/api");
const { requireLogin } = require("../../utils/auth-gate");
const {
  MODE_META,
  readBindingManifest,
  sensitiveEntriesFor,
  degradationFor,
  isNewerRuntimeProfile,
} = require("../../utils/device-binding");

const ROLE_LABELS = Object.freeze({
  account_owner: "账号持有人",
  device_admin: "设备管理员",
  primary_subject: "主要使用者",
  guardian: "监护人",
  delegate: "代理人",
  emergency_contact: "紧急联系人",
  member: "家庭成员",
});

function roleLabels(roles) {
  if (!Array.isArray(roles)) return [];
  return roles
    .filter(
      (role) =>
        role &&
        typeof role === "object" &&
        typeof role.role === "string" &&
        role.status !== "superseded" &&
        role.status !== "revoked" &&
        role.status !== "expired",
    )
    .map((role) => ROLE_LABELS[role.role] || role.role);
}

function uniqueLabels(labels) {
  return [...new Set(labels)];
}

function currentUserLabel(profile, candidates) {
  const activeId = profile?.active_subject_id;
  if (!activeId) return "";
  const match = (candidates || []).find((candidate) => candidate.person_id === activeId);
  return match?.display_name || "已确认的使用者";
}

Page({
  data: {
    loading: true,
    error: "",
    hasBinding: false,
    binding: null,
    bindingModeLabel: "",
    bindingRoles: [],
    profile: null,
    resolution: null,
    candidates: [],
    selectedCandidateId: "",
    canConfirmWithApp: false,
    switching: false,
    degradation: null,
    sensitiveEntries: [],
    currentUserLabel: "",
  },

  onLoad() {
    const app = getApp();
    if (typeof app?.subscribeAuthCleared === "function") {
      this._unsubscribeAuthCleared = app.subscribeAuthCleared(() => this._enterGuestState());
    }
  },

  async onShow() {
    if (!(await requireLogin({ reason: "manage_device" }))) {
      this._enterGuestState();
      return;
    }
    await this.loadDevice();
  },

  onUnload() {
    if (this._unsubscribeAuthCleared) {
      this._unsubscribeAuthCleared();
      this._unsubscribeAuthCleared = null;
    }
  },

  _enterGuestState() {
    this.setData({
      loading: false,
      error: "",
      hasBinding: false,
      binding: null,
      bindingModeLabel: "",
      bindingRoles: [],
      profile: null,
      resolution: null,
      candidates: [],
      selectedCandidateId: "",
      canConfirmWithApp: false,
      switching: false,
      degradation: null,
      sensitiveEntries: [],
      currentUserLabel: "",
    });
  },

  async loadDevice() {
    const flowSeq = (this._flowSeq = (this._flowSeq || 0) + 1);
    this.setData({ loading: true, error: "" });
    const binding = readBindingManifest();
    if (!binding || typeof binding.device_id !== "string") {
      this.setData({ loading: false, hasBinding: false });
      return;
    }
    try {
      const profile = await api.getRuntimeProfile(binding.device_id);
      if (profile === null || flowSeq !== this._flowSeq) return; // 晚到响应丢弃
      const needResolution =
        profile.degraded || profile.service_mode === "family_shared";
      const resolution = needResolution
        ? await api.resolveSessionSubject({ deviceId: binding.device_id })
        : null;
      if (flowSeq !== this._flowSeq) return;
      const candidates = resolution?.candidate_subjects || [];
      this.setData({
        hasBinding: true,
        binding,
        bindingModeLabel: MODE_META[binding.declared_mode]?.title || "已绑定设备",
        bindingRoles: uniqueLabels(roleLabels(binding.roles || [])),
        profile,
        resolution,
        candidates,
        selectedCandidateId:
          candidates.find((candidate) => candidate.person_id === profile.active_subject_id)
            ?.person_id || "",
        canConfirmWithApp: (resolution?.allowed_confirmation_methods || []).includes(
          "app_confirm",
        ),
        degradation: degradationFor(profile),
        sensitiveEntries: sensitiveEntriesFor(profile),
        currentUserLabel: currentUserLabel(profile, candidates),
        error: "",
      });
    } catch (error) {
      this.setData({ hasBinding: true, error: error?.message || "设备信息加载失败。" });
    } finally {
      this.setData({ loading: false });
    }
  },

  onPullDownRefresh() {
    if (!api.hasAuthenticatedSession()) {
      wx.stopPullDownRefresh();
      return;
    }
    this.loadDevice().finally(() => wx.stopPullDownRefresh());
  },

  openBindPage() {
    wx.navigateTo({ url: "/pages/bind/index" });
  },

  selectCandidate(event) {
    const personId = event.currentTarget.dataset.personId;
    if (!personId) return;
    this.setData({
      selectedCandidateId: personId,
      error: "",
    });
  },

  async confirmSubject() {
    const flowSeq = (this._flowSeq = (this._flowSeq || 0) + 1);
    const { selectedCandidateId, switching, profile, resolution } = this.data;
    if (switching || !selectedCandidateId) return;
    if (!(resolution?.allowed_confirmation_methods || []).includes("app_confirm")) {
      this.setData({ error: "当前会话需要通过语音确认身份，暂时不能在应用里切换。" });
      return;
    }
    if (!profile?.session_id) {
      this.setData({ error: "请先开始一次语音对话，再切换当前使用者。" });
      return;
    }
    this.setData({ switching: true, error: "" });
    try {
      const nextProfile = await api.setActiveSubject(profile.session_id, {
        personId: selectedCandidateId,
        confirmationMethod: "app_confirm",
      });
      if (nextProfile === null || flowSeq !== this._flowSeq) {
        this.setData({ error: "切换结果已过期，请下拉刷新后重试。" });
        return;
      }
      if (nextProfile.valid !== true) {
        this.setData({
          error: "服务端返回的 Runtime Profile 校验失败，切换未生效。",
        });
        return;
      }
      if (!isNewerRuntimeProfile(profile, nextProfile)) {
        // 只接受同一会话中 epoch 严格提升的新 profile。
        this.setData({ error: "服务端返回的会话版本未提升，已拒绝应用。" });
        return;
      }
      const needResolution =
        nextProfile.degraded || nextProfile.service_mode === "family_shared";
      const resolutionNext = needResolution
        ? await api.resolveSessionSubject({ deviceId: this.data.binding.device_id })
        : null;
      if (flowSeq !== this._flowSeq) return;
      const candidates = resolutionNext
        ? resolutionNext.candidate_subjects || []
        : this.data.candidates;
      this.setData({
        profile: nextProfile,
        resolution: resolutionNext,
        candidates,
        selectedCandidateId:
          candidates.find(
            (candidate) => candidate.person_id === nextProfile.active_subject_id,
          )?.person_id || "",
        canConfirmWithApp: (resolutionNext?.allowed_confirmation_methods || []).includes(
          "app_confirm",
        ),
        degradation: degradationFor(nextProfile),
        sensitiveEntries: sensitiveEntriesFor(nextProfile),
        currentUserLabel: currentUserLabel(nextProfile, candidates),
      });
      wx.showToast({ title: "已切换使用者", icon: "success" });
    } catch (error) {
      this.setData({ error: error?.message || "切换失败，请稍后重试。" });
    } finally {
      this.setData({ switching: false });
    }
  },

  openEntry(event) {
    const page = event.currentTarget.dataset.page;
    if (typeof page !== "string" || !page.startsWith("/pages/")) return;
    wx.navigateTo({ url: page });
  },
});
