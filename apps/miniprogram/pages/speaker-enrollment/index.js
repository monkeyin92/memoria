const api = require("../../utils/api");
const { requireLogin } = require("../../utils/auth-gate");

const RECORDING_OPTIONS = {
  duration: 8_000,
  sampleRate: 16_000,
  numberOfChannels: 1,
  encodeBitRate: 24_000,
  format: "PCM",
  frameSize: 1,
};

const RECORDING_PROMPTS = [
  {
    label: "自然说话",
    hint: "用你平时最常用的音色",
    prompt: "今天过得怎么样，我想和你聊聊天。",
    scene: "owner-natural",
  },
  {
    label: "轻声说话",
    hint: "比平时稍轻一些，不需要耳语",
    prompt: "现在很安静，我想慢慢说给你听。",
    scene: "owner-soft",
  },
  {
    label: "带笑说话",
    hint: "带一点开心的语气",
    prompt: "见到你真好，今天也有值得开心的事。",
    scene: "owner-bright",
  },
  {
    label: "稳重说话",
    hint: "稍微放慢速度，清楚地说完",
    prompt: "请记住，这是我本人在和你说话。",
    scene: "owner-steady",
  },
];

function freshRecordings() {
  return RECORDING_PROMPTS.map((item) => ({ ...item, ready: false, sample: null }));
}

function mergeFrames(frames) {
  const length = frames.reduce((sum, frame) => sum + frame.byteLength, 0);
  const merged = new Uint8Array(length);
  let offset = 0;
  frames.forEach((frame) => {
    merged.set(frame, offset);
    offset += frame.byteLength;
  });
  return merged;
}

