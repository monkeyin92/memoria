const api = require("../../utils/api");
const { requireLogin } = require("../../utils/auth-gate");
const { companionById, defaultCompanionId } = require("../../utils/companions");
const { MODE_META, readBindingManifest } = require("../../utils/device-binding");
const {
  currentUserSummary,
  devicePlaceName,
  deviceStatusSummary,
} = require("../../utils/device-status");
const { greetingFor, formatDateLabel } = require("../../utils/greeting");
const { readOnboardingSessionId } = require("../../utils/device-onboarding/session-store");
const { readSubjectLabel } = require("../../utils/subject-label");

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
    devicePlaceName: "我的设备",
    heroRoleId: defaultCompanionId,
    pageLede: formatDateLabel(),
    heroTitle: "给今天，留一点回味。",
    heroCaption: "在设备旁唤醒「茉莉」。需要记住的事，稍后确认。",
    heroFoot: "在设备上使用，手机不录音",
    pendingCount: 0,
    todayCount: 0,
    todayMeta: "",
    todayTitle: "",
    todayOverview: "",
    dailySummaryText: "",
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
    todoExpanded: true,
  },

  onLoad() {
    const app = getApp();
    if (typeof app?.subscribeAuthCleared === "function") {
      this._unsubscribeAuthCleared = app.subscribeAuthCleared(() => this._enterGuestState());
    }
  },

  onShow() {
    if (typeof this.getTabBar === "function" && this.getTabBar()) { this.getTabBar().setData({ selected: 0 }); }
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

  toggleTodo() {
    this.setData({ todoExpanded: !this.data.todoExpanded });
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

  /*
   * 一键分享：只把已经过门禁加载的每日摘要文案带出去；未登录或没有摘要时
   * 分享一张不含任何私人内容的通用卡片。
   */
  onShareAppMessage() {
    const summary = this.data.authenticated ? this.data.dailySummaryText : "";
    return {
      title: (summary || "Memoria · 每天的对话回顾").slice(0, 80),
      path: "/pages/home/index",
    };
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
    const companion = companionById(profile?.companion_id);
    // 设备当前分配给使用人的自定义人格（cu_*）优先于账号级内置人格；
    // 名字来自账号目录，读不到时退回内置人格而不是猜一个名字。
    const custom = await this._resolveCustomPersona(runtime);
    if (flowSeq !== this._flowSeq || !api.isAuthEpochCurrent(authEpoch)) return;
    const personaName = custom ? custom.display_name : companion.name;
    const personaVoice = custom ? "你提供的声音样本" : companion.voiceName;
    const personaSummary = custom
      ? custom.style_description || companion.description || companion.tagline
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
      heroRoleId: companion.id,
      devicePlaceName: devicePlaceName(binding, personaName),
      pageLede: formatDateLabel(),
      heroTitle: online ? "给今天，留一点回味。" : "等它回来，记录还在。",
      heroCaption: online
        ? "在设备旁唤醒「茉莉」。需要记住的事，稍后确认。"
        : "可查看已同步的回顾。设备恢复连接后再记录。",
      heroFoot: online ? "在设备上使用，手机不录音" : "绑定关系不受影响",
      pendingCount: today.pendingCount || 0,
      todayCount: today.todayCount || 0,
      todayMeta: today.todayMeta || "",
      todayTitle: today.todayTitle || "",
      todayOverview: today.todayOverview || "",
      dailySummaryText: today.dailySummaryText || "",
      error: failures.length ? "部分状态暂时无法同步。" : "",
    });
  },

  // 只有设备签发的 Runtime Profile 会声明实际使用的人格；自定义人格还要在
  // 账号目录里存在才展示，避免把未知 id 当成名字显示。
  async _resolveCustomPersona(runtime) {
    const personaId = runtime?.persona?.persona_id;
    if (typeof personaId !== "string" || !personaId.startsWith("cu_")) return null;
    try {
      const payload = await api.listPersonas();
      const record = (payload?.custom_personas || []).find(
        (item) => item.persona_id === personaId,
      );
      return record || null;
    } catch {
      return null;
    }
  },

  async _loadToday(identity) {
    const empty = {
      pendingCount: 0,
      todayCount: 0,
      todayMeta: "",
      todayTitle: "",
      todayOverview: "",
      dailySummaryText: "",
    };
    if (!identity) return empty;
    // 每日摘要是账号自己的回顾，服务端按登录账号鉴权，不走 Runtime Profile 门禁。
    const [daysResult, reviewResult, sessionsResult] = await Promise.all([
      api.getMemoryDays(identity.user_id, 7),
      api.getConversationReview(),
      // 会话列表是可选增强：拿不到就退回 day 的 message_count，不能让首页
      // 因为一个可选接口失败而整体空白。
      typeof api.getConversationSessions === "function"
        ? api.getConversationSessions(20).catch(() => null)
        : Promise.resolve(null),
    ]);
    const today = todayKey();
    const day = (daysResult.items || []).find((item) => (item.day || item.date) === today);
    // 「今日对话次数」以当前主体的会话数为准；会话列表不可用时退回服务端
    // 日计数。两者都在同一道 MemoryRecallPrivate 门禁之后才读取。
    const sessionsToday = ((sessionsResult && sessionsResult.items) || []).filter(
      (item) => item.occurred_at && todayKey(new Date(item.occurred_at)) === today,
    ).length;
    const count = sessionsToday || day?.message_count || 0;
    const pending = (reviewResult?.memory_candidates || []).length;
    const overview =
      day?.summary?.overview || day?.overview || day?.summary_text || "";
    const title = day?.summary?.title || day?.title || "";
    if (!count && !pending) return empty;
    // 每日摘要只组合「次数 + 服务端已生成的回顾」，不生成情绪、不编造事实。
    const dailySummaryText = count
      ? overview
        ? `今天和 Memoria 聊了 ${count} 次。${overview}`
        : `今天和 Memoria 聊了 ${count} 次。回顾还没生成，可在回顾页生成。`
      : "";
    return {
      pendingCount: pending,
      todayCount: count,
      todayMeta: count ? `今天 · ${count} 次对话` : "今天",
      todayTitle: title || (pending ? "有内容等你确认" : ""),
      todayOverview: overview || "具体内容在回顾里。",
      dailySummaryText,
    };
  },
});
