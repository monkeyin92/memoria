const api = require("../../utils/api");
const { companionById, defaultCompanionId } = require("../../utils/companions");
const { MiniProgramMediaSession } = require("../../utils/media-gateway");
const { authoritativeTranscript } = require("../../utils/transcript-events");

const CONNECTION_REFUSED_RETRY_DELAY_MS = 400;

const defaultProfile = {
  display_name: "新朋友",
  bio: "慢慢说，我会认真听。",
  companion_id: defaultCompanionId,
  voice_reply: true,
};

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

function stateLabel(state) {
  return {
    idle: "轻触开始陪伴",
    connecting: "正在连接",
    listening: "我在听",
    thinking: "正在想",
    speaking: "正在回应",
    reconnecting: "正在恢复连接",
    closed: "连接已结束",
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
    micEnabled: true,
    sessionId: "",
    connecting: false,
    active: false,
    expression: "neutral",
    ...mascotAssetsFor(companionById(defaultCompanionId)),
  },

  onShow() {
    if (!api.currentAccessToken()) {
      wx.navigateTo({ url: "/pages/auth/index" });
      return;
    }
    this.loadProfile();
  },

  onUnload() {
    this._resetExpression();
    this._endMediaLocally();
  },

  onPullDownRefresh() {
    this.loadProfile().finally(() => wx.stopPullDownRefresh());
  },

  async loadProfile() {
    const identity = api.currentIdentity();
    if (!identity) return;
    try {
      const profile = { ...defaultProfile, ...(await api.getProfile(identity.user_id)) };
      const companion = companionById(profile.companion_id);
      this.setData({
        profile,
        companion,
        ...mascotAssetsFor(companion),
      });
    } catch (error) {
      this.setData({ error: error?.message || "个人资料暂时无法加载。" });
    }
  },

  startVoice() {
    if (this._startVoicePromise) return this._startVoicePromise;
    if (this.data.connecting || this.data.active) return Promise.resolve();
    let attempt;
    attempt = this._startVoiceOnce().finally(() => {
      if (this._startVoicePromise === attempt) this._startVoicePromise = null;
    });
    this._startVoicePromise = attempt;
    return attempt;
  },

  async _startVoiceOnce() {
    const identity = api.currentIdentity();
    if (!identity) {
      wx.navigateTo({ url: "/pages/auth/index" });
      return;
    }
    this.setData({
      connecting: true,
      status: "connecting",
      statusLabel: stateLabel("connecting"),
      error: "",
      transcript: [],
      sessionId: "",
    });
    this._ending = false;
    this._session = null;
    this._resetExpression();
    try {
      await authorizationForRecord();
      const session = await api.createMiniProgramSession({ userId: identity.user_id });
      this._session = session;
      this.setData({ sessionId: session.session_id });
      this._session = await this._connectInitialMedia(session);
      this.setData({
        active: true,
      });
    } catch (error) {
      await this._endMediaLocally();
      const canRetry = Boolean(this._session?.session_id);
      this.setData({
        active: false,
        status: canRetry ? "closed" : "idle",
        statusLabel: stateLabel(canRetry ? "closed" : "idle"),
        error: error?.message || "语音会话未能开始。",
      });
    } finally {
      this.setData({ connecting: false });
    }
  },

  async _connectInitialMedia(session) {
    try {
      await this._connectMedia(session);
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
      await this._connectMedia(recovered);
      return recovered;
    }
  },

  async _connectMedia(session) {
    let media;
    media = new MiniProgramMediaSession(session, {
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
    if (event?.type !== "ui_event") return;
    const payload = event.event || {};
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
      if (mapped) this._setStatus(mapped);
      return;
    }
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
      this.setData({ expression: "neutral" });
    }, ttl);
  },

  _resetExpression() {
    if (this._expressionTimer) {
      clearTimeout(this._expressionTimer);
      this._expressionTimer = null;
    }
    this._pendingExpression = null;
    this._lastUserTurnId = 0;
    this.setData({ expression: "neutral" });
  },

  _appendTranscript(item) {
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

  _setStatus(status) {
    this.setData({ status, statusLabel: stateLabel(status) });
  },

  async toggleMic() {
    if (!this._media || !this.data.active) return;
    const next = !this.data.micEnabled;
    try {
      await this._media.setMicrophoneEnabled(next);
      this.setData({ micEnabled: next });
    } catch (error) {
      this.setData({ error: error?.message || "麦克风状态切换失败。" });
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
      micEnabled: true,
      status: "closed",
      statusLabel: stateLabel("closed"),
    });
  },

  async retryVoice() {
    if (!this._session || this._recovering) return;
    this._recovering = true;
    this.setData({ error: "", status: "reconnecting", statusLabel: stateLabel("reconnecting") });
    try {
      const gatewayTicket = await api.refreshMiniProgramGatewayTicket(this._session.session_id);
      await this._endMediaLocally();
      const recovered = {
        ...this._session,
        media_gateway: gatewayTicket,
      };
      await this._connectMedia(recovered);
      await api.notifyRtcRecovered(recovered.session_id);
      this._session = recovered;
      this.setData({ active: true, micEnabled: true });
    } catch (error) {
      this.setData({
        active: false,
        status: "closed",
        statusLabel: stateLabel("closed"),
        error: error?.message || "恢复语音连接失败。",
      });
    } finally {
      this._recovering = false;
    }
  },

  _onMediaClosed() {
    if (this._ending) return;
    this.setData({
      active: false,
      status: "closed",
      statusLabel: stateLabel("closed"),
      error: "连接已断开，可轻触“恢复语音”。",
    });
  },

  async _onMediaInterrupted(message) {
    if (this._ending || this._handlingMediaInterruption) return;
    this._handlingMediaInterruption = true;
    try {
      await this._endMediaLocally();
      this.setData({
        active: false,
        micEnabled: true,
        status: "closed",
        statusLabel: stateLabel("closed"),
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
