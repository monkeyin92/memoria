const api = require("../../utils/api");
const { requireLogin } = require("../../utils/auth-gate");
const { companionById } = require("../../utils/companions");
const { parseCustomPersona } = require("../../utils/custom-persona");
const { MODE_META, readBindingManifest } = require("../../utils/device-binding");
const { deviceStatusSummary } = require("../../utils/device-status");
const { greetingFor } = require("../../utils/greeting");
const contracts = require("../../utils/multi-subject-contracts");

function todayKey(date = new Date()) {
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${date.getFullYear()}-${month}-${day}`;
}

function emptyDashboard() {
  return {
    loading: false,
    syncingBindings: false,
    bindingSyncError: "",
    bindingChoices: [],
    needsBindingChoice: false,
    error: "",
    hasBinding: false,
    online: false,
    onlineLabel: "状态待同步",
    currentUserLabel: "待确认",
    wakeWordLabel: "未读取",
    personaName: "星澜",
    personaVoice: "暖阳青年",
    personaSummary: "系统默认人格与声音。",
    todayMeta: "今天还没有可回顾的内容",
    todayOverview: "对着设备说几句后，再回来看。",
  };
}

function bindingChoiceItems(bindings) {
  return (bindings || []).map((binding) => ({
    binding,
    bindingId: binding.binding_id,
    label: `Memoria · ${String(binding.device_id || "").slice(-4) || "未知"}`,
    modeLabel: MODE_META[binding.declared_mode]?.title || "已绑定设备",
  }));
}

Page({
  data: {
    greeting: `${greetingFor()}，朋友`,
    authenticated: false,
    ...emptyDashboard(),
  },

  onLoad() {
    const app = getApp();
    if (typeof app?.subscribeAuthCleared === "function") {
      this._unsubscribeAuthCleared = app.subscribeAuthCleared(() => this._enterGuestState());
    }
  },

  onShow() {
    const authenticated = api.hasAuthenticatedSession();
    const identity = api.currentIdentity();
    const name = identity?.display_name || "朋友";
    this.setData({
      authenticated,
      greeting: `${greetingFor()}，${name}`,
    });
    if (!authenticated) {
      this._enterGuestState();
      return;
    }
    this.loadHome();
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
    this.loadHome().finally(() => wx.stopPullDownRefresh());
  },

  _enterGuestState() {
    this._flowSeq = (this._flowSeq || 0) + 1;
    this.setData({
      authenticated: false,
      greeting: `${greetingFor()}，朋友`,
      ...emptyDashboard(),
    });
  },

  async loginFromHome() {
    if (!(await requireLogin({ reason: "view_dashboard" }))) return;
    this.setData({ authenticated: true });
    await this.loadHome();
  },

  openOnboarding() {
    wx.navigateTo({ url: "/pages/device-onboarding/index?fresh=1" });
  },

  openDevice() {
    wx.switchTab({ url: "/pages/device/index" });
  },

  openProfile() {
    wx.switchTab({ url: "/pages/profile/index" });
  },

  openMemory() {
    wx.switchTab({ url: "/pages/memory/index" });
  },

  async retryBindingSync() {
    if (!(await requireLogin({ reason: "view_dashboard" }))) {
      this._enterGuestState();
      return;
    }
    this.setData({ authenticated: true });
    await this.loadHome();
  },

  async chooseDeviceBinding(event) {
    const bindingId = event.currentTarget.dataset.bindingId;
    const choice = this.data.bindingChoices.find((item) => item.bindingId === bindingId);
    if (!choice) return;
    try {
      api.selectDeviceBinding(choice.binding);
      this.setData({
        loading: true,
        syncingBindings: false,
        bindingSyncError: "",
        needsBindingChoice: false,
      });
      await this.loadHome();
    } catch (error) {
      this.setData({
        bindingSyncError: error?.message || "无法切换到这台设备，请重试。",
      });
    }
  },

  async loadHome() {
    const flowSeq = (this._flowSeq = (this._flowSeq || 0) + 1);
    const authEpoch = api.currentAuthEpoch();
    const cachedBinding = readBindingManifest();
    this.setData({
      loading: true,
      syncingBindings: !cachedBinding,
      bindingSyncError: "",
      needsBindingChoice: false,
      bindingChoices: [],
      error: "",
    });

    const bindingState = await api.syncDeviceBindings();
    if (flowSeq !== this._flowSeq || !api.isAuthEpochCurrent(authEpoch)) return;

    if (bindingState.status === "error") {
      this.setData({
        ...emptyDashboard(),
        bindingSyncError: "设备同步失败，请检查网络后重试。",
      });
      return;
    }
    if (bindingState.status === "empty") {
      this.setData(emptyDashboard());
      return;
    }
    if (bindingState.status === "choose") {
      this.setData({
        ...emptyDashboard(),
        needsBindingChoice: true,
        bindingChoices: bindingChoiceItems(bindingState.bindings),
      });
      return;
    }

    const binding = bindingState.binding;
    if (!binding || typeof binding.device_id !== "string") {
      this.setData({
        ...emptyDashboard(),
        bindingSyncError: "设备同步结果不完整，请重试。",
      });
      return;
    }

    const identity = api.currentIdentity();
    const [activationResult, runtimeResult, settingsResult, profileResult, todayResult] =
      await Promise.allSettled([
        api.getActivationStatus(binding.device_id),
        api.getRuntimeProfile(binding.device_id),
        api.getDeviceSettings(binding.device_id),
        identity ? api.getProfile(identity.user_id) : Promise.resolve(null),
        this._loadToday(identity),
      ]);
    if (flowSeq !== this._flowSeq || !api.isAuthEpochCurrent(authEpoch)) return;

    const activation =
      activationResult.status === "fulfilled" ? activationResult.value : null;
    const runtime =
      runtimeResult.status === "fulfilled" && runtimeResult.value !== null
        ? runtimeResult.value
        : null;
    const settings =
      settingsResult.status === "fulfilled" && settingsResult.value !== null
        ? settingsResult.value
        : null;
    const profile =
      profileResult.status === "fulfilled" && profileResult.value !== null
        ? profileResult.value
        : null;
    const today =
      todayResult.status === "fulfilled" ? todayResult.value : emptyDashboard();
    const summary = deviceStatusSummary(activation, runtime);
    const custom = parseCustomPersona(profile?.bio);
    const companion = companionById(profile?.companion_id);
    const personaName = custom.active ? custom.name : companion.name;
    const personaVoice = custom.active ? "自定义声音，评估通过前使用系统音色" : companion.voiceName;
    const personaSummary = custom.active
      ? custom.text
      : companion.description || companion.tagline;
    const failures = [activationResult, runtimeResult].filter(
      (result) => result.status === "rejected",
    );

    this.setData({
      loading: false,
      syncingBindings: false,
      bindingSyncError:
        bindingState.status === "cached"
          ? "设备列表暂时无法同步，已显示本机保存的设备。"
          : "",
      bindingChoices: [],
      needsBindingChoice: false,
      hasBinding: true,
      online: summary.online,
      onlineLabel: summary.onlineLabel,
      currentUserLabel: runtime?.active_subject_id ? "已确认" : "待在设备上确认",
      wakeWordLabel: settings?.wake_word_display || "未读取",
      personaName,
      personaVoice,
      personaSummary,
      todayMeta: today.todayMeta,
      todayOverview: today.todayOverview,
      error: failures.length ? "部分状态暂时无法同步。" : "",
    });
  },

  async _loadToday(identity) {
    const empty = {
      todayMeta: "今天还没有可回顾的内容",
      todayOverview: "对着设备说几句后，再回来看。",
    };
    if (!identity) return empty;
    const gate = await api.requireRuntimeCapability(contracts.Capability.MemoryRecallPrivate);
    if (!gate.allowed) return empty;
    const [daysResult, reviewResult] = await Promise.all([
      api.getMemoryDays(identity.user_id, 7),
      api.getConversationReview(),
    ]);
    const today = todayKey();
    const day = (daysResult.items || []).find((item) => (item.day || item.date) === today);
    const count = day?.message_count || 0;
    const pending = (reviewResult?.memory_candidates || []).length;
    const overview =
      day?.summary?.overview || day?.overview || day?.summary_text || "";
    if (!count && !pending) return empty;
    return {
      todayMeta: `${count} 段对话 · ${pending} 条待你确认`,
      todayOverview: overview || "具体内容在回顾里。",
    };
  },
});