Page({
  data: {
    recordings: freshRecordings(),
    completed: 0,
    consent: false,
    recordingIndex: -1,
    elapsed: "0.0",
    busy: false,
    error: "",
  },

  onLoad() {
    this._recorder = wx.getRecorderManager();
    this._frames = [];
    this._recordingStartedAt = 0;
    this._unloaded = false;
    this._onRecorderStartBound = this._onRecorderStart.bind(this);
    this._onRecorderStopBound = this._onRecorderStop.bind(this);
    this._onFrameRecordedBound = this._onFrameRecorded.bind(this);
    this._onRecorderErrorBound = this._onRecorderError.bind(this);
    this._onInterruptionBeginBound = this._onInterruptionBegin.bind(this);
    this._recorder.onStart(this._onRecorderStartBound);
    this._recorder.onStop(this._onRecorderStopBound);
    this._recorder.onFrameRecorded(this._onFrameRecordedBound);
    this._recorder.onError(this._onRecorderErrorBound);
    this._recorder.onInterruptionBegin(this._onInterruptionBeginBound);

    const app = getApp();
    if (typeof app?.subscribeAuthCleared === "function") {
      this._unsubscribeAuthCleared = app.subscribeAuthCleared(() => {
        this._discardCurrentRecording("登录状态已失效，请重新登录。");
        wx.navigateBack();
      });
    }
  },

  async onShow() {
    await requireLogin({ reason: "edit_profile" });
  },

  onUnload() {
    this._unloaded = true;
    this._clearElapsedTimer();
    if (this.data.recordingIndex >= 0) {
      this._discardRecording = true;
      try {
        this._recorder.stop();
      } catch {
        // 页面离开时仅做资源清理。
      }
    }
    this._recorder?.offStart?.(this._onRecorderStartBound);
    this._recorder?.offStop?.(this._onRecorderStopBound);
    this._recorder?.offFrameRecorded?.(this._onFrameRecordedBound);
    this._recorder?.offError?.(this._onRecorderErrorBound);
    this._recorder?.offInterruptionBegin?.(this._onInterruptionBeginBound);
    this._unsubscribeAuthCleared?.();
    this._unsubscribeAuthCleared = null;
  },

  onConsentChange(event) {
    const consent = (event.detail.value || []).includes("accepted");
    this.setData({ consent, error: consent ? "" : this.data.error });
  },

  startRecording(event) {
    const index = Number(event.currentTarget.dataset.index);
    const recording = this.data.recordings[index];
    if (!this.data.consent) {
      this.setData({ error: "请先阅读并同意主人声纹用途说明。" });
      return;
    }
    if (!recording || this.data.busy || this.data.recordingIndex >= 0) return;
    if (!recording.ready && index > this.data.completed) {
      this.setData({ error: "请按顺序完成前一段录音。" });
      return;
    }

    this._frames = [];
    this._discardRecording = false;
    this._recordingStartedAt = Date.now();
    this.setData({ recordingIndex: index, elapsed: "0.0", error: "" });
    try {
      this._recorder.start(RECORDING_OPTIONS);
    } catch (error) {
      this.setData({
        recordingIndex: -1,
        error: error?.message || "录音无法启动，请检查麦克风权限。",
      });
    }
  },

  stopRecording() {
    if (this.data.recordingIndex < 0) return;
    try {
      this._recorder.stop();
    } catch (error) {
      this._onRecorderError(error);
    }
  },

  _onRecorderStart() {
    this._clearElapsedTimer();
    this._elapsedTimer = setInterval(() => {
      if (this._unloaded || this.data.recordingIndex < 0) return;
      const seconds = Math.min(8, (Date.now() - this._recordingStartedAt) / 1000);
      this.setData({ elapsed: seconds.toFixed(1) });
    }, 100);
  },

  _onFrameRecorded(event) {
    if (this._unloaded || this.data.recordingIndex < 0 || !event?.frameBuffer) return;
    const frame = new Uint8Array(event.frameBuffer);
    if (frame.byteLength) this._frames.push(frame);
  },

  _onRecorderStop() {
    const index = this.data.recordingIndex;
    const duration = Date.now() - this._recordingStartedAt;
    const discard = this._discardRecording;
    this._clearElapsedTimer();
    if (this._unloaded) return;
    this.setData({ recordingIndex: -1, elapsed: "0.0" });
    if (discard || index < 0) return;
    if (duration < 1_500 || !this._frames.length) {
      this.setData({ error: "这一段太短，请连续说 3 到 8 秒后再停止。" });
      return;
    }

    const recordings = this.data.recordings.slice();
    const recording = recordings[index];
    const pcm = mergeFrames(this._frames);
    recordings[index] = {
      ...recording,
      ready: true,
      sample: {
        audio_base64: wx.arrayBufferToBase64(pcm.buffer),
        sample_rate: RECORDING_OPTIONS.sampleRate,
        device: "wechat-miniprogram-recorder",
        scene: recording.scene,
      },
    };
    this.setData({
      recordings,
      completed: recordings.filter((item) => item.ready).length,
      error: "",
    });
  },

  _onRecorderError(error) {
    this._clearElapsedTimer();
    this.setData({
      recordingIndex: -1,
      elapsed: "0.0",
      error: error?.errMsg || error?.message || "录音失败，请检查麦克风权限后重试。",
    });
  },

  _onInterruptionBegin() {
    this._discardCurrentRecording("录音被系统中断，请重新录制这一段。");
  },

  _discardCurrentRecording(message) {
    if (this.data.recordingIndex >= 0) {
      this._discardRecording = true;
      try {
        this._recorder.stop();
      } catch {
        this._clearElapsedTimer();
        this.setData({ recordingIndex: -1, elapsed: "0.0" });
      }
    }
    if (!this._unloaded) this.setData({ error: message });
  },

  _clearElapsedTimer() {
    if (this._elapsedTimer) clearInterval(this._elapsedTimer);
    this._elapsedTimer = null;
  },

  async submit() {
    const { recordings, consent, busy, recordingIndex } = this.data;
    if (busy || recordingIndex >= 0) return;
    if (!consent) {
      this.setData({ error: "请先同意主人声纹用途说明。" });
      return;
    }
    if (!recordings.every((item) => item.ready && item.sample)) {
      this.setData({ error: "请先完成四种说话状态的录音。" });
      return;
    }

    this.setData({ busy: true, error: "" });
    try {
      await api.enrollSpeakerProfiles(recordings.map((item) => item.sample));
      if (this._unloaded) return;
      wx.showToast({ title: "主人声纹已提交", icon: "success" });
      wx.navigateBack();
    } catch (error) {
      if (!this._unloaded) {
        this.setData({ error: error?.message || "提交失败，请稍后重试。" });
      }
    } finally {
      if (!this._unloaded) this.setData({ busy: false });
    }
  },
});
