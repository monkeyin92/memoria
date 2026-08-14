const api = require("../../utils/api");
const { requireLogin } = require("../../utils/auth-gate");
const {
  MODE_META,
  readBindingManifest,
  sensitiveEntriesFor,
  degradationFor,
  isNewerRuntimeProfile,
} = require("../../utils/device-binding");
const { deviceStatusSummary } = require("../../utils/device-status");
const { readOnboardingSessionId } = require("../../utils/device-onboarding/session-store");


const ROLE_LABELS = Object.freeze({
  account_owner: "账号持有人",
  device_admin: "设备管理员",
  primary_subject: "主要使用者",
  guardian: "监护人",
  delegate: "代理人",
  emergency_contact: "紧急联系人",
  member: "家庭成员",
});

// 音频模式/唤醒方式/打断方式取值与 services/control_api/app/device_control.py
// 的合同保持一致；全双工是否可选由服务端声学能力登记决定，客户端不自行开放。
const AUDIO_MODE_LABELS = Object.freeze({
  full_duplex_verified: "全双工（服务端已登记声学验收）",
  interrupt_assist: "打断辅助",
  half_duplex_safe: "半双工安全模式",
});

const WAKE_MODE_OPTIONS = Object.freeze([
  { value: "button", label: "按键唤醒" },
  { value: "keyword", label: "唤醒词唤醒" },
  { value: "button_or_keyword", label: "按键或唤醒词" },
]);

const ALL_BARGE_IN_OPTIONS = Object.freeze([
  { value: "none", label: "无" },
  { value: "button", label: "物理按键" },
  { value: "keyword", label: "本地停止词" },
  { value: "voice", label: "语音打断" },
]);

function clampPercent(value) {
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed < 0 || parsed > 100) return null;
  return parsed;
}

