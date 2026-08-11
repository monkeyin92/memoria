const api = require("../../utils/api");
const { requireLogin } = require("../../utils/auth-gate");
const {
  MODE_META,
  readBindingManifest,
  sensitiveEntriesFor,
  degradationFor,
  isNewerRuntimeProfile,
} = require("../../utils/device-binding");
const { readOnboardingSessionId } = require("../../utils/device-onboarding/session-store");

const ACTIVATION_LABELS = Object.freeze({
  pending_manifest: "等待配置",
  manifest_ready: "等待机器人拉取",
  device_downloading: "机器人同步中",
  device_applied: "设备已应用",
  device_acknowledged: "设备已确认",
  ready_for_conversation: "可开始对话",
  failed: "激活失败",
  expired: "激活已过期",
  unknown: "状态待同步",
});

function activationLabel(status) {
  return ACTIVATION_LABELS[status] || "状态待同步";
}

function deviceStatusSummary(activation, profile) {
  const ready = activation?.status === "ready_for_conversation";
  return {
    onlineLabel: ready
      ? "在线，可直接对话"
      : activation?.network?.internet === true
        ? "已联网，等待激活"
        : activation
          ? "暂未确认在线"
          : profile
            ? "绑定已确认，设备状态待同步"
            : "状态暂不可用",
    firmwareVersion: activation?.firmware_version || "未读取",
    networkLabel: activation?.network?.status || "未读取",
    activationLabel: activationLabel(activation?.status),
  };
}

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
    activation: null,
    onlineLabel: "状态待同步",
    firmwareVersion: "未读取",
    networkLabel: "未读取",
    activationLabel: "状态待同步",
    hasPendingOnboarding: false,
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
      activation: null,
      onlineLabel: "状态待同步",
      firmwareVersion: "未读取",
      networkLabel: "未读取",
      activationLabel: "状态待同步",
      hasPendingOnboarding: false,
    });
  },

  async loadDevice() {
    const flowSeq = (this._flowSeq = (this._flowSeq || 0) + 1);
    this.setData({ loading: true, error: "" });
    const binding = readBindingManifest();
    if (!binding || typeof binding.device_id !== "string") {
      let hasPendingOnboarding = false;
      try {
        hasPendingOnboarding = Boolean(readOnboardingSessionId());
      } catch {
        hasPendingOnboarding = false;
      }
      this.setData({ loading: false, hasBinding: false, hasPendingOnboarding });
      return;
    }
    try {
      const [profileResult, activationResult] = await Promise.allSettled([
        api.getRuntimeProfile(binding.device_id),
        api.getActivationStatus(binding.device_id),
      ]);
      if (flowSeq !== this._flowSeq) return; // 晚到响应丢弃
      const profile =
        profileResult.status === "fulfilled" && profileResult.value !== null
          ? profileResult.value
          : null;
      const activation = activationResult.status === "fulfilled" ? activationResult.value : null;
      const needResolution =
        profile?.degraded || profile?.service_mode === "family_shared";
      const resolution = needResolution
        ? await api.resolveSessionSubject({ deviceId: binding.device_id })
        : null;
      if (flowSeq !== this._flowSeq) return;
      const candidates = resolution?.candidate_subjects || [];
      const summary = deviceStatusSummary(activation, profile);
      const failures = [profileResult, activationResult].filter(
        (result) => result.status === "rejected",
      );
      this.setData({
        hasBinding: true,
        binding,
        bindingModeLabel: MODE_META[binding.declared_mode]?.title || "已绑定设备",
        bindingRoles: uniqueLabels(roleLabels(binding.roles || [])),
        profile,
        resolution,
        candidates,
        selectedCandidateId:
          candidates.find((candidate) => candidate.person_id === profile?.active_subject_id)
            ?.person_id || "",
        canConfirmWithApp: (resolution?.allowed_confirmation_methods || []).includes(
          "app_confirm",
        ),
        degradation: profile ? degradationFor(profile) : null,
        sensitiveEntries: profile ? sensitiveEntriesFor(profile) : [],
        currentUserLabel: currentUserLabel(profile, candidates),
        activation,
        onlineLabel: summary.onlineLabel,
        firmwareVersion: summary.firmwareVersion,
        networkLabel: summary.networkLabel,
        activationLabel: summary.activationLabel,
        error:
          failures.length === 2
            ? "设备状态暂时无法读取，绑定关系仍保留；请稍后下拉刷新。"
            : "",
      });
    } catch (error) {
      this.setData({
        hasBinding: true,
        error: error?.message || "设备信息加载失败。",
      });
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

  openOnboarding() {
    wx.navigateTo({ url: "/pages/device-onboarding/index" });
  },

  resumeOnboarding() {
    let sessionId = "";
    try {
      sessionId = readOnboardingSessionId();
    } catch {
      sessionId = "";
    }
    if (!sessionId) {
      wx.showToast({ title: "没有可恢复的启用会话", icon: "none" });
      return;
    }
    wx.navigateTo({
      url: `/pages/device-onboarding/index?session_id=${encodeURIComponent(sessionId)}`,
    });
  },

  openReprovision() {
    const deviceId = this.data.binding?.device_id;
    const query = deviceId ? `&device_id=${encodeURIComponent(deviceId)}` : "";
    wx.navigateTo({ url: `/pages/device-onboarding/index?mode=reprovision${query}` });
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
