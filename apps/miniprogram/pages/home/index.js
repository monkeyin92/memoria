const api = require("../../utils/api");
const { statusBarStyle } = require("../../utils/nav-inset");
const { SUBSCRIPTION_PUSH_ENABLED } = require("../../config");
const { openLogin, requireLogin } = require("../../utils/auth-gate");
const {
  GUEST_HOME,
  LOGIN_GATED_ACTIONS,
  guestAgreement,
  loginGatedAction,
} = require("../../utils/guest-access");
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
const {
  boundChildFromManifest,
  normalizeSessionSummaries,
} = require("../../utils/child-memory");
const { attentionUrl, loadUnreadSafetyAlerts, shouldOfferCrisisSubscription } = require("../../utils/crisis-alerts");
const guardianPush = require("../../utils/guardian-push");
const {
  UNREAD_SAFETY_COPY,
  SAFETY_SUBSCRIBE_HELPER,
  alertTimeLabel,
  buildTrialDeviceCard,
  memoryPreviewItems,
  pinnedAlertMeta,
  usageCenterCopy,
  usageRingStyle,
} = require("../../utils/trial-device-card");

function countCreatedToday(summaries, now = new Date()) {
  const today = todayKey(now);
  return summaries.filter((item) => {
    const created = Date.parse(item.createdAt);
    return Number.isFinite(created) && todayKey(new Date(created)) === today;
  }).length;
}

function guestGreeting() {
  return `${greetingFor()}，欢迎回来`;
}