function indexOfOption(options, value) {
  const index = options.findIndex((option) => option.value === value);
  return index >= 0 ? index : 0;
}

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
    settings: null,
    settingsSaving: false,
    settingsError: "",
    diagnostics: null,
    diagnosticsUnavailable: false,
    acousticCapability: null,
    acousticVerified: false,
    audioModeOptions: [],
    currentAudioModeLabel: "未读取",
    effectiveAudioModeLabel: "未连接，暂无实际模式",
    liveRuntimeStatusLabel: "未读取",
    audioModeIndex: 0,
    wakeModeOptions: WAKE_MODE_OPTIONS,
    wakeModeIndex: 0,
    wakeModeLabel: "未读取",
    bargeInOptions: ALL_BARGE_IN_OPTIONS,
    bargeInChecked: {},
    allowedAudioModesLabel: "",
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
      settings: null,
      settingsSaving: false,
      settingsError: "",
      diagnostics: null,
      diagnosticsUnavailable: false,
      acousticCapability: null,
      acousticVerified: false,
      audioModeOptions: [],
      currentAudioModeLabel: "未读取",
      effectiveAudioModeLabel: "未连接，暂无实际模式",
      liveRuntimeStatusLabel: "未读取",
      audioModeIndex: 0,
      wakeModeIndex: 0,
      wakeModeLabel: "未读取",
      bargeInOptions: ALL_BARGE_IN_OPTIONS,
      bargeInChecked: {},
      allowedAudioModesLabel: "",
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
      const [profileResult, activationResult, settingsResult, diagnosticsResult] =
        await Promise.allSettled([
          api.getRuntimeProfile(binding.device_id),
          api.getActivationStatus(binding.device_id),
          api.getDeviceSettings(binding.device_id),
          api.getDeviceDiagnostics(binding.device_id),
        ]);
      if (flowSeq !== this._flowSeq) return; // 晚到响应丢弃
      const profile =
        profileResult.status === "fulfilled" && profileResult.value !== null
          ? profileResult.value
          : null;
      const activation = activationResult.status === "fulfilled" ? activationResult.value : null;
      const settings =
        settingsResult.status === "fulfilled" && settingsResult.value !== null
          ? settingsResult.value
          : null;
      const diagnostics =
        diagnosticsResult.status === "fulfilled" &&
        diagnosticsResult.value !== null
          ? diagnosticsResult.value
          : null;
      const acousticCapability = diagnostics?.acoustic_capability || null;
      const acousticVerified = acousticCapability?.aec_verified === true;
      const allowedModes = Array.isArray(diagnostics?.allowed_audio_modes)
        ? diagnostics.allowed_audio_modes
        : [];
      const audioModeOptions = allowedModes.map((mode) => ({
        value: mode,
        label: AUDIO_MODE_LABELS[mode] || mode,
      }));
      // 语音打断依赖已验收的 AEC；未验收或诊断不可用时 fail-closed 隐藏。
      const bargeInOptions = ALL_BARGE_IN_OPTIONS.filter(
        (option) => option.value !== "voice" || acousticVerified,
      );
      const bargeInKinds = Array.isArray(settings?.allowed_barge_in)
        ? settings.allowed_barge_in
        : [];
      const bargeInChecked = {};
      for (const kind of bargeInKinds) bargeInChecked[kind] = true;
      const wakeModeIndex = indexOfOption(WAKE_MODE_OPTIONS, settings?.wake_mode);
      const audioModeIndex = indexOfOption(audioModeOptions, settings?.audio_mode);
      const liveRuntime = diagnostics?.live_runtime || null;
      const effectiveAudioModeLabel =
        liveRuntime?.connected === true && liveRuntime?.audio_mode_effective
          ? AUDIO_MODE_LABELS[liveRuntime.audio_mode_effective] ||
            liveRuntime.audio_mode_effective
          : liveRuntime?.connected === false
            ? "未连接，暂无实际模式"
            : "Edge 状态暂不可用";
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
        settings,
        settingsError: "",
        diagnostics,
        diagnosticsUnavailable: diagnostics === null,
        acousticCapability,
        acousticVerified,
        audioModeOptions,
        currentAudioModeLabel: settings?.audio_mode
          ? AUDIO_MODE_LABELS[settings.audio_mode] || settings.audio_mode
          : "未读取",
        effectiveAudioModeLabel,
        liveRuntimeStatusLabel:
          liveRuntime?.connected === true
            ? `已连接 · epoch ${liveRuntime.stream_epoch}`
            : liveRuntime?.connected === false
              ? "当前未连接"
              : "状态暂不可用",
        audioModeIndex,
        wakeModeIndex,
        wakeModeLabel: settings?.wake_mode
          ? WAKE_MODE_OPTIONS.find((option) => option.value === settings.wake_mode)?.label ||
            settings.wake_mode
          : "未读取",
        bargeInOptions,
        bargeInChecked,
        allowedAudioModesLabel: audioModeOptions.map((option) => option.label).join(" / "),
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
      this.setData({ error: "请先在机器人上开始对话，再回来切换当前使用者。" });
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

  async changeVolume(event) {
    const value = clampPercent(event.detail.value);
    if (value === null) return;
    await this.saveDeviceSetting({ volume_limit: value });
  },

  async changeBrightness(event) {
    const value = clampPercent(event.detail.value);
    if (value === null) return;
    await this.saveDeviceSetting({ screen_brightness: value });
  },

  async selectAudioMode(event) {
    const option = this.data.audioModeOptions[Number(event.detail.value)];
    if (!option) return;
    await this.saveDeviceSetting({ audio_mode: option.value });
  },

  async selectWakeMode(event) {
    const option = WAKE_MODE_OPTIONS[Number(event.detail.value)];
    if (!option) return;
    await this.saveDeviceSetting({ wake_mode: option.value });
  },

  async toggleBargeIn(event) {
    const value = Array.isArray(event.detail.value) ? event.detail.value : [];
    const kinds = value.filter((kind) =>
      ALL_BARGE_IN_OPTIONS.some((option) => option.value === kind),
    );
    await this.saveDeviceSetting({ allowed_barge_in: kinds });
  },

  async saveDeviceSetting(changes) {
    const binding = this.data.binding;
    const current = this.data.settings;
    if (!binding || !current || this.data.settingsSaving) return;
    if (
      changes.allowed_barge_in !== undefined &&
      (!Array.isArray(changes.allowed_barge_in) || changes.allowed_barge_in.length === 0)
    ) {
      wx.showToast({ title: "至少保留一种打断方式", icon: "none" });
      return;
    }
    const previous = { ...current };
    this.setData({ settingsSaving: true, settingsError: "", error: "" });
    try {
      const updated = await api.updateDeviceSettings(binding.device_id, changes, {
        expectedVersion: current.settings_version,
      });
      this.setData({
        settings: updated,
        currentAudioModeLabel: updated.audio_mode
          ? AUDIO_MODE_LABELS[updated.audio_mode] || updated.audio_mode
          : "未读取",
        wakeModeLabel:
          WAKE_MODE_OPTIONS.find((option) => option.value === updated.wake_mode)?.label ||
          updated.wake_mode ||
          "未读取",
        audioModeIndex: indexOfOption(this.data.audioModeOptions, updated.audio_mode),
        wakeModeIndex: indexOfOption(WAKE_MODE_OPTIONS, updated.wake_mode),
        bargeInChecked: (() => {
          const checked = {};
          for (const kind of Array.isArray(updated.allowed_barge_in)
            ? updated.allowed_barge_in
            : []) {
            checked[kind] = true;
          }
          return checked;
        })(),
      });
    } catch (error) {
      // 服务端没有确认就不保留本地假状态；重新展示最后一个权威版本。
      this.setData({
        settings: previous,
        settingsError: error?.message || "设备设置未能保存，请刷新后重试。",
      });
    } finally {
      this.setData({ settingsSaving: false });
    }
  },
});
