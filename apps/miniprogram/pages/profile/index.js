const api = require("../../utils/api");
const compliance = require("../../utils/compliance");
const { companions, companionById, defaultCompanionId } = require("../../utils/companions");
const { encodeCustomPersona, parseCustomPersona } = require("../../utils/custom-persona");
const { requireLogin } = require("../../utils/auth-gate");
const { MODE_META, readBindingManifest } = require("../../utils/device-binding");
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

function voiceCloneStatusLabel(payload) {
  if (!payload) return "暂时读不到自定义声音状态。";
  const items = Array.isArray(payload.items) ? payload.items : [];
  if (items.some((item) => item.status === "active")) {
    return "自定义声音已就绪。下次在设备上说话就会用；听着不像，再录一段即可。";
  }
  if (items.some((item) => item.status === "enrolling")) {
    return "正在生成自定义声音，通常一分钟内完成。结果就在这一页，不用去别处看。";
  }
  if (items.some((item) => item.status === "candidate")) {
    return "声音已经生成，正在接到设备上。";
  }
  if (items.some((item) => item.status === "failed")) {
    return "这次没生成成功。请换一段 10–60 秒、更清楚的人声再试。";
  }
  if (payload.consent && !payload.consent.revoked_at) {
    return "还没有声音样本。录一段或上传音频后，大约一分钟就能用。";
  }
  return "还没有自定义声音样本。";
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
    companionImage: companionById(defaultCompanionId).image,
    personaHeadline: `${companionById(defaultCompanionId).name}，你的日常角色`,
    accountId: "",
    speakerEnrollmentHint: "识别身份，不等同于自定义声音",
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
    customPersonaActive: false,
    customPersonaName: "",
    customPersonaText: "",
    voiceCloneAllowed: false,
    voiceCloneStatusLabel: "还没有自定义声音样本。",
    voiceSampleBusy: false,
    recording: false,
    recordSeconds: 0,
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
    this._stopRecordingTimer();
    if (this.data.recording && this._recorder) {
      try {
        this._recorder.stop();
      } catch {
        // Recorder stop is best effort when leaving the page.
      }
    }
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
      speakerEnrollmentState: "blocked",
      speakerEnrollmentBlockReason: "",
      speakerEnrollmentRemediationSteps: [],
      speakerEnrollmentProfileCount: 0,
      deliveredCapabilities: [],
      deliveredCapabilitiesLoading: false,
      customPersonaActive: false,
      customPersonaName: "",
      customPersonaText: "",
      voiceCloneAllowed: false,
      voiceCloneStatusLabel: "还没有自定义声音样本。",
      voiceSampleBusy: false,
      recording: false,
      recordSeconds: 0,
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
      const custom = parseCustomPersona(profile.bio);
      const [capabilityState, speakerState, voiceCloneState] = await Promise.all([
        this.loadRuntimeCapabilities(),
        this.loadSpeakerEnrollmentStatus(),
        this.loadVoiceCloneStatus(),
      ]);
      if (!api.isAuthEpochCurrent(authEpoch)) return;
      const speakerHint = {
        active: "已允许用于识别本人，可随时在设备上重新录制",
        pending: "上一版声纹还没生效。请唤醒设备，按提示再说几句话。",
        requested: "已记录授权。唤醒设备，按提示说几句话即可。手机不录音。",
        required: "在设备上完成，不在手机采集",
      }[speakerState.speakerEnrollmentState] || speakerState.speakerEnrollmentBlockReason || "识别身份，不等同于自定义声音";
      const companion = companionById(profile.companion_id);
      this.setData({
        profile,
        profileInitial: profileInitialFor(profile.display_name),
        isMinor: profile.subject_category === "minor",
        customPersonaActive: custom.active,
        customPersonaName: custom.name,
        customPersonaText: custom.text,
        companionImage: companion.image,
        personaHeadline: `${custom.active ? custom.name || companion.name : companion.name}，你的日常角色`,
        accountId: identity.user_id,
        speakerEnrollmentHint: speakerHint,
        ...capabilityState,
        ...speakerState,
        ...voiceCloneState,
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
        voiceCloneAllowed: false,
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
          voiceCloneAllowed: false,
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
          voiceCloneAllowed: false,
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
        voiceCloneAllowed: capabilities.includes(contracts.Capability.VoiceCloneUse),
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
        voiceCloneAllowed: false,
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
    if (parseCustomPersona(nextProfile.bio).active) {
      nextProfile.bio = "";
    }
    this.setData({
      profile: nextProfile,
      customPersonaActive: false,
    });
    await this.saveProfile();
  },

  chooseCustomPersona() {
    this.setData({ customPersonaActive: true });
  },

  onCustomPersonaName(event) {
    this.setData({ customPersonaName: event.detail.value });
  },

  onCustomPersonaText(event) {
    this.setData({ customPersonaText: event.detail.value });
  },

  async saveCustomPersona() {
    if (!(await requireLogin({ reason: "edit_profile" }))) return;
    const encoded = encodeCustomPersona({
      name: this.data.customPersonaName,
      text: this.data.customPersonaText,
    });
    if (!encoded) {
      wx.showToast({ title: "请填写名字和人格描述", icon: "none" });
      return;
    }
    this.setData({
      customPersonaActive: true,
      "profile.bio": encoded,
      "profile.companion_id": this.data.profile.companion_id || defaultCompanionId,
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
      const merged = { ...this.data.profile, ...profile };
      const custom = parseCustomPersona(merged.bio);
      this.setData({
        profile: merged,
        profileInitial: profileInitialFor(merged.display_name),
        customPersonaActive: custom.active,
        customPersonaName: custom.name || this.data.customPersonaName,
        customPersonaText: custom.text || this.data.customPersonaText,
      });
      wx.showToast({ title: "已保存", icon: "success" });
    } catch (error) {
      if (!api.isAuthEpochCurrent(authEpoch)) return;
      this.setData({ error: error?.message || "保存失败。" });
    } finally {
      if (api.isAuthEpochCurrent(authEpoch)) this.setData({ saving: false });
    }
  },

  async loadVoiceCloneStatus() {
    try {
      let payload = await api.listVoiceProfiles();
      const pending = (payload.items || []).find((item) => item.status === "candidate");
      if (pending?.profile_id) {
        try {
          await api.readyVoiceForDevice(pending.profile_id);
          payload = await api.listVoiceProfiles();
        } catch {
          // Keep the listing we already have; the page still explains the current state.
        }
      }
      return { voiceCloneStatusLabel: voiceCloneStatusLabel(payload) };
    } catch (error) {
      if (error?.status === 403) {
        return { voiceCloneStatusLabel: "当前未开启声音复刻，或尚未完成授权。" };
      }
      return { voiceCloneStatusLabel: error?.message || "暂时读不到自定义声音状态。" };
    }
  },

  _stopRecordingTimer() {
    if (this._recordTimer) {
      clearInterval(this._recordTimer);
      this._recordTimer = null;
    }
  },

  _ensureRecorder() {
    if (this._recorder) return this._recorder;
    const recorder = wx.getRecorderManager();
    recorder.onStart(() => {
      this._stopRecordingTimer();
      this.setData({ recording: true, recordSeconds: 0 });
      this._recordTimer = setInterval(() => {
        this.setData({ recordSeconds: this.data.recordSeconds + 1 });
      }, 1000);
    });
    recorder.onStop((result) => {
      this._stopRecordingTimer();
      this.setData({ recording: false });
      this._submitRecordedSample(result);
    });
    recorder.onError((error) => {
      this._stopRecordingTimer();
      this.setData({ recording: false, voiceSampleBusy: false });
      wx.showToast({ title: error?.errMsg || "录音失败", icon: "none" });
    });
    this._recorder = recorder;
    return recorder;
  },

  async _ensureRecordPermission() {
    const setting = await new Promise((resolve) => {
      wx.getSetting({
        success: resolve,
        fail: () => resolve({ authSetting: {} }),
      });
    });
    if (setting?.authSetting?.["scope.record"]) return true;
    const authorized = await new Promise((resolve) => {
      wx.authorize({
        scope: "scope.record",
        success: () => resolve(true),
        fail: () => resolve(false),
      });
    });
    if (authorized) return true;
    const open = await new Promise((resolve) => {
      wx.showModal({
        title: "需要麦克风权限",
        content: "录制自定义声音样本需要麦克风。请在设置中允许。",
        confirmText: "去设置",
        success: (result) => resolve(Boolean(result.confirm)),
      });
    });
    if (!open) return false;
    await new Promise((resolve) => {
      wx.openSetting({ complete: resolve });
    });
    const after = await new Promise((resolve) => {
      wx.getSetting({
        success: resolve,
        fail: () => resolve({ authSetting: {} }),
      });
    });
    return Boolean(after?.authSetting?.["scope.record"]);
  },

  async toggleVoiceRecord() {
    if (!(await requireLogin({ reason: "edit_profile" }))) return;
    if (this.data.voiceSampleBusy) return;
    if (this.data.recording) {
      this._ensureRecorder().stop();
      return;
    }
    if (!(await this._ensureRecordPermission())) return;
    this._ensureRecorder().start({
      duration: 60000,
      sampleRate: 16000,
      numberOfChannels: 1,
      encodeBitRate: 48000,
      format: "mp3",
    });
  },

  async uploadVoiceSample() {
    if (!(await requireLogin({ reason: "edit_profile" }))) return;
    if (this.data.voiceSampleBusy || this.data.recording) return;
    const chosen = await new Promise((resolve, reject) => {
      wx.chooseMessageFile({
        count: 1,
        type: "file",
        extension: ["mp3", "wav", "m4a", "aac", "ogg", "flac"],
        success: resolve,
        fail: (error) => {
          if (String(error?.errMsg || "").includes("cancel")) resolve(null);
          else reject(error);
        },
      });
    });
    const file = chosen?.tempFiles?.[0];
    if (!file?.path) return;
    const mediaType = mediaTypeForPath(file.path) || mediaTypeForPath(file.name);
    if (!mediaType) {
      wx.showToast({ title: "请选择 wav / mp3 / m4a 音频", icon: "none" });
      return;
    }
    const size = Number(file.size) || 0;
    if (size < 20000) {
      wx.showToast({ title: "音频太短，请使用 10–60 秒样本", icon: "none" });
      return;
    }
    await this._enrollVoiceSample({
      filePath: file.path,
      mediaType,
      durationMs: clampCloneDuration(estimateDurationMs(size, mediaType)),
      sampleRate: 16000,
    });
  },

  async _submitRecordedSample(result) {
    const duration = Number(result?.duration) || 0;
    if (duration < 10000) {
      wx.showToast({ title: "请至少录 10 秒", icon: "none" });
      return;
    }
    if (!result?.tempFilePath) {
      wx.showToast({ title: "没有录到声音", icon: "none" });
      return;
    }
    await this._enrollVoiceSample({
      filePath: result.tempFilePath,
      mediaType: "audio/mpeg",
      durationMs: clampCloneDuration(duration),
      sampleRate: 16000,
    });
  },

  async _enrollVoiceSample({ filePath, mediaType, durationMs, sampleRate }) {
    this.setData({
      voiceSampleBusy: true,
      error: "",
      voiceCloneStatusLabel: "正在生成自定义声音，大约一分钟，请先留在这一页。",
    });
    try {
      const audioBase64 = await new Promise((resolve, reject) => {
        wx.getFileSystemManager().readFile({
          filePath,
          encoding: "base64",
          success: (file) => resolve(file.data),
          fail: (error) => reject(error),
        });
      });
      try {
        await api.grantVoiceCloneConsent();
      } catch (error) {
        if (error?.status !== 409) throw error;
      }
      const enrolled = await api.enrollVoiceClone({
        audioBase64,
        mediaType,
        durationMs,
        sampleRate,
        enrollmentKey: `miniprogram-${Date.now()}`,
        readyForDevice: true,
      });
      const voiceCloneState = await this.loadVoiceCloneStatus();
      this.setData(voiceCloneState);
      wx.showToast({
        title: enrolled?.status === "active" ? "声音已就绪" : "声音处理中",
        icon: "success",
      });
    } catch (error) {
      const message = error?.message || "这次没生成成功，请再试一次。";
      this.setData({
        error: message,
        voiceCloneStatusLabel: message,
      });
    } finally {
      this.setData({ voiceSampleBusy: false });
    }
  },

  async loginFromProfile() {
    if (!(await requireLogin({ reason: "view_profile" }))) return;
    this.setData({ authenticated: true });
    await this._loadAuthenticatedData();
  },

  openCompanion() {
    wx.navigateTo({ url: "/pages/companion/index" });
  },

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
