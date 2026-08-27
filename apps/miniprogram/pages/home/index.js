const api = require("../../utils/api");
const { companionById, defaultCompanionId } = require("../../utils/companions");
const { requireLogin } = require("../../utils/auth-gate");
const { readBindingManifest } = require("../../utils/device-binding");
const { deviceStatusSummary } = require("../../utils/device-status");
const contracts = require("../../utils/multi-subject-contracts");

const defaultProfile = {
  display_name: "新朋友",
  companion_id: defaultCompanionId,
  subject_category: null,
};

const LEARNING_MODE_OPTIONS = Object.freeze([
  { value: "off", label: "自在陪伴" },
  { value: "tutor_english", label: "英语口语" },
  { value: "tutor_homework", label: "作业陪伴" },
]);

function mascotAssetsFor(companion) {
  const face = companion.face || {};
  return {
    faceStyle:
      `left:${face.left};top:${face.top};width:${face.width};height:${face.height};` +
      `--face-ink:${face.ink};--eye-top:${face.eyeTop};--eye-bottom:${face.eyeBottom};--eye-glow:${face.glow};`,
    faceTone: face.tone || "light",
    hasChest: Boolean(companion.chest),
    chestStyle: companion.chest ? `top:${companion.chest.top};` : "",
  };
}

function greeting() {
  const hour = new Date().getHours();
  if (hour < 6) return "夜深了";
  if (hour < 11) return "早上好";
  if (hour < 14) return "中午好";
  if (hour < 18) return "下午好";
  return "晚上好";
}

