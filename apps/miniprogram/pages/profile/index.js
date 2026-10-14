const api = require("../../utils/api");
const { statusBarStyle } = require("../../utils/nav-inset");
const compliance = require("../../utils/compliance");
const { companions, companionById, defaultCompanionId } = require("../../utils/companions");
const { requireLogin } = require("../../utils/auth-gate");
const { MODE_META, readBindingManifest, entryAllowed } = require("../../utils/device-binding");
const contracts = require("../../utils/multi-subject-contracts");

const defaultProfile = {
  display_name: "新朋友",
  auto_summary: true,
  gentle_reminders: false,
  companion_id: defaultCompanionId,
  subject_category: null,
};

const DELETE_CONFIRMATION_TEXT = "永久删除我的全部数据";


// 只有服务端会把自定义人格写进 Runtime Profile 的 persona；非 cu_ 前缀表示
// 内置人格，克隆声音时不该带 custom_persona_id。
function customPersonaIdFor(profile) {
  const personaId = profile?.persona?.persona_id;
  if (typeof personaId !== "string" || !personaId.startsWith("cu_")) return "";
  return personaId;
}

function mediaTypeForPath(filePath) {
  const lower = String(filePath || "").toLowerCase();
  if (lower.endsWith(".wav")) return "audio/wav";
  if (lower.endsWith(".mp3")) return "audio/mpeg";
  if (lower.endsWith(".m4a") || lower.endsWith(".mp4")) return "audio/mp4";
  if (lower.endsWith(".aac")) return "audio/aac";
  if (lower.endsWith(".ogg")) return "audio/ogg";
  if (lower.endsWith(".flac")) return "audio/flac";
  return "";
}

function estimateDurationMs(byteLength, mediaType) {
  const bytes = Number(byteLength) || 0;
  if (mediaType === "audio/wav" || mediaType === "audio/x-wav") {
    return Math.round((bytes / 32000) * 1000);
  }
  return Math.round((bytes * 8) / 48);
}

function clampCloneDuration(durationMs) {
  const value = Math.round(Number(durationMs) || 0);
  if (value < 10000) return 10000;
  if (value > 60000) return 60000;
  return value;
}


function profileInitialFor(displayName) {
  const name = String(displayName || "").trim();
  return name ? name.slice(0, 1) : "友";
}

function deviceBindingLabel(binding) {
  const tail = String(binding?.device_id || "").slice(-4);
  return "Memoria · " + (tail || "未知");
}

function deviceBindingModeLabel(binding) {
  return MODE_META[binding?.declared_mode]?.title || "已绑定设备";
}

function deviceBindingChoiceItems(bindings) {
  return (bindings || []).map((binding) => ({
    binding,
    bindingId: binding.binding_id,
    label: deviceBindingLabel(binding),
    modeLabel: deviceBindingModeLabel(binding),
  }));
}

function deviceBindingReset(accountId = "", syncing = false) {
  return {
    deviceBindingAccountId: accountId,
    deviceBindingState: syncing ? "syncing" : "idle",
    deviceBindingSyncing: syncing,
    deviceBindingStateLabel: syncing ? "同步中" : "待同步",
    deviceBindingDetail: syncing
      ? "正在从服务端恢复账号可见设备。"
      : "登录后从服务端同步账号可见设备。",
    deviceBindingError: "",
    deviceBinding: null,
    deviceBindingCountLabel: "未确认",
    deviceBindingChoices: [],
  };
}

