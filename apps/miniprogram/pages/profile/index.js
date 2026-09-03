const api = require("../../utils/api");
const compliance = require("../../utils/compliance");
const { companions, companionById, defaultCompanionId } = require("../../utils/companions");
const { requireLogin } = require("../../utils/auth-gate");
const { readBindingManifest } = require("../../utils/device-binding");
const contracts = require("../../utils/multi-subject-contracts");

const defaultProfile = {
  display_name: "新朋友",
  auto_summary: true,
  gentle_reminders: false,
  reject_non_owner_voice: true,
  companion_id: defaultCompanionId,
  subject_category: null,
};

const DELETE_CONFIRMATION_TEXT = "永久删除我的全部数据";

function profileFaceStyleFor(companionId) {
  const face = companionById(companionId).face;
  return (
    `left:${face.left};top:${face.top};width:${face.width};height:${face.height};` +
    `--profile-face-ink:${face.ink};--profile-eye-top:${face.eyeTop};` +
    `--profile-eye-bottom:${face.eyeBottom};--profile-eye-glow:${face.glow};`
  );
}

function companionFaceStyleFor(companion) {
  const face = companion.face;
  return (
    `left:${face.left};top:${face.top};width:${face.width};height:${face.height};` +
    `--companion-face-ink:${face.ink};--companion-eye-top:${face.eyeTop};` +
    `--companion-eye-bottom:${face.eyeBottom};--companion-eye-glow:${face.glow};`
  );
}

const companionCards = companions.map((companion) => ({
  ...companion,
  faceStyle: companionFaceStyleFor(companion),
}));

