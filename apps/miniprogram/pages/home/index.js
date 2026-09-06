const api = require("../../utils/api");
const { requireLogin } = require("../../utils/auth-gate");
const { companionById, defaultCompanionId } = require("../../utils/companions");
const { parseCustomPersona } = require("../../utils/custom-persona");
const { MODE_META, readBindingManifest } = require("../../utils/device-binding");
const {
  currentUserSummary,
  devicePlaceName,
  deviceStatusSummary,
} = require("../../utils/device-status");
const { greetingFor, formatDateLabel } = require("../../utils/greeting");
const { readOnboardingSessionId } = require("../../utils/device-onboarding/session-store");
const { readSubjectLabel } = require("../../utils/subject-label");
const contracts = require("../../utils/multi-subject-contracts");

function todayKey(date = new Date()) {
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${date.getFullYear()}-${month}-${day}`;
}

function pendingOnboarding() {
  try {
    return Boolean(readOnboardingSessionId());
  } catch {
    return false;
  }
}

function emptyDashboard() {
  const companion = companionById(defaultCompanionId);
  return {
    loading: false,
    syncingBindings: false,
    bindingSyncError: "",
    bindingChoices: [],
    needsBindingChoice: false,
    error: "",
    hasBinding: false,
    hasPendingOnboarding: pendingOnboarding(),
    online: false,
    onlineLabel: "状态待同步",
    currentUserLabel: "未读取",
    wakeWordLabel: "未读取",
    personaName: companion.name,
    personaVoice: companion.voiceName,
    personaSummary: companion.description,
    devicePlaceName: "家中的设备",
    pageLede: formatDateLabel(),
    heroTitle: "给今天，留一点回味。",
    heroCaption: "在设备旁唤醒「茉莉」。需要记住的事，稍后确认。",
    heroFoot: "在设备上使用，手机不录音",
    pendingCount: 0,
    todayMeta: "",
    todayTitle: "",
    todayOverview: "",
  };
}

function bindingChoiceItems(bindings) {
  return (bindings || []).map((binding) => {
    const companion = companionById(defaultCompanionId);
    return {
      binding,
      bindingId: binding.binding_id,
      label: devicePlaceName(binding, companion.name),
      modeLabel: MODE_META[binding.declared_mode]?.title || "已绑定设备",
    };
  });
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
      pageLede: formatDateLabel(),
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
      pageLede: "把设备连好，再慢慢留下日常。",
      ...emptyDashboard(),
    });
  },

  async loginFromHome() {
    if (!(await requireLogin({ reason: "view_dashboard" }))) return;
    this.setData({ authenticated: true });
    await this.loadHome();
  },

  openHowItWorks() {
    wx.showModal({
      title: "怎么用",
      content:
        "在设备旁唤醒「茉莉」说话。手机用来连接设备、确认回顾和管理资料，不录音，也不代替设备对话。",
      showCancel: false,
      confirmText: "知道了",
    });
  },

  showSyncHelp() {
    wx.showModal({
      title: "手机和电脑显示不同？",
      content:
        "设备跟随微信账号同步。请先登录后点重新同步。同步失败不代表未绑定，也不需要重新配网。",
      showCancel: false,
      confirmText: "知道了",
    });
  },

  openOnboarding() {
    if (pendingOnboarding()) {
      wx.showModal({
        title: "继续上次启用？",
        content: "上次已开始连接，可以继续设置，也可以重新开始。",
        confirmText: "继续上次",
        cancelText: "重新开始",
        success: (result) => {
          wx.navigateTo({
            url: result.confirm
              ? "/pages/device-onboarding/index"
              : "/pages/device-onboarding/index?fresh=1",
          });
        },
      });
      return;
    }
    wx.navigateTo({ url: "/pages/device-onboarding/index" });
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

  openPendingMemory() {
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
      hasPendingOnboarding: pendingOnboarding(),
    });

    const bindingState = await api.syncDeviceBindings();
    if (flowSeq !== this._flowSeq || !api.isAuthEpochCurrent(authEpoch)) return;

    if (bindingState.status === "error") {
      this.setData({
        ...emptyDashboard(),
        bindingSyncError: "设备信息暂未同步。这不代表未绑定，无需重新配网。",
      });
      return;
    }
    if (bindingState.status === "empty") {
      this.setData({
        ...emptyDashboard(),
        pageLede: "只需几步，把你的设备连接起来。",
      });
      return;
    }
    if (bindingState.status === "choose") {
      this.setData({
        ...emptyDashboard(),
        needsBindingChoice: true,
        bindingChoices: bindingChoiceItems(bindingState.bindings),
        pageLede: "你的账号下有多台设备。",
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
    const [
      activationResult,
      runtimeResult,
      settingsResult,
      profileResult,
      todayResult,
      diagnosticsResult,
    ] = await Promise.allSettled([
      api.getActivationStatus(binding.device_id),
      api.getRuntimeProfile(binding.device_id),
      api.getDeviceSettings(binding.device_id),
      identity ? api.getProfile(identity.user_id) : Promise.resolve(null),
      this._loadToday(identity),
      api.getDeviceDiagnostics(binding.device_id),
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
    const today = todayResult.status === "fulfilled" ? todayResult.value : {};
    const diagnostics =
      diagnosticsResult.status === "fulfilled" && diagnosticsResult.value !== null
        ? diagnosticsResult.value
        : null;
    const summary = deviceStatusSummary(activation, runtime, diagnostics);
    const custom = parseCustomPersona(profile?.bio);
    const companion = companionById(profile?.companion_id);
    const personaName = custom.active ? custom.name : companion.name;
    const personaVoice = custom.active ? "你提供的声音样本" : companion.voiceName;
    const personaSummary = custom.active
      ? custom.text
      : companion.description || companion.tagline;
    const failures = [activationResult, runtimeResult].filter(
      (result) => result.status === "rejected",
    );
    const online = summary.online;

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
      hasPendingOnboarding: pendingOnboarding(),
      online,
      onlineLabel: summary.onlineLabel,
      currentUserLabel: currentUserSummary({
        subjectLabel: readSubjectLabel(binding),
      }),
      wakeWordLabel: settings?.wake_word_display || "未读取",
      personaName,
      personaVoice,
      personaSummary,
      devicePlaceName: devicePlaceName(binding, personaName),
      pageLede: formatDateLabel(),
      heroTitle: online ? "给今天，留一点回味。" : "等它回来，记录还在。",
      heroCaption: online
        ? "在设备旁唤醒「茉莉」。需要记住的事，稍后确认。"
        : "可查看已同步的回顾。设备恢复连接后再记录。",
      heroFoot: online ? "在设备上使用，手机不录音" : "绑定关系不受影响",
      pendingCount: today.pendingCount || 0,
      todayMeta: today.todayMeta || "",
      todayTitle: today.todayTitle || "",
      todayOverview: today.todayOverview || "",
      error: failures.length ? "部分状态暂时无法同步。" : "",
    });
  },

  async _loadToday(identity) {
    const empty = {
      pendingCount: 0,
      todayMeta: "",
      todayTitle: "",
      todayOverview: "",
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
    const title = day?.summary?.title || day?.title || "";
    if (!count && !pending) return empty;
    return {
      pendingCount: pending,
      todayMeta: count ? `今天 · ${count} 段对话` : "今天",
      todayTitle: title || (pending ? "有内容等你确认" : ""),
      todayOverview: overview || "具体内容在回顾里。",
    };
  },
});