function deviceBindingStateData(state) {
  const status = state?.status || "error";
  const binding = state?.binding || null;
  const bindings = Array.isArray(state?.bindings) ? state.bindings : [];
  const error = state?.error?.message || "";
  const view = {
    deviceBindingState: status,
    deviceBindingSyncing: false,
    deviceBindingError: error,
    deviceBinding: binding,
    deviceBindingCountLabel: "未确认",
    deviceBindingChoices: [],
  };

  if ((status === "ready" || status === "cached") && binding) {
    view.deviceBindingLabel = deviceBindingLabel(binding);
    view.deviceBindingModeLabel = deviceBindingModeLabel(binding);
    view.deviceBindingCountLabel = status === "cached"
      ? "当前 1 台（列表未同步）"
      : bindings.length ? bindings.length + " 台" : "当前 1 台";
  } else {
    view.deviceBindingLabel = "";
    view.deviceBindingModeLabel = "";
  }

  if (status === "ready") {
    view.deviceBindingStateLabel = "已绑定设备";
    view.deviceBindingDetail = view.deviceBindingModeLabel + " · " + view.deviceBindingLabel;
    view.deviceBindingChoices = deviceBindingChoiceItems(bindings);
  } else if (status === "cached") {
    view.deviceBindingStateLabel = "已绑定设备";
    view.deviceBindingDetail = view.deviceBindingModeLabel + " · " + view.deviceBindingLabel +
      "；设备列表暂时无法同步，当前为本机保存的绑定。";
  } else if (status === "empty") {
    view.deviceBindingCountLabel = "0 台";
    view.deviceBindingStateLabel = "未绑定设备";
    view.deviceBindingDetail = "服务端确认这个账号还没有生效绑定。";
  } else if (status === "choose") {
    view.deviceBindingCountLabel = bindings.length + " 台待选择";
    view.deviceBindingStateLabel = "待选择设备";
    view.deviceBindingDetail = "服务端找到多台生效设备，请选择当前设备。";
    view.deviceBindingChoices = deviceBindingChoiceItems(bindings);
  } else if (status === "syncing") {
    view.deviceBindingStateLabel = "同步中";
    view.deviceBindingDetail = "正在从服务端恢复账号可见设备。";
  } else {
    view.deviceBindingStateLabel = "同步失败";
    view.deviceBindingDetail = "设备信息暂未同步，这不代表未绑定。请稍后重新同步，无需重新配网。";
  }

  return view;
}

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
    {
      label: "档案活跃天数",
      value: String(payload.memory_days_with_activity ?? 0),
    },
    {
      label: "档案消息条数",
      value: String(payload.total_messages ?? 0),
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
    statusBarStyle: statusBarStyle(),
    identity: null,
    profile: defaultProfile,
    profileInitial: profileInitialFor(defaultProfile.display_name),
    companions,
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
    personaHeadline: `${companionById(defaultCompanionId).name}，你的日常角色`,
    accountId: "",
    hasRuntimeProfile: false,
    runtimeCapabilities: [],
    speakerEntryAllowed: false,
    digitalSelfEntryAllowed: false,
    guardianEntryAllowed: false,
    rawVoiceEntryAllowed: false,
    profileUnavailableReason: "",
    deliveredCapabilities: [],
    deliveredCapabilitiesLoading: false,
    customPersonaId: "",
    customPersonaLabel: "",
    ...deviceBindingReset(),
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
    if (typeof this.getTabBar === "function" && this.getTabBar()) {
      this.getTabBar().setData({ selected: 4 });
    }
    const authenticated = api.hasAuthenticatedSession();
    const identity = api.currentIdentity();
    if (identity && identity.user_id !== this.data.deviceBindingAccountId) {
      this.setData({
        ...deviceBindingReset(identity.user_id, true),
        authenticated,
      });
    } else {
      this.setData({ authenticated });
    }
    if (!authenticated) {
      this._enterGuestState();
      return;
    }
    this._loadAuthenticatedData();
  },

  onUnload() {
    this._profileDataSeq = (this._profileDataSeq || 0) + 1;
    this._deviceBindingSeq = (this._deviceBindingSeq || 0) + 1;
    // 卸载后清空忙位与在途请求引用：迟到的请求因 seq 已递增会被丢弃，
    // 其 finally 因为 busy key 已清空不会再误改这里的释放状态。
    this._profileDataBusy = false;
    this._profileDataBusyKey = "";
    this._profileDataRequest = null;
    this._deviceBindingBusy = false;
    this._deviceBindingBusyKey = "";
    this._deviceBindingRequest = null;
    if (this._unsubscribeAuthCleared) {
      this._unsubscribeAuthCleared();
      this._unsubscribeAuthCleared = null;
    }
  },

  _enterGuestState() {
    this._profileDataSeq = (this._profileDataSeq || 0) + 1;
    this._deviceBindingSeq = (this._deviceBindingSeq || 0) + 1;
    this._profileDataBusy = false;
    this._profileDataBusyKey = "";
    this._profileDataRequest = null;
    this._deviceBindingBusy = false;
    this._deviceBindingBusyKey = "";
    this._deviceBindingRequest = null;
    this._lastCapabilityState = null;
    this.setData({
      authenticated: false,
      identity: null,
      profile: defaultProfile,
      profileInitial: profileInitialFor(defaultProfile.display_name),
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
      deliveredCapabilities: [],
      deliveredCapabilitiesLoading: false,
      customPersonaId: "",
      customPersonaLabel: "",
      ...deviceBindingReset(),
    });
  },

  _deviceBindingStale(flowSeq, authEpoch) {
    return (
      flowSeq !== this._deviceBindingSeq || !api.isAuthEpochCurrent(authEpoch)
    );
  },

  async loadDeviceBindingSync() {
    const identity = api.currentIdentity();
    if (!identity || !api.hasAuthenticatedSession()) return null;
    const authEpoch = api.currentAuthEpoch();
    const busyKey = authEpoch + ":" + identity.user_id;
    if (
      this._deviceBindingBusy &&
      this._deviceBindingBusyKey === busyKey &&
      this._deviceBindingRequest
    ) {
      // 同一账号同一 epoch 的在途同步直接复用，避免重复请求竞态。
      return this._deviceBindingRequest || null;
    }

    const flowSeq = (this._deviceBindingSeq = (this._deviceBindingSeq || 0) + 1);
    const accountChanged = identity.user_id !== this.data.deviceBindingAccountId;
    this.setData({
      ...(accountChanged ? deviceBindingReset(identity.user_id, true) : {}),
      deviceBindingState: "syncing",
      deviceBindingSyncing: true,
    });

    this._deviceBindingBusy = true;
    this._deviceBindingBusyKey = busyKey;
    const request = (async () => {
      try {
        const state = await api.syncDeviceBindings();
        if (this._deviceBindingStale(flowSeq, authEpoch)) return null;
        this.setData(deviceBindingStateData(state));
        return state;
      } catch (error) {
        if (this._deviceBindingStale(flowSeq, authEpoch)) return null;
        const state = { status: "error", binding: null, bindings: [], error };
        this.setData(deviceBindingStateData(state));
        return state;
      } finally {
        // 只有仍持有同一 busy key 的请求才能清忙位；账号切换后旧请求
        // 迟到时不能吞掉新账号正在进行的同步。
        if (this._deviceBindingBusyKey === busyKey) {
          this._deviceBindingBusy = false;
          this._deviceBindingBusyKey = "";
        }
        if (this._deviceBindingRequest === request) {
          this._deviceBindingRequest = null;
        }
      }
    })();
    this._deviceBindingRequest = request;
    return request;
  },

  async _loadAuthenticatedData() {
    const identity = api.currentIdentity();
    if (!identity || !api.hasAuthenticatedSession()) return null;
    const authEpoch = api.currentAuthEpoch();
    const busyKey = authEpoch + ":" + identity.user_id;
    if (
      this._profileDataBusy &&
      this._profileDataBusyKey === busyKey &&
      this._profileDataRequest
    ) {
      // 同一账号同一 epoch 的在途加载直接复用；不同账号/epoch 必须允许
      // 发起新请求，否则换账号会被旧账号的忙位卡死在“同步中”。
      return this._profileDataRequest;
    }

    const flowSeq = (this._profileDataSeq = (this._profileDataSeq || 0) + 1);
    this._profileDataBusy = true;
    this._profileDataBusyKey = busyKey;
    const request = (async () => {
      try {
        const bindingState = await this.loadDeviceBindingSync();
        if (
          !bindingState ||
          flowSeq !== this._profileDataSeq ||
          !api.isAuthEpochCurrent(authEpoch) ||
          !api.currentIdentity()
        ) {
          return null;
        }
        await Promise.all([
          this.loadProfile(),
          this.loadStats(),
          this.loadDeliveredCapabilities(),
        ]);
        return bindingState;
      } finally {
        // 只有仍持有同一 busy key 的请求才能清忙位；换账号后旧请求迟到
        // 时不能清掉新账号正在进行的加载忙位。
        if (this._profileDataBusyKey === busyKey) {
          this._profileDataBusy = false;
          this._profileDataBusyKey = "";
        }
        if (this._profileDataRequest === request) {
          this._profileDataRequest = null;
        }
      }
    })();
    this._profileDataRequest = request;
    return request;
  },

  async retryDeviceBindingSync() {
    if (!(await requireLogin({ reason: "view_profile" }))) {
      this._enterGuestState();
      return;
    }
    this.setData({ authenticated: true });
    await this._loadAuthenticatedData();
  },

  async chooseDeviceBinding(event) {
    if (this._profileDataBusy || this._deviceBindingBusy) return;
    const bindingId = event.currentTarget.dataset.bindingId;
    const choice = this.data.deviceBindingChoices.find(
      (item) => item.bindingId === bindingId,
    );
    if (!choice) return;

    const flowSeq = (this._deviceBindingSeq = (this._deviceBindingSeq || 0) + 1);
    const authEpoch = api.currentAuthEpoch();
    try {
      const binding = api.selectDeviceBinding(choice.binding);
      if (this._deviceBindingStale(flowSeq, authEpoch)) return;
      this.setData({
        ...deviceBindingStateData({
          status: "ready",
          binding,
          bindings: this.data.deviceBindingChoices.map((item) => item.binding),
        }),
        deviceBindingDetail: "已选择当前设备，正在刷新页面状态。",
      });
      await this._loadAuthenticatedData();
    } catch (error) {
      if (this._deviceBindingStale(flowSeq, authEpoch)) return;
      this.setData({
        deviceBindingError: error?.message || "无法切换到这台设备，请重试。",
      });
    }
  },

  async loadProfile() {
    const identity = api.currentIdentity();
    if (!identity) return;
    const authEpoch = api.currentAuthEpoch();
    this.setData({ loading: true, identity, error: "" });
    try {
      const profile = { ...defaultProfile, ...(await api.getProfile(identity.user_id)) };
      if (!api.isAuthEpochCurrent(authEpoch)) return;
      // 使用人身份来自设备绑定，这里不再读取说话人识别（speaker enrollment）状态。
      const [capabilityState] = await Promise.all([
        this.loadRuntimeCapabilities(),
              ]);
      const customPersonaState = capabilityState.customPersonaId
        ? await this.loadCustomPersonaLabel(capabilityState.customPersonaId)
        : { customPersonaId: "", customPersonaLabel: "" };
      if (!api.isAuthEpochCurrent(authEpoch)) return;
      const companion = companionById(profile.companion_id);
      this.setData({
        profile,
        profileInitial: profileInitialFor(profile.display_name),
        isMinor: profile.subject_category === "minor",
        customPersonaId: customPersonaState.customPersonaId,
        customPersonaLabel: customPersonaState.customPersonaLabel,
        personaHeadline: `${companion.name}，你的日常角色`,
        accountId: identity.user_id,
        ...capabilityState,
      });
      // 回到这一页（onShow 会重新加载）时，若服务端仍在生成就把进度接上。
    } catch (error) {
      if (!api.isAuthEpochCurrent(authEpoch)) return;
      this.setData({ error: error?.message || "个人资料无法加载。" });
    } finally {
      if (api.isAuthEpochCurrent(authEpoch)) this.setData({ loading: false });
    }
  },

  async loadRuntimeCapabilities() {
    const binding = readBindingManifest();
    if (!binding || typeof binding.device_id !== "string") {
      const reasonByState = {
        idle: "设备绑定尚未同步，暂不能读取 Runtime Profile。",
        syncing: "设备绑定正在同步，暂不能读取 Runtime Profile。",
        choose: "请先选择当前设备，再读取 Runtime Profile。",
        error: "设备绑定同步失败，暂不能读取 Runtime Profile。",
        empty: "服务端确认还没有绑定设备，无法取得 Runtime Profile。",
      };
      return {
        hasRuntimeProfile: false,
        runtimeCapabilities: [],
        speakerEntryAllowed: false,
        digitalSelfEntryAllowed: false,
        guardianEntryAllowed: false,
        rawVoiceEntryAllowed: false,
        customPersonaId: "",
        profileUnavailableReason: reasonByState[this.data.deviceBindingState] ||
          "设备绑定状态不完整，无法取得 Runtime Profile。",
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
          customPersonaId: "",
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
          customPersonaId: "",
          profileUnavailableReason: "服务端返回的 Runtime Profile 校验失败，敏感能力已关闭。",
        };
      }
      const capabilities = profile?.capabilities || [];
      const state = {
        hasRuntimeProfile: true,
        runtimeCapabilities: capabilities,
        speakerEntryAllowed: false,
        digitalSelfEntryAllowed: entryAllowed(profile, contracts.Capability.DigitalSelfPreview),
        guardianEntryAllowed: entryAllowed(profile, contracts.Capability.GuardianSummaryView),
        rawVoiceEntryAllowed: entryAllowed(profile, contracts.Capability.RawAudioRetention),
        customPersonaId: customPersonaIdFor(profile),
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
        customPersonaId: "",
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
    if (this.data.deviceBindingState !== "ready" && this.data.deviceBindingState !== "cached") {
      this.setData({ stats: { totalDays: 0, moments: 0, streak: 0 } });
      return;
    }
    // 回顾统计是账号自己的数据，服务端按登录账号鉴权，不走 Runtime Profile 门禁。
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
    const next = event.detail.value;
    const previous = this.data.profile[key];
    this.setData({ [`profile.${key}`]: next, error: "" });
    this._savePreference(key, next, previous);
  },

  async _savePreference(key, next, previous) {
    const identity = api.currentIdentity();
    if (!identity) {
      this.setData({ [`profile.${key}`]: previous });
      return;
    }
    const authEpoch = api.currentAuthEpoch();
    try {
      const profile = await api.updateProfile(identity.user_id, {
        ...this.data.profile,
        [key]: next,
      });
      if (!api.isAuthEpochCurrent(authEpoch)) return;
      this.setData({ profile: { ...this.data.profile, ...profile } });
      wx.showToast({ title: "已保存", icon: "success" });
    } catch (error) {
      if (!api.isAuthEpochCurrent(authEpoch)) return;
      this.setData({
        [`profile.${key}`]: previous,
        error: error?.message || "保存失败。",
      });
      wx.showToast({ title: "没保存成功，已恢复原来的设置", icon: "none" });
    }
  },

  async chooseCompanion(event) {
    if (!(await requireLogin({ reason: "edit_profile" }))) return;
    const companionId = event.currentTarget.dataset.id;
    const nextProfile = {
      ...this.data.profile,
      companion_id: companionId,
    };
    this.setData({ profile: nextProfile });
    await this.saveProfile();
  },

  // 自定义人格是账号级的不可变记录（cu_*），只在专门的页面里创建/删除；
  // 这里只负责显示当前分配给它的人。
  openCustomPersona() {
    wx.navigateTo({ url: "/pages/persona-custom/index" });
  },

  async loadCustomPersonaLabel(personaId) {
    const authEpoch = api.currentAuthEpoch();
    try {
      const payload = await api.listPersonas();
      if (!api.isAuthEpochCurrent(authEpoch)) return { customPersonaId: "", customPersonaLabel: "" };
      const record = (payload?.custom_personas || []).find(
        (item) => item.persona_id === personaId,
      );
      if (!record) return { customPersonaId: "", customPersonaLabel: "" };
      return { customPersonaId: record.persona_id, customPersonaLabel: record.display_name };
    } catch {
      // 目录读取失败只影响展示：不提交 custom_persona_id，避免把未知 id 交给服务端。
      return { customPersonaId: "", customPersonaLabel: "" };
    }
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
      const merged = { ...this.data.profile, ...profile };
      this.setData({
        profile: merged,
        profileInitial: profileInitialFor(merged.display_name),
      });
      wx.showToast({ title: "已保存", icon: "success" });
    } catch (error) {
      if (!api.isAuthEpochCurrent(authEpoch)) return;
      this.setData({ error: error?.message || "保存失败。" });
    } finally {
      if (api.isAuthEpochCurrent(authEpoch)) this.setData({ saving: false });
    }
  },

  // 轮询会反复取回同一份快照：只有视图真的变了才写回，避免无谓渲染。
  copyAccountId() {
    const accountId = this.data.accountId;
    if (!accountId) return;
    wx.setClipboardData({
      data: accountId,
      success: () => wx.showToast({ title: "已复制账号编号", icon: "none" }),
    });
  },

  async openDigitalSelf() {
    if (!(await requireLogin({ reason: "view_profile" }))) return;
    if (!this._allowSensitiveEntry("数字分身", contracts.Capability.DigitalSelfPreview)) return;
    wx.navigateTo({ url: "/pages/digital-self/index" });
  },


  openCompanion() {
    wx.switchTab({ url: "/pages/companion/index" });
  },

  openDevice() {
    wx.switchTab({ url: "/pages/device/index" });
  },

  async openGuardianSummary() {
    if (!(await requireLogin({ reason: "view_guardian_summary" }))) return;
    if (!this._allowSensitiveEntry("成长小结", contracts.Capability.GuardianSummaryView)) return;
    wx.navigateTo({ url: "/pages/guardian/index" });
  },

  // The *EntryAllowed flags come from entryAllowed(), which also opens
  // action-time capabilities (digital self, raw audio) on a valid profile.
  // Checking the capability list directly would keep those entries shut forever.
  _allowSensitiveEntry(label, capability) {
    const flagByCapability = {
      [contracts.Capability.DigitalSelfPreview]: "digitalSelfEntryAllowed",
      [contracts.Capability.GuardianSummaryView]: "guardianEntryAllowed",
      [contracts.Capability.RawAudioRetention]: "rawVoiceEntryAllowed",
    };
    const flag = flagByCapability[capability];
    if (flag ? this.data[flag] === true : this.data.runtimeCapabilities.includes(capability)) {
      return true;
    }
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
      confirmColor: "#b3261e",
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