function formatDate(date) {
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${date.getFullYear()}-${month}-${day}`;
}

function computeStats(items) {
  const days = (items || []).map((item) => ({
    date: item.day || item.date || "",
    count: item.message_count || 0,
  }));
  const totalDays = days.length;
  const moments = days.reduce((sum, item) => sum + item.count, 0);
  const dates = new Set(days.map((item) => item.date));
  const cursor = new Date();
  if (!dates.has(formatDate(cursor))) cursor.setDate(cursor.getDate() - 1);
  let streak = 0;
  while (dates.has(formatDate(cursor))) {
    streak += 1;
    cursor.setDate(cursor.getDate() - 1);
  }
  return { totalDays, moments, streak };
}

function formatDeliveredCapabilityRows(payload) {
  if (!payload || typeof payload !== "object") return [];
  return [
    { label: "微信已绑定", value: payload.wechat_bound ? "是" : "否" },
    { label: "手机号已验证", value: payload.wechat_phone_verified ? "是" : "否" },
    { label: "已绑定设备", value: String(payload.devices_bound ?? 0) },
    {
      label: "主人声纹",
      value: `${payload.speaker_profiles_active ?? 0} 个活跃`,
    },
    {
      label: "档案活跃天数",
      value: String(payload.memory_days_with_activity ?? 0),
    },
    {
      label: "档案消息条数",
      value: String(payload.total_messages ?? 0),
    },
    {
      label: "访客声纹拦截",
      value: payload.reject_non_owner_voice ? "开启" : "关闭",
    },
    {
      label: "半双工口径",
      value: payload.advertised_duplex_level || "none",
    },
    {
      label: "模型训练贡献",
      value: payload.model_training_contribution_enabled ? "开启" : "默认关闭",
    },
    {
      label: "数据导出",
      value: payload.account_export_available ? "可用" : "不可用",
    },
    {
      label: "账号注销",
      value: payload.account_deletion_available ? "可用" : "不可用",
    },
  ];
}

Page({
  data: {
    identity: null,
    profile: defaultProfile,
    profileFaceStyle: profileFaceStyleFor(defaultCompanionId),
    companions: companionCards,
    stats: { totalDays: 0, moments: 0, streak: 0 },
    loading: false,
    saving: false,
    error: "",
    showDelete: false,
    deleteConfirmation: "",
    deleting: false,
    deleteError: "",
    deleteConfirmText: DELETE_CONFIRMATION_TEXT,
    authenticated: false,
    isMinor: false,
    hasRuntimeProfile: false,
    runtimeCapabilities: [],
    speakerEntryAllowed: false,
    digitalSelfEntryAllowed: false,
    guardianEntryAllowed: false,
    rawVoiceEntryAllowed: false,
    profileUnavailableReason: "",
    speakerEnrollmentState: "blocked",
    speakerEnrollmentBlockReason: "",
    speakerEnrollmentProfileCount: 0,
    deliveredCapabilities: [],
    deliveredCapabilitiesLoading: false,
    complianceCopy: {
      positioning: compliance.PRODUCT_POSITIONING,
      aiDisclosure: compliance.AI_DISCLOSURE,
      trainingDefaultOff: compliance.TRAINING_DEFAULT_OFF,
      minorRestrictions: compliance.MINOR_RESTRICTIONS,
    },
  },

  onLoad() {
    const app = getApp();
    if (typeof app?.subscribeAuthCleared === "function") {
      this._unsubscribeAuthCleared = app.subscribeAuthCleared(() => this._enterGuestState());
    }
  },

  onShow() {
    const authenticated = api.hasAuthenticatedSession();
    this.setData({ authenticated });
    if (!authenticated) {
      this._enterGuestState();
      return;
    }
    this.loadProfile();
    this.loadStats();
    this.loadDeliveredCapabilities();
  },

  onUnload() {
    if (this._unsubscribeAuthCleared) {
      this._unsubscribeAuthCleared();
      this._unsubscribeAuthCleared = null;
    }
  },

  _enterGuestState() {
    this.setData({
      authenticated: false,
      identity: null,
      profile: defaultProfile,
      profileFaceStyle: profileFaceStyleFor(defaultCompanionId),
      stats: { totalDays: 0, moments: 0, streak: 0 },
      saving: false,
      error: "",
      showDelete: false,
      deleteConfirmation: "",
      deleting: false,
      deleteError: "",
      isMinor: false,
      hasRuntimeProfile: false,
      runtimeCapabilities: [],
      speakerEntryAllowed: false,
      digitalSelfEntryAllowed: false,
      guardianEntryAllowed: false,
      rawVoiceEntryAllowed: false,
      profileUnavailableReason: "",
      speakerEnrollmentState: "blocked",
      speakerEnrollmentBlockReason: "",
      speakerEnrollmentRemediationSteps: [],
      speakerEnrollmentProfileCount: 0,
      deliveredCapabilities: [],
      deliveredCapabilitiesLoading: false,
    });
  },

  async loadProfile() {
    const identity = api.currentIdentity();
    if (!identity) return;
    const authEpoch = api.currentAuthEpoch();
    this.setData({ loading: true, identity, error: "" });
    try {
      const profile = { ...defaultProfile, ...(await api.getProfile(identity.user_id)) };
      if (!api.isAuthEpochCurrent(authEpoch)) return;
      const [capabilityState, speakerState] = await Promise.all([
        this.loadRuntimeCapabilities(),
        this.loadSpeakerEnrollmentStatus(),
      ]);
      if (!api.isAuthEpochCurrent(authEpoch)) return;
      this.setData({
        profile,
        profileFaceStyle: profileFaceStyleFor(profile.companion_id),
        isMinor: profile.subject_category === "minor",
        ...capabilityState,
        ...speakerState,
      });
    } catch (error) {
      if (!api.isAuthEpochCurrent(authEpoch)) return;
      this.setData({ error: error?.message || "个人资料无法加载。" });
    } finally {
      if (api.isAuthEpochCurrent(authEpoch)) this.setData({ loading: false });
    }
  },

  async loadSpeakerEnrollmentStatus() {
    try {
      const status = await api.getSpeakerEnrollmentStatus();
      const enrollment = status?.enrollment || {};
      const blockReason = {
        account_not_registered: "当前账户还没有完成注册。",
        subject_category_unavailable: "服务端还没有确认当前主体，需先完成主体资料认证。",
        subject_capability_forbidden: "当前主体尚未满足主人声纹所需的已验证成人条件。",
        minor_forbidden: "未成年人主体不能登记主人声纹。",
      }[status?.block_code] || "服务端暂未授权主人声纹能力。";
      return {
        speakerEnrollmentState: enrollment.state || "blocked",
        speakerEnrollmentBlockReason:
          enrollment.state === "blocked" ? blockReason : "",
        speakerEnrollmentRemediationSteps: status?.remediation?.steps || [],
        speakerEnrollmentProfileCount: Number(enrollment.profile_count || 0),
      };
    } catch (error) {
      return {
        speakerEnrollmentState: "blocked",
        speakerEnrollmentBlockReason: error?.message || "主人声纹状态暂时无法获取。",
        speakerEnrollmentProfileCount: 0,
      };
    }
  },

  async startSpeakerEnrollment() {
    if (!(await requireLogin({ reason: "start_speaker_enrollment" }))) return;
    const binding = readBindingManifest();
    if (!binding || typeof binding.device_id !== "string") {
      wx.showToast({ title: "请先绑定设备", icon: "none" });
      this.openDevice();
      return;
    }
    try {
      await api.createSpeakerEnrollmentIntent();
      wx.showToast({ title: "请唤醒设备说话", icon: "success" });
      const status = await this.loadSpeakerEnrollmentStatus();
      this.setData(status);
    } catch (error) {
      wx.showToast({ title: error?.message || "暂时无法请求设备登记", icon: "none" });
    }
  },

  async loadRuntimeCapabilities() {
    const binding = readBindingManifest();
    if (!binding || typeof binding.device_id !== "string") {
      return {
        hasRuntimeProfile: false,
        runtimeCapabilities: [],
        speakerEntryAllowed: false,
        digitalSelfEntryAllowed: false,
        guardianEntryAllowed: false,
        rawVoiceEntryAllowed: false,
        profileUnavailableReason: "还没有绑定设备，无法取得 Runtime Profile。",
      };
    }
    try {
      const profile = await api.getRuntimeProfile(binding.device_id);
      if (profile === null) {
        // 晚到响应：保持现有状态，不覆盖更新的结果。
        return this._lastCapabilityState || {
          hasRuntimeProfile: false,
          runtimeCapabilities: [],
          speakerEntryAllowed: false,
          digitalSelfEntryAllowed: false,
          guardianEntryAllowed: false,
          rawVoiceEntryAllowed: false,
          profileUnavailableReason: "能力状态正在刷新，请稍后重试。",
        };
      }
      if (profile.valid !== true) {
        return {
          hasRuntimeProfile: false,
          runtimeCapabilities: [],
          speakerEntryAllowed: false,
          digitalSelfEntryAllowed: false,
          guardianEntryAllowed: false,
          rawVoiceEntryAllowed: false,
          profileUnavailableReason: "服务端返回的 Runtime Profile 校验失败，敏感能力已关闭。",
        };
      }
      const capabilities = profile?.capabilities || [];
      const state = {
        hasRuntimeProfile: true,
        runtimeCapabilities: capabilities,
        speakerEntryAllowed: capabilities.includes(contracts.Capability.VoiceProfileCreate),
        digitalSelfEntryAllowed: capabilities.includes(contracts.Capability.DigitalSelfPreview),
        guardianEntryAllowed: capabilities.includes(contracts.Capability.GuardianSummaryView),
        rawVoiceEntryAllowed: capabilities.includes(contracts.Capability.RawAudioRetention),
        profileUnavailableReason: "",
      };
      this._lastCapabilityState = state;
      return state;
    } catch {
      return {
        hasRuntimeProfile: false,
        runtimeCapabilities: [],
        speakerEntryAllowed: false,
        digitalSelfEntryAllowed: false,
        guardianEntryAllowed: false,
        rawVoiceEntryAllowed: false,
        profileUnavailableReason: "Runtime Profile 获取失败，敏感能力入口已关闭。",
      };
    }
  },

  async loadDeliveredCapabilities() {
    const authEpoch = api.currentAuthEpoch();
    this.setData({ deliveredCapabilitiesLoading: true });
    try {
      const payload = await api.getDeliveredCapabilities();
      if (!api.isAuthEpochCurrent(authEpoch)) return;
      this.setData({
        deliveredCapabilities: formatDeliveredCapabilityRows(payload),
      });
    } catch {
      if (!api.isAuthEpochCurrent(authEpoch)) return;
      this.setData({ deliveredCapabilities: [] });
    } finally {
      if (api.isAuthEpochCurrent(authEpoch)) {
        this.setData({ deliveredCapabilitiesLoading: false });
      }
    }
  },

  async loadStats() {
    const identity = api.currentIdentity();
    if (!identity) return;
    // 私人回顾统计也是 memory_recall_private 敏感动作：未授权时保持 0，不请求。
    const gate = await api.requireRuntimeCapability(contracts.Capability.MemoryRecallPrivate);
    if (!gate.allowed) {
      this.setData({ stats: { totalDays: 0, moments: 0, streak: 0 } });
      return;
    }
    const authEpoch = api.currentAuthEpoch();
    try {
      const result = await api.getMemoryDays(identity.user_id, 30);
      if (!api.isAuthEpochCurrent(authEpoch)) return;
      this.setData({ stats: computeStats(result.items) });
    } catch {
      // 统计只作展示，失败时保持默认值，不打断页面。
    }
  },

  onSwitch(event) {
    const key = event.currentTarget.dataset.key;
    this.setData({ [`profile.${key}`]: event.detail.value });
  },

  async chooseCompanion(event) {
    if (!(await requireLogin({ reason: "edit_profile" }))) return;
    const companionId = event.currentTarget.dataset.id;
    this.setData({
      "profile.companion_id": companionId,
      profileFaceStyle: profileFaceStyleFor(companionId),
    });
    await this.saveProfile();
  },

  async saveProfile() {
    if (!(await requireLogin({ reason: "edit_profile" }))) return;
    const identity = api.currentIdentity();
    if (!identity || this.data.saving) return;
    const authEpoch = api.currentAuthEpoch();
    this.setData({ saving: true, error: "" });
    try {
      const profile = await api.updateProfile(identity.user_id, this.data.profile);
      if (!api.isAuthEpochCurrent(authEpoch)) return;
      this.setData({ profile: { ...this.data.profile, ...profile } });
      wx.showToast({ title: "已保存", icon: "success" });
    } catch (error) {
      if (!api.isAuthEpochCurrent(authEpoch)) return;
      this.setData({ error: error?.message || "保存失败。" });
    } finally {
      if (api.isAuthEpochCurrent(authEpoch)) this.setData({ saving: false });
    }
  },

  async loginFromProfile() {
    if (!(await requireLogin({ reason: "view_profile" }))) return;
    this.setData({ authenticated: true });
    await Promise.all([this.loadProfile(), this.loadStats(), this.loadDeliveredCapabilities()]);
  },

  async openDigitalSelf() {
    if (!(await requireLogin({ reason: "view_profile" }))) return;
    if (!this._allowSensitiveEntry("数字分身", contracts.Capability.DigitalSelfPreview)) return;
    wx.navigateTo({ url: "/pages/digital-self/index" });
  },


  openDevice() {
    wx.switchTab({ url: "/pages/device/index" });
  },

  async openGuardianSummary() {
    if (!(await requireLogin({ reason: "view_guardian_summary" }))) return;
    if (!this._allowSensitiveEntry("成长小结", contracts.Capability.GuardianSummaryView)) return;
    wx.navigateTo({ url: "/pages/guardian/index" });
  },

  _allowSensitiveEntry(label, capability) {
    if (this.data.runtimeCapabilities.includes(capability)) return true;
    const reason = this.data.profileUnavailableReason || "服务端未按当前主体授权";
    wx.showToast({ title: `${label}暂未开放：${reason}`, icon: "none" });
    return false;
  },

  openPrivacy() {
    if (!this._allowSensitiveEntry("原始语音授权", contracts.Capability.RawAudioRetention)) {
      return;
    }
    wx.navigateTo({ url: "/pages/privacy/index" });
  },

  async _finishLogout() {
    api.logoutLocal();
    this._enterGuestState();
  },

  async logout() {
    try {
      await api.logoutCurrentDevice();
    } catch {
      // 服务端登出失败时仍清理本地会话，保证可以重新登录。
    }
    await this._finishLogout();
  },

  logoutAll() {
    wx.showModal({
      title: "退出所有设备",
      content: "确定要结束所有设备上的登录吗？其他设备需要重新登录。",
      confirmText: "全部退出",
      confirmColor: "#ff6b8a",
      success: async (result) => {
        if (!result.confirm) return;
        try {
          await api.logoutAllDevices();
        } catch {
          // 同上，失败也继续清理本地会话。
        }
        await this._finishLogout();
      },
    });
  },

  openDeleteModal() {
    this.setData({
      showDelete: true,
      deleteConfirmation: "",
      deleteError: "",
    });
  },

  closeDeleteModal() {
    if (this.data.deleting) return;
    this.setData({ showDelete: false });
  },

  noop() {},

  onDeleteConfirmation(event) {
    this.setData({ deleteConfirmation: event.detail.value });
  },

  async confirmDelete() {
    const { deleteConfirmation, deleting } = this.data;
    if (deleting) return;
    if (deleteConfirmation !== DELETE_CONFIRMATION_TEXT) {
      this.setData({ deleteError: `请完整输入「${DELETE_CONFIRMATION_TEXT}」。` });
      return;
    }
    this.setData({ deleting: true, deleteError: "" });
    try {
      await api.requestAccountDeletion({
        confirmation: deleteConfirmation,
      });
      this.setData({ showDelete: false, deleting: false });
      wx.showToast({ title: "注销申请已提交", icon: "success" });
      await this._finishLogout();
    } catch (error) {
      this.setData({
        deleting: false,
        deleteError: error?.message || "注销申请失败，请稍后重试。",
      });
    }
  },
});