function avatarGlyphFrom(name) {
  const text = typeof name === "string" ? name.trim() : "";
  return text ? text.slice(0, 1) : "M";
}

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
    showChildMemory: false,
    childMemoryMinorId: "",
    childMemoryName: "",
    memoryPreviews: [],
    safetyAlertId: "",
    statusPill: "离线",
    statusLine: "离线 · 已绑定",
    usageKnown: false,
    usageUsedMinutes: null,
    usageLimitMinutes: null,
    usageFillPercent: 0,
    usageRingStyle: usageRingStyle(null, null),
    ...usageCenterCopy(null, null),
    summaryCount: 0,
    avatarGlyph: "M",
    subscriptionPushEnabled: SUBSCRIPTION_PUSH_ENABLED,
    quietHoursLabel: "",
    nightModeOn: false,
    nightToggleBusy: false,
    showSafetyNightCard: false,
    unreadSafetyAlert: false,
    unreadSafetyCopy: UNREAD_SAFETY_COPY,
    unreadSafetyMeta: pinnedAlertMeta(),
    safetySubscribeOn: false,
    safetySubscribeKnown: false,
    safetySubscribeBusy: false,
    safetySubscribeHelper: SAFETY_SUBSCRIBE_HELPER,
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
    statusBarStyle: statusBarStyle(),
    greeting: guestGreeting(),
    guestHome: GUEST_HOME,
    loginGatedActions: LOGIN_GATED_ACTIONS,
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
    const authenticated = api.hasAuthenticatedSession();
    this.setData({ authenticated, pageLede: formatDateLabel() });
    if (!authenticated) {
      this._enterGuestState();
      return;
    }
    this.loadHome();
  },

  _syncTabBar(authenticated) {
    if (typeof this.getTabBar === "function" && this.getTabBar()) {
      this.getTabBar().setData({ selected: 0, hidden: !authenticated });
    }
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
    this._syncTabBar(false);
    this.setData({
      authenticated: false,
      greeting: guestGreeting(),
      pageLede: "把设备连好，再慢慢留下日常。",
      ...emptyDashboard(),
    });
  },

  openAccountLogin() {
    openLogin({ reason: "view_dashboard" });
  },

  async onGuestGatedAction(event) {
    const action = loginGatedAction(event.currentTarget.dataset.id);
    if (!action) return;
    if (!(await requireLogin({ reason: action.reason }))) return;
    this.setData({ authenticated: true });
    await this.loadHome();
  },

  openGuestAgreement(event) {
    const agreement = guestAgreement(event.currentTarget.dataset.id);
    if (!agreement) return;
    wx.navigateTo({ url: agreement.url });
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

  openNightSettings() {
    wx.switchTab({ url: "/pages/device/index" });
  },

  onNightModeChange() {
    this.setData({ nightModeOn: this.data.nightModeOn });
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

  openChildMemory() {
    const minorId = this.data.childMemoryMinorId;
    if (!minorId) return;
    const name = this.data.childMemoryName || "孩子";
    wx.navigateTo({
      url: `/pages/child-memory/index?minorId=${encodeURIComponent(minorId)}&name=${encodeURIComponent(name)}&section=recent`,
    });
  },

  openUnreadSafety() {
    wx.navigateTo({ url: attentionUrl(this.data.safetyAlertId) });
  },

  async onSafetySubscribeChange(event) {
    if (!this.data.safetySubscribeKnown || this.data.safetySubscribeBusy) {
      this.setData({ safetySubscribeOn: this.data.safetySubscribeOn });
      return;
    }
    const nextOn = Boolean(event.detail.value);
    if (!nextOn) {
      const keepOn = await new Promise((resolve) => {
        wx.showModal({
          title: guardianPush.SAFETY_SUBSCRIBE_OFF_TITLE,
          content: guardianPush.SAFETY_SUBSCRIBE_OFF_CONFIRM,
          confirmText: guardianPush.SAFETY_SUBSCRIBE_OFF_KEEP,
          cancelText: guardianPush.SAFETY_SUBSCRIBE_OFF_PROCEED,
          success: (result) => resolve(Boolean(result?.confirm)),
          fail: () => resolve(true),
        });
      });
      if (keepOn) {
        this.setData({ safetySubscribeOn: true });
        return;
      }
      this.setData({ safetySubscribeBusy: true });
      try {
        const config = this._safetyPushConfig;
        if (!config?.templateId) {
          this.setData(
            guardianPush.safetySubscribeView({
              safetySubscribeKnown: true,
              safetySubscribeOn: false,
            }),
          );
          return;
        }
        const outcome = await guardianPush.optOutCrisisPushAlerts({ config, api });
        if (!outcome.ok) {
          this.setData({ safetySubscribeOn: true });
          wx.showToast({ title: "关闭失败，请稍后再试", icon: "none" });
          return;
        }
        this.setData(
          guardianPush.safetySubscribeView({
            safetySubscribeKnown: true,
            safetySubscribeOn: false,
          }),
        );
      } catch (error) {
        this.setData({ safetySubscribeOn: true });
        wx.showToast({ title: error?.message || "关闭失败，请稍后再试", icon: "none" });
      } finally {
        this.setData({ safetySubscribeBusy: false });
      }
      return;
    }
    const config = this._safetyPushConfig;
    this.setData({ safetySubscribeBusy: true });
    try {
      if (!config?.enabled || !config.templateId) {
        this.setData(
          guardianPush.safetySubscribeView({
            safetySubscribeKnown: true,
            safetySubscribeOn: false,
          }),
        );
        wx.showToast({ title: "安全提醒订阅尚未开通", icon: "none" });
        return;
      }
      const outcome = await guardianPush.subscribeCrisisAlerts({ config, api });
      if (outcome.result === "accept" && outcome.recorded) {
        this.setData(
          guardianPush.safetySubscribeView({
            safetySubscribeKnown: true,
            safetySubscribeOn: true,
          }),
        );
        wx.showToast({ title: "已订阅安全提醒", icon: "none" });
      } else {
        this.setData(
          guardianPush.safetySubscribeView({
            safetySubscribeKnown: true,
            safetySubscribeOn: false,
          }),
        );
        wx.showToast({ title: "未完成订阅，请再试一次", icon: "none" });
      }
    } catch (error) {
      this.setData(
        guardianPush.safetySubscribeView({
          safetySubscribeKnown: true,
          safetySubscribeOn: false,
        }),
      );
      wx.showToast({ title: error?.message || "订阅失败，请稍后再试", icon: "none" });
    } finally {
      this.setData({ safetySubscribeBusy: false });
    }
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
    this._syncTabBar(true);
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
      limitsResult,
    ] = await Promise.allSettled([
      api.getActivationStatus(binding.device_id),
      api.getRuntimeProfile(binding.device_id),
      api.getDeviceSettings(binding.device_id),
      identity ? api.getProfile(identity.user_id) : Promise.resolve(null),
      this._loadToday(identity),
      api.getDeviceDiagnostics(binding.device_id),
      api.getSessionLimits(binding.device_id),
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
    const boundChild = boundChildFromManifest(binding);
    const offerSafety = shouldOfferCrisisSubscription(binding);
    const unread = offerSafety ? await loadUnreadSafetyAlerts(api) : null;
    if (flowSeq !== this._flowSeq || !api.isAuthEpochCurrent(authEpoch)) return;

    let safetyView = {
      safetySubscribeKnown: false,
      safetySubscribeOn: false,
    };
    if (offerSafety) {
      const safety = await guardianPush.loadSafetySubscribeState(api);
      if (flowSeq !== this._flowSeq || !api.isAuthEpochCurrent(authEpoch)) return;
      this._safetyPushConfig = safety.config;
      safetyView = guardianPush.safetySubscribeView(safety);
    } else {
      this._safetyPushConfig = null;
    }

    let memoryPreviews = [];
    let summaryCount = 0;
    let recentChildSummary = "";
    if (boundChild?.personId) {
      try {
        const summaryPayload = await api.getChildSessionSummaries(boundChild.personId);
        if (flowSeq !== this._flowSeq || !api.isAuthEpochCurrent(authEpoch)) return;
        const summaries = normalizeSessionSummaries(summaryPayload);
        memoryPreviews = memoryPreviewItems(summaries, 3);
        recentChildSummary = memoryPreviews[0]?.text || "";
        summaryCount = countCreatedToday(summaries);
      } catch {
        memoryPreviews = [];
      }
    }
    const todayView =
      today.daysUnavailable && recentChildSummary
        ? {
            ...today,
            todayTitle: "近期摘要",
            todayOverview: recentChildSummary,
            dailySummaryText: `最近：${recentChildSummary}`,
          }
        : today;

    const trialCard = buildTrialDeviceCard({
      online,
      runtimeProfile: runtime,
      usedMinutes: limitsResult.status === "fulfilled" ? limitsResult.value?.today_used_minutes : null,
    });
    const unreadSafetyMeta = pinnedAlertMeta({
      occurredAt: alertTimeLabel(unread?.newest.occurredAt),
    });
    const placeName = devicePlaceName(binding, personaName);
    const childName = boundChild?.displayName || "";
    const todayCount = today.todayCount || 0;

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
      devicePlaceName: placeName,
      pageLede: formatDateLabel(),
      heroTitle: online ? "给今天，留一点回味。" : "等它回来，记录还在。",
      heroCaption: online
        ? `在设备旁唤醒「${settings?.wake_word_display || "茉莉"}」。需要记住的事，稍后确认。`
        : "可查看已同步的回顾。设备恢复连接后再记录。",
      heroFoot: online ? "在设备上使用，手机不录音" : "绑定关系不受影响",
      pendingCount: todayView.pendingCount || 0,
      todayCount,
      todayMeta: todayView.todayMeta || "",
      todayTitle: todayView.todayTitle || "",
      todayOverview: todayView.todayOverview || "",
      dailySummaryText: todayView.dailySummaryText || "",
      summaryCount,
      avatarGlyph: avatarGlyphFrom(identity?.display_name),
      subscriptionPushEnabled: SUBSCRIPTION_PUSH_ENABLED,
      showChildMemory: Boolean(boundChild),
      childMemoryMinorId: boundChild?.personId || "",
      childMemoryName: childName,
      memoryPreviews,
      safetyAlertId: unread?.newest.notificationId || "",
      statusPill: trialCard.statusPill,
      statusLine: trialCard.statusLine,
      usageKnown: trialCard.usageKnown,
      usageUsedMinutes: trialCard.usageUsedMinutes,
      usageLimitMinutes: trialCard.usageLimitMinutes,
      usageFillPercent: trialCard.usageFillPercent,
      usageRingStyle: trialCard.usageRingStyle,
      usageCenterLabel: trialCard.usageCenterLabel,
      usageCapLabel: trialCard.usageCapLabel,
      quietHoursLabel: trialCard.quietHoursLabel,
      nightModeOn: trialCard.nightModeOn,
      showSafetyNightCard: offerSafety,
      unreadSafetyAlert: unread !== null,
      unreadSafetyCopy: UNREAD_SAFETY_COPY,
      unreadSafetyMeta,
      safetySubscribeHelper: SAFETY_SUBSCRIBE_HELPER,
      ...safetyView,
      error: failures.length ? "部分状态暂时无法同步。" : "",
    });
  },

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
      daysUnavailable: false,
    };
    if (!identity) return empty;
    // A closed daily-summary read is a normal state in production: count today
    // from conversation sessions, never from the legacy day rows.
    const [daysResult, reviewResult, sessionsResult] = await Promise.all([
      Promise.resolve().then(() => api.getMemoryDays(identity.user_id, 7)).catch(() => null),
      api.getConversationReview(),
      typeof api.getConversationSessions === "function"
        ? api.getConversationSessions(20).catch(() => null)
        : Promise.resolve(null),
    ]);
    const daysUnavailable = daysResult === null;
    const today = todayKey();
    const day = (daysResult?.items || []).find((item) => (item.day || item.date) === today);
    const sessionsToday = ((sessionsResult && sessionsResult.items) || []).filter(
      (item) => item.occurred_at && todayKey(new Date(item.occurred_at)) === today,
    ).length;
    const count = sessionsToday;
    const pending = (reviewResult?.memory_candidates || []).length;
    const overview =
      day?.summary?.overview || day?.overview || day?.summary_text || "";
    const title = day?.summary?.title || day?.title || "";
    if (!count && !pending) return { ...empty, daysUnavailable };
    const dailySummaryText = count
      ? overview
        ? `今天和 Memoria 聊了 ${count} 次。${overview}`
        : `今天和 Memoria 聊了 ${count} 次。`
      : "";
    return {
      pendingCount: pending,
      todayCount: count,
      todayMeta: count ? `今天 · ${count} 次对话` : "今天",
      todayTitle: title || (pending ? "有内容等你确认" : ""),
      todayOverview: overview || "具体内容在回顾里。",
      dailySummaryText,
      daysUnavailable,
    };
  },
});