function todayString() {
  const date = new Date();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${date.getFullYear()}-${month}-${day}`;
}

// 小程序已退出实时语音：首页只消费 Control API 的普通 HTTPS 数据，
// 不创建任何录音器、不连接媒体 WSS、不播放实时 TTS、不加入 LiveKit。
// 对话发生在机器人上；这里只展示服务端权威的设备与回顾状态。
Page({
  data: {
    greeting: greeting(),
    profile: defaultProfile,
    companion: companionById(defaultCompanionId),
    authenticated: false,
    loading: false,
    error: "",
    hasBinding: false,
    device: null,
    persona: null,
    activeSubjectLabel: "",
    memoryAllowed: false,
    todayCount: null,
    recentSummary: "",
    guardianNoticeCount: 0,
    settings: null,
    settingsSaving: false,
    learningModeOptions: LEARNING_MODE_OPTIONS,
    ...mascotAssetsFor(companionById(defaultCompanionId)),
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
    this.loadDashboard();
  },

  onUnload() {
    if (this._unsubscribeAuthCleared) {
      this._unsubscribeAuthCleared();
      this._unsubscribeAuthCleared = null;
    }
  },

  onPullDownRefresh() {
    if (!api.hasAuthenticatedSession()) {
      wx.stopPullDownRefresh();
      return;
    }
    this.loadDashboard().finally(() => wx.stopPullDownRefresh());
  },

  _enterGuestState() {
    const companion = companionById(defaultCompanionId);
    this.setData({
      authenticated: false,
      loading: false,
      error: "",
      profile: defaultProfile,
      companion,
      ...mascotAssetsFor(companion),
      hasBinding: false,
      device: null,
      persona: null,
      activeSubjectLabel: "",
      memoryAllowed: false,
      todayCount: null,
      recentSummary: "",
      guardianNoticeCount: 0,
      settings: null,
      settingsSaving: false,
    });
  },

  async loadDashboard() {
    const identity = api.currentIdentity();
    if (!identity) return;
    const authEpoch = api.currentAuthEpoch();
    this.setData({ loading: true, error: "" });
    const binding = readBindingManifest();
    const hasBinding = Boolean(binding && typeof binding.device_id === "string");
    this.setData({ hasBinding });
    const [profileResult, runtimeResult, activationResult, settingsResult] =
      await Promise.allSettled([
      api.getProfile(identity.user_id),
      hasBinding ? api.getRuntimeProfile(binding.device_id) : Promise.resolve(null),
      hasBinding ? api.getActivationStatus(binding.device_id) : Promise.resolve(null),
      hasBinding ? api.getDeviceSettings(binding.device_id) : Promise.resolve(null),
    ]);
    if (!api.isAuthEpochCurrent(authEpoch)) return;
    const profile = {
      ...defaultProfile,
      ...(profileResult.status === "fulfilled" && profileResult.value ? profileResult.value : {}),
    };
    const companion = companionById(profile.companion_id);
    const runtime = runtimeResult.status === "fulfilled" ? runtimeResult.value : null;
    const activation = activationResult.status === "fulfilled" ? activationResult.value : null;
    const validRuntime = runtime && runtime.valid === true ? runtime : null;
    this.setData({
      profile,
      companion,
      ...mascotAssetsFor(companion),
      device: hasBinding ? deviceStatusSummary(activation, validRuntime) : null,
      persona: validRuntime ? validRuntime.persona : null,
      activeSubjectLabel: !hasBinding
        ? ""
        : validRuntime && validRuntime.active_subject_id
          ? "已确认使用者"
          : validRuntime
            ? "待确认"
            : "状态待同步",
      error: profileResult.status === "rejected" ? profileResult.reason?.message || "个人资料暂时无法加载。" : "",
      settings:
        settingsResult.status === "fulfilled" && settingsResult.value
          ? settingsResult.value
          : null,
    });
    await this.loadLowRiskOverviews();
  },

  // 低风险概览：今日对话次数与最近摘要只经服务端 Runtime Profile
  // memory_recall_private 授权后读取；家长提醒数量只展示计数，不展开原文。
  async loadLowRiskOverviews() {
    const identity = api.currentIdentity();
    if (!identity || !api.hasAuthenticatedSession()) return;
    const authEpoch = api.currentAuthEpoch();
    const gate = await api.requireRuntimeCapability(contracts.Capability.MemoryRecallPrivate);
    if (!api.isAuthEpochCurrent(authEpoch)) return;
    const updates = { memoryAllowed: gate.allowed };
    if (gate.allowed) {
      try {
        const result = await api.getMemoryDays(identity.user_id, 2);
        if (!api.isAuthEpochCurrent(authEpoch)) return;
        const items = Array.isArray(result?.items) ? result.items : [];
        const todayItem = items.find((item) => (item.day || item.date) === todayString());
        const latest = items[0];
        updates.todayCount =
          todayItem && Number.isFinite(todayItem.message_count) ? todayItem.message_count : 0;
        updates.recentSummary =
          latest?.summary?.overview || latest?.overview || latest?.summary_text || "";
      } catch {
        // 概览只作展示，失败保持空态，不打断首页。
      }
    } else {
      updates.todayCount = null;
      updates.recentSummary = "";
    }
    try {
      const notifications = await api.getGuardianNotifications();
      if (!api.isAuthEpochCurrent(authEpoch)) return;
      const items = Array.isArray(notifications)
        ? notifications
        : Array.isArray(notifications?.items)
          ? notifications.items
          : [];
      updates.guardianNoticeCount = items.length;
    } catch {
      // 通知计数失败保持 0，不展示提醒入口。
    }
    this.setData(updates);
  },

  async loginForDashboard() {
    if (!(await requireLogin({ reason: "view_dashboard" }))) return;
    this.setData({ authenticated: true });
    await this.loadDashboard();
  },

  async toggleDeviceSetting(event) {
    const key = event.currentTarget.dataset.key;
    if (!["night_mode", "do_not_disturb"].includes(key)) return;
    await this.saveDeviceSetting({ [key]: Boolean(event.detail.value) });
  },

  async changeVolume(event) {
    const value = Number(event.detail.value);
    if (!Number.isInteger(value) || value < 0 || value > 100) return;
    await this.saveDeviceSetting({ volume_limit: value });
  },

  async selectLearningMode(event) {
    const learningMode = event.currentTarget.dataset.mode;
    if (!LEARNING_MODE_OPTIONS.some((item) => item.value === learningMode)) return;
    await this.saveDeviceSetting({ learning_mode: learningMode });
  },

  async saveDeviceSetting(changes) {
    const binding = readBindingManifest();
    const current = this.data.settings;
    if (!binding || !current || this.data.settingsSaving) return;
    const previous = { ...current };
    this.setData({ settingsSaving: true, error: "" });
    try {
      const updated = await api.updateDeviceSettings(binding.device_id, changes, {
        expectedVersion: current.settings_version,
      });
      this.setData({ settings: updated });
    } catch (error) {
      // 服务端没有确认就不保留本地假状态；重新展示最后一个权威版本。
      this.setData({
        settings: previous,
        error: error?.message || "设备设置未能保存，请刷新后重试。",
      });
    } finally {
      this.setData({ settingsSaving: false });
    }
  },

  openDevice() {
    wx.switchTab({ url: "/pages/device/index" });
  },

  openMemory() {
    wx.switchTab({ url: "/pages/memory/index" });
  },

  openProfile() {
    wx.switchTab({ url: "/pages/profile/index" });
  },

  openOnboarding() {
    wx.navigateTo({ url: "/pages/device-onboarding/index?fresh=1" });
  },

  openGuardian() {
    wx.navigateTo({ url: "/pages/guardian/index" });
  },
});
