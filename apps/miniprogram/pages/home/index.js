const api = require("../../utils/api");
const { companionById, defaultCompanionId } = require("../../utils/companions");
const { MiniProgramMediaSession } = require("../../utils/media-gateway");

const defaultProfile = {
  display_name: "新朋友",
  bio: "慢慢说，我会认真听。",
  companion_id: defaultCompanionId,
  voice_reply: true,
};

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
  },

  onShow() {
    if (!api.currentAccessToken()) {
      wx.navigateTo({ url: "/pages/auth/index" });
      return;
    }
    this.loadProfile();
  },

  onUnload() {
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
      this.setData({
        profile,
        companion: companionById(profile.companion_id),
      });
    } catch (error) {
      this.setData({ error: error?.message || "个人资料暂时无法加载。" });
    }
  },

  async startVoice() {
    if (this.data.connecting || this.data.active) return;
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
    try {
      await authorizationForRecord();
      const session = await api.createMiniProgramSession({ userId: identity.user_id });
      this._session = session;
      this.setData({ sessionId: session.session_id });
      await this._connectMedia(session);
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

  async _connectMedia(session) {
    const media = new MiniProgramMediaSession(session, {
      onEvent: (event) => this._onGatewayEvent(event),
      onClose: () => this._onMediaClosed(),
      onError: (message) => this.setData({ error: message }),
      onInterrupted: (message) => this._onMediaInterrupted(message),
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
    if (event?.type === "transcription") {
      const segments = Array.isArray(event.segments) ? event.segments : [];
      const text = segments.map((segment) => segment.text || "").join("");
      if (text) {
        this._appendTranscript({
          speaker: "assistant",
          text,
          final: segments.every((segment) => segment.final === true),
          turnId: event.turn_id,
          generationId: event.generation_id,
          source: "transcription",
        });
      }
      return;
    }
    if (event?.type !== "ui_event") return;
    const payload = event.event || {};
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
    if (payload.type !== "transcript_delta") return;
    if (
      (payload.speaker === "user" || payload.speaker === "assistant") &&
      typeof payload.text === "string" &&
      payload.text
    ) {
      this._appendTranscript({
        speaker: payload.speaker,
        text: payload.text,
        final: payload.final !== false,
        turnId: payload.turn_id,
        generationId: payload.generation_id,
        source: "authoritative",
      });
    }
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
    const transcript = [...this.data.transcript];
    const index = key ? transcript.findIndex((entry) => entry.key === key) : -1;
    if (index >= 0) {
      if (transcript[index].source === "authoritative" && next.source !== "authoritative") {
        return;
      }
      transcript[index] = next;
    } else {
      transcript.push(next);
    }
    this.setData({ transcript: transcript.slice(-24) });
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
    if (this._ending || this._interrupting) return;
    this._interrupting = true;
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
      this._interrupting = false;
    }
  },

  async _endMediaLocally() {
    const media = this._media;
    this._media = null;
    if (media) await media.close();
  },
});
