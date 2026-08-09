const api = require("../../utils/api");
const { companionById, defaultCompanionId } = require("../../utils/companions");
const { MiniProgramMediaSession } = require("../../utils/media-gateway");
const {
  acceptTranscriptRevision,
  assistantStreamingTranscript,
  authoritativeTranscript,
} = require("../../utils/transcript-events");
const { requireLogin } = require("../../utils/auth-gate");

const CONNECTION_REFUSED_RETRY_DELAY_MS = 400;

const defaultProfile = {
  display_name: "新朋友",
  companion_id: defaultCompanionId,
  voice_reply: true,
  subject_category: null,
};

const SESSION_FOCUS_OPTIONS = Object.freeze([
  { value: "chat", label: "自在陪伴", description: "像平常一样聊聊" },
  { value: "tutor_english", label: "英语口语", description: "情景对话与温和纠音" },
  { value: "tutor_homework", label: "作业陪伴", description: "讲思路，不直接给答案" },
]);

// 与 H5（apps/h5/src/lib/emotion.js）一致：负面声学标签统一表现为关切，
// 不机械镜像愤怒或厌恶；其余回落 neutral。
const VOICE_EMOTION_EXPRESSIONS = {
  happy: "happy",
  surprised: "curious",
  sad: "caring",
  angry: "caring",
  fearful: "caring",
  disgusted: "caring",
};
const VOICE_EMOTION_LABELS = new Set([
  "neutral",
  "happy",
  "sad",
  "angry",
  "fearful",
  "disgusted",
  "surprised",
]);
const ASSISTANT_EXPRESSIONS = new Set([
  "neutral",
  "happy",
  "curious",
  "caring",
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

function stateLabel(state, inputMode = "voice") {
  const textMode = inputMode === "text";
  return {
    idle: "选择一种方式开始",
    connecting: textMode ? "正在进入文字对话" : "正在连接",
    listening: textMode ? "文字对话中" : "我在听",
    thinking: "正在想",
    speaking: textMode ? "正在回复" : "正在回应",
    reconnecting: "正在恢复连接",
    closed: "对话已结束",
  }[state] || "正在准备";
}

function authorizationForRecord() {
  return new Promise((resolve, reject) => {
    wx.authorize({
      scope: "scope.record",
      success: resolve,
      fail: () => {
        wx.showModal({
          title: "需要麦克风权限",
          content: "请在设置中允许麦克风权限后，再开始语音陪伴。",
          confirmText: "去设置",
          success(result) {
            if (result.confirm) {
              wx.openSetting({
                success(setting) {
                  if (setting.authSetting?.["scope.record"]) resolve();
                  else reject(new Error("未获得麦克风权限。"));
                },
                fail: reject,
              });
            }
            else reject(new Error("未获得麦克风权限。"));
          },
        });
      },
    });
  });
}

function wait(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

Page({
  data: {
    greeting: greeting(),
    profile: defaultProfile,
    companion: companionById(defaultCompanionId),
    status: "idle",
    statusLabel: stateLabel("idle"),
    error: "",
    transcript: [],
    inputMode: "voice",
    textDraft: "",
    textTurnPending: false,
    micEnabled: true,
    sessionId: "",
    sessionFocus: "chat",
    sessionFocusOptions: SESSION_FOCUS_OPTIONS,
    connecting: false,
    active: false,
    authenticated: false,
    expression: "neutral",
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
    this.loadProfile();
  },

  onUnload() {
    this._invalidateVoiceAttempt();
    if (this._unsubscribeAuthCleared) {
      this._unsubscribeAuthCleared();
      this._unsubscribeAuthCleared = null;
    }
    this._resetExpression();
    this._endMediaLocally();
  },

  onPullDownRefresh() {
    if (!api.hasAuthenticatedSession()) {
      wx.stopPullDownRefresh();
      return;
    }
    this.loadProfile().finally(() => wx.stopPullDownRefresh());
  },

  async loadProfile() {
    const identity = api.currentIdentity();
    if (!identity) return;
    const authEpoch = api.currentAuthEpoch();
    try {
      const profile = { ...defaultProfile, ...(await api.getProfile(identity.user_id)) };
      if (!api.isAuthEpochCurrent(authEpoch)) return;
      const companion = companionById(profile.companion_id);
      this.setData({
        profile,
        companion,
        ...mascotAssetsFor(companion),
      });
    } catch (error) {
      if (!api.isAuthEpochCurrent(authEpoch)) return;
      this.setData({ error: error?.message || "个人资料暂时无法加载。" });
    }
  },

  startVoice() {
    return this._startConversation("voice");
  },

  startText() {
    return this._startConversation("text");
  },

  selectSessionFocus(event) {
    if (this.data.active || this.data.connecting) return;
    const focus = event.currentTarget.dataset.focus;
    if (SESSION_FOCUS_OPTIONS.some((item) => item.value === focus)) {
      this.setData({ sessionFocus: focus, error: "" });
    }
  },

  _startConversation(inputMode) {
    if (this._startConversationPromise) return this._startConversationPromise;
    if (this.data.connecting || this.data.active) return Promise.resolve();
    const attemptId = this._invalidateVoiceAttempt();
    let attempt;
    attempt = this._startVoiceOnce(attemptId, inputMode).finally(() => {
      if (this._startConversationPromise === attempt) {
        this._startConversationPromise = null;
      }
    });
    this._startConversationPromise = attempt;
    return attempt;
  },

  async _startVoiceOnce(attemptId, inputMode = "voice") {
    if (!(await requireLogin({ reason: inputMode === "text" ? "start_text" : "start_voice" }))) {
      return;
    }
    if (!this._isVoiceAttemptCurrent(attemptId)) return;
    this.setData({ authenticated: true });
    const identity = api.currentIdentity();
    if (!identity) return;
    await this.loadProfile();
    if (!this._isVoiceAttemptCurrent(attemptId)) return;
    this.setData({
      connecting: true,
      status: "connecting",
      statusLabel: stateLabel("connecting", inputMode),
      error: "",
      transcript: [],
      inputMode,
      textDraft: "",
      textTurnPending: false,
      micEnabled: inputMode === "voice",
      sessionId: "",
    });
    this._ending = false;
    this._session = null;
    this._resetExpression();
    this._transcriptRevisionByTurn = new Map();
    try {
      if (inputMode === "voice") {
        await authorizationForRecord();
        if (!this._isVoiceAttemptCurrent(attemptId)) return;
      }
      const session = await api.createMiniProgramSession({
        userId: identity.user_id,
        sessionFocus: this.data.sessionFocus,
      });
      if (!this._isVoiceAttemptCurrent(attemptId)) return;
      this._session = session;
      this.setData({ sessionId: session.session_id });
      this._session = await this._connectInitialMedia(session, inputMode);
      if (!this._isVoiceAttemptCurrent(attemptId)) {
        await this._endMediaLocally();
        return;
      }
      this.setData({
        active: true,
      });
    } catch (error) {
      await this._endMediaLocally();
      if (!this._isVoiceAttemptCurrent(attemptId)) return;
      const canRetry = Boolean(this._session?.session_id);
      this.setData({
        active: false,
        status: canRetry ? "closed" : "idle",
        statusLabel: stateLabel(canRetry ? "closed" : "idle"),
        error: error?.message || (inputMode === "text" ? "文字对话未能开始。" : "语音会话未能开始。"),
      });
    } finally {
      this.setData({ connecting: false });
    }
  },

  _invalidateVoiceAttempt() {
    this._voiceAttemptId = (this._voiceAttemptId || 0) + 1;
    return this._voiceAttemptId;
  },

  _isVoiceAttemptCurrent(attemptId) {
    return attemptId === this._voiceAttemptId && api.hasAuthenticatedSession();
  },

  _enterGuestState() {
    this._invalidateVoiceAttempt();
    this._ending = true;
    this._session = null;
    this._endMediaLocally().catch(() => {});
    this._resetExpression();
    this._transcriptRevisionByTurn = new Map();
    const companion = companionById(defaultCompanionId);
    this.setData({
      authenticated: false,
      profile: defaultProfile,
      companion,
      ...mascotAssetsFor(companion),
      status: "idle",
      statusLabel: stateLabel("idle"),
      error: "",
      transcript: [],
      inputMode: "voice",
      textDraft: "",
      textTurnPending: false,
      micEnabled: true,
      sessionId: "",
      sessionFocus: "chat",
      connecting: false,
      active: false,
    });
  },

  async _connectInitialMedia(session, inputMode = this.data.inputMode) {
    try {
      await this._connectMedia(session, inputMode);
      return session;
    } catch (error) {
      await this._endMediaLocally();
      if (error?.code !== "socket_connection_refused") throw error;
      this.setData({
        status: "reconnecting",
        statusLabel: stateLabel("reconnecting"),
        error: "网络连接暂时被拒绝，正在重试一次。",
      });
      await wait(CONNECTION_REFUSED_RETRY_DELAY_MS);
      const mediaGateway = await api.refreshMiniProgramGatewayTicket(session.session_id);
      const recovered = {
        ...session,
        media_gateway: mediaGateway,
      };
      await this._connectMedia(recovered, inputMode);
      return recovered;
    }
  },

  async _connectMedia(session, inputMode = this.data.inputMode) {
    let media;
    media = new MiniProgramMediaSession(session, {
      microphoneEnabled: inputMode === "voice",
      playbackEnabled: inputMode === "voice",
      onEvent: (event) => {
        if (this._media === media) this._onGatewayEvent(event);
      },
      onClose: () => {
        if (this._media === media) this._onMediaClosed();
      },
      onError: (message) => {
        if (this._media === media) this.setData({ error: message });
      },
      onInterrupted: (message) => {
        if (this._media === media) this._onMediaInterrupted(message);
      },
    });
    this._media = media;
    await media.connect();
  },

  _onGatewayEvent(event) {
    if (event?.type === "ready") {
      this._setStatus("listening");
      return;
    }
    if (event?.type === "transport_state") {
      this._setStatus(event.state === "reconnecting" ? "reconnecting" : "listening");
      return;
    }
    if (event?.type === "gateway_error") {
      this.setData({ error: "语音服务连接已中断，请轻触重新开始。" });
      return;
    }
    const transcript = authoritativeTranscript(event);
    if (transcript) {
      this._appendTranscript(transcript);
      return;
    }
    const assistantStream =
      this.data.inputMode === "text"
        ? assistantStreamingTranscript(event)
        : null;
    const assistantFence = this._assistantStateFence;
    if (
      assistantStream &&
      this.data.status === "speaking" &&
      assistantFence?.turnId === assistantStream.turnId &&
      assistantFence.generationId === assistantStream.generationId
    ) {
      this._appendTranscript(assistantStream);
      return;
    }
    if (event?.type !== "ui_event") return;
    const payload = event.event || {};
    if (payload.type === "assistant_expression") {
      this._onAssistantExpression(payload);
      return;
    }
    if (payload.type === "emotion_observation") {
      this._onEmotionObservation(payload);
      return;
    }
    if (payload.type === "assistant_state") {
      const mapped = {
        ready: "listening",
        speaker_enroll: "listening",
        listening: "listening",
        user_speaking: "listening",
        eot_pending: "listening",
        backchannel: "listening",
        thinking: "thinking",
        thinking_silent: "thinking",
        tool_waiting: "thinking",
        speaking: "speaking",
        interrupted: "listening",
        interruption_pending: "listening",
        recovering: "reconnecting",
        closed: "closed",
      }[payload.state];
      if (mapped) this._setStatus(mapped, payload);
      return;
    }
  },

  _onAssistantExpression(payload) {
    const { expression, session_id: sessionId, turn_id: turnId, generation_id: generationId } = payload;
    if (sessionId !== this._session?.session_id) return;
    if (
      !ASSISTANT_EXPRESSIONS.has(expression) ||
      !Number.isInteger(turnId) ||
      !Number.isInteger(generationId) ||
      !Number.isInteger(payload.tool_epoch) ||
      payload.tool_epoch < 0
    ) {
      return;
    }
    const stateFence = this._assistantStateFence;
    if (
      stateFence &&
      (generationId < stateFence.generationId ||
        (generationId === stateFence.generationId &&
          (turnId !== stateFence.turnId ||
            payload.tool_epoch < stateFence.toolEpoch)))
    ) {
      return;
    }
    if (
      stateFence &&
      generationId === stateFence.generationId &&
      turnId === stateFence.turnId &&
      payload.tool_epoch !== stateFence.toolEpoch &&
      !["thinking", "speaking"].includes(this.data.status)
    ) {
      return;
    }
    const next = { expression, turnId, generationId, toolEpoch: payload.tool_epoch };
    if (
      this.data.status === "speaking" &&
      stateFence &&
      stateFence.turnId === turnId &&
      stateFence.generationId === generationId &&
      stateFence.toolEpoch === payload.tool_epoch
    ) {
      this._activateAssistantExpression(next);
      return;
    }
    this._pendingAssistantExpression = next;
  },

  _activateAssistantExpression(next) {
    if (this._expressionTimer) {
      clearTimeout(this._expressionTimer);
      this._expressionTimer = null;
    }
    this._assistantExpression = next;
    this.setData({ expression: next.expression });
  },

  _clearAssistantExpression(generationId) {
    const active = this._assistantExpression;
    if (
      active &&
      Number.isInteger(generationId) &&
      generationId < active.generationId
    ) {
      return;
    }
    this._assistantExpression = null;
    if (
      this._pendingAssistantExpression &&
      Number.isInteger(generationId) &&
      this._pendingAssistantExpression.generationId <= generationId
    ) {
      this._pendingAssistantExpression = null;
    }
    if (active) this.setData({ expression: "neutral" });
  },

  // 情绪只响应当前会话中已被权威用户终稿确认的 emotion_observation；
  // 不用助手字幕关键词反向驱动表情（与 H5 约束一致）。
  _onEmotionObservation(payload) {
    const label = payload.label;
    const turnId = payload.turn_id;
    const generationId = payload.generation_id;
    const ttl = payload.expires_after_ms;
    if (typeof label !== "string" || !VOICE_EMOTION_LABELS.has(label)) return;
    if (payload.persist !== false) return;
    if (!Number.isInteger(turnId) || !Number.isInteger(generationId)) return;
    if (!(ttl > 0 && ttl <= 60000)) return;
    const expression = VOICE_EMOTION_EXPRESSIONS[label] || "neutral";
    if (this._lastUserTurnId && turnId === this._lastUserTurnId) {
      this._activateExpression(expression, ttl);
    } else if (this._lastUserTurnId && turnId === this._lastUserTurnId + 1) {
      // 目标话轮的权威终稿可能稍后到达，先挂起等 transcript 到达再激活
      this._pendingExpression = { expression, turnId, ttl };
    }
    // 其余迟到 / 乱序事件直接丢弃
  },

  _activateExpression(expression, ttl) {
    if (this._expressionTimer) {
      clearTimeout(this._expressionTimer);
      this._expressionTimer = null;
    }
    this.setData({ expression });
    this._expressionTimer = setTimeout(() => {
      this._expressionTimer = null;
      if (!this._assistantExpression) this.setData({ expression: "neutral" });
    }, ttl);
  },

  _resetExpression() {
    if (this._expressionTimer) {
      clearTimeout(this._expressionTimer);
      this._expressionTimer = null;
    }
    this._pendingExpression = null;
    this._pendingAssistantExpression = null;
    this._assistantExpression = null;
    this._assistantStateFence = null;
    this._lastUserTurnId = 0;
    this.setData({ expression: "neutral" });
  },

  _appendTranscript(item) {
    this._transcriptRevisionByTurn ||= new Map();
    if (
      item.source === "authoritative" &&
      !acceptTranscriptRevision(this._transcriptRevisionByTurn, item)
    ) {
      return;
    }
    const current = this.data.transcript?.[0];
    if (
      item.source === "display" &&
      current?.source === "authoritative" &&
      current.speaker === "assistant" &&
      current.turnId === item.turnId &&
      current.generationId === item.generationId
    ) {
      return;
    }
    const key =
      Number.isInteger(item.turnId) && Number.isInteger(item.generationId)
        ? `${item.speaker}:${item.turnId}:${item.generationId}`
        : "";
    const next = {
      ...item,
      key,
      id: key || `${Date.now()}-${Math.random()}`,
    };
    this.setData({ transcript: [next] });
    if (
      next.speaker === "user" &&
      next.final &&
      next.source === "authoritative" &&
      Number.isInteger(next.turnId)
    ) {
      this._lastUserTurnId = next.turnId;
      if (this._pendingExpression && this._pendingExpression.turnId === next.turnId) {
        const pending = this._pendingExpression;
        this._pendingExpression = null;
        this._activateExpression(pending.expression, pending.ttl);
      }
    }
  },

  _setStatus(status, event = {}) {
    const turnId = event.turn_id;
    const generationId = event.generation_id;
    const toolEpoch = event.tool_epoch;
    if (
      status === "speaking" &&
      Number.isInteger(turnId) &&
      Number.isInteger(generationId) &&
      Number.isInteger(toolEpoch)
    ) {
      this._assistantStateFence = { turnId, generationId, toolEpoch };
      const pending = this._pendingAssistantExpression;
      if (
        pending &&
        pending.turnId === turnId &&
        pending.generationId === generationId &&
        pending.toolEpoch === toolEpoch
      ) {
        this._pendingAssistantExpression = null;
        this._activateAssistantExpression(pending);
      }
    } else if (status !== "speaking") {
      this._assistantStateFence = null;
      this._pendingAssistantExpression = null;
      this._clearAssistantExpression(generationId);
    }
    this.setData({
      status,
      statusLabel: stateLabel(status, this.data.inputMode),
      textTurnPending:
        status === "listening" ? this.data.textTurnPending : false,
    });
  },

  onTextDraftInput(event) {
    this.setData({ textDraft: event?.detail?.value || "" });
  },

  sendText() {
    const text = String(this.data.textDraft || "").trim();
    if (
      !this._media ||
      !this.data.active ||
      this.data.inputMode !== "text" ||
      this.data.status !== "listening" ||
      this.data.textTurnPending ||
      !text
    ) {
      return;
    }
    try {
      if (!this._media.sendText(text)) {
        throw new Error("文字连接尚未就绪，请稍后再试。");
      }
      this._appendTranscript({
        speaker: "user",
        text,
        final: false,
        source: "display",
      });
      this.setData({
        textDraft: "",
        textTurnPending: true,
        error: "",
      });
    } catch (error) {
      this.setData({ error: error?.message || "文字消息发送失败。" });
    }
  },

  async stopVoice() {
    this._ending = true;
    try {
      if (this._session?.session_id) await api.stopResponse(this._session.session_id);
    } catch {
      // The local media path must still close even if a best-effort stop signal fails.
    }
    await this._endMediaLocally();
    this._session = null;
    this._resetExpression();
    this.setData({
      active: false,
      sessionId: "",
      inputMode: "voice",
      textDraft: "",
      textTurnPending: false,
      micEnabled: true,
      status: "closed",
      statusLabel: stateLabel("closed"),
    });
  },

  async retryVoice() {
    if (!this._session || this._recovering) return;
    this._recovering = true;
    this.setData({
      error: "",
      status: "reconnecting",
      statusLabel: stateLabel("reconnecting"),
      textTurnPending: false,
    });
    try {
      const gatewayTicket = await api.refreshMiniProgramGatewayTicket(this._session.session_id);
      await this._endMediaLocally();
      const recovered = {
        ...this._session,
        media_gateway: gatewayTicket,
      };
      await this._connectMedia(recovered, this.data.inputMode);
      await api.notifyRtcRecovered(recovered.session_id);
      this._session = recovered;
      this.setData({
        active: true,
        micEnabled: this.data.inputMode === "voice",
      });
    } catch (error) {
      this.setData({
        active: false,
        status: "closed",
        statusLabel: stateLabel("closed", this.data.inputMode),
        textTurnPending: false,
        error: error?.message || "恢复对话连接失败。",
      });
    } finally {
      this._recovering = false;
    }
  },

  _onMediaClosed() {
    if (this._ending) return;
    this._resetExpression();
    this.setData({
      active: false,
      status: "closed",
      statusLabel: stateLabel("closed", this.data.inputMode),
      textTurnPending: false,
      error: "连接已断开，可轻触“恢复对话”。",
    });
  },

  async _onMediaInterrupted(message) {
    if (this._ending || this._handlingMediaInterruption) return;
    this._handlingMediaInterruption = true;
    try {
      await this._endMediaLocally();
      this._resetExpression();
      this.setData({
        active: false,
        micEnabled: true,
        status: "closed",
        statusLabel: stateLabel("closed", this.data.inputMode),
        textTurnPending: false,
        error: message || "录音被系统中断，请轻触恢复语音。",
      });
    } finally {
      this._handlingMediaInterruption = false;
    }
  },

  async _endMediaLocally() {
    const media = this._media;
    this._media = null;
    if (media) await media.close();
  },
});
