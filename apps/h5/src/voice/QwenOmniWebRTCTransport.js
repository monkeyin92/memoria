import { createSpeakerGatedStream } from "./speakerGate.js";
import { extractInboundAudioStats } from "./webrtcStats.js";

const ICE_GATHERING_TIMEOUT_MS = 10_000;
const RTC_STATS_INTERVAL_MS = 5_000;
const OMNI_JITTER_BUFFER_TARGET_MS = 120;
const WELCOME_QUIET_WINDOW_MS = 400;
const POST_RESPONSE_FEEDBACK_GUARD_MS = 500;
const FEEDBACK_RESPONSE_TIMEOUT_MS = 2_000;
const WELCOME_INSTRUCTIONS =
  "请主动用一句自然、温暖的中文向用户打招呼并邀请用户直接开口，不要解释规则，也不要用固定填充词起音。";

const DEFAULT_INSTRUCTIONS = [
  "你是 Memoria，一个温暖、自然的中文语音陪伴助手。",
  "默认只说一到两句、先回应核心；只有用户明确要求详细时再展开。",
  "回应要真诚、有一点自己的看法，不要把每句话都变成追问。",
  "请结合用户声音里的语气和情绪，调整你的语气、节奏和情感。",
  "只有多步骤安排、需要查看信息或确实要先推理时，才用一句短而自然的思考式衔接，例如“嗯，好的，我先想一下”“可以，让我先理一理”“好，我先看看怎么安排”。这些只是语气示例，不要照抄固定模板，不要连续两轮使用同一个开场。",
  "直接问题、简单确认和情绪倾诉要直接回应；不要为了显得自然而每轮都加“嗯”“好的”“让我想想”。",
  "当用户说“可以、好、行”是在接受上一轮的提议时，直接执行或进入下一步，不要重复刚说过的安排，也不要重新问同一个问题。",
  "对比示例：用户说“帮我安排一个十五分钟的口语训练”时，可以先说“嗯，好，我先理一下”，短停顿后继续；用户问“今天星期几”时直接回答，不加思考开场。",
  "声音要像边组织边说：开场可以略慢，词组间有轻微停顿，重点词可以略微拉长，整段不要匀速朗读；不要用假结巴或重复音节表演思考。",
  "在自然开场中，“嗯”或“好”可以略微延长，后面留一个很短的呼吸式停顿，正文恢复正常速度；不要把整段都说慢。",
  "听见用户自然笑出声，且话题轻松时，可以先短促、真诚地轻笑一次再回答，但不要机械模仿每次笑声。能自然发出笑声时，不要把“哈哈”逐字念出来；如果不能自然发笑，就直接温暖回应。用户难过、生气、害怕、求助或涉及严肃风险时绝对不要笑，也不要咳嗽。",
  "同一用户问题只生成一轮完整回答，不要在说完后立刻再开第二句新开场。",
  "用户只是“嗯、对、好的”这类附和时继续当前话题，不要误当成新指令。",
].join("");

function waitForIceGathering(pc) {
  if (pc.iceGatheringState === "complete") return Promise.resolve();
  return new Promise((resolve, reject) => {
    const timeout = window.setTimeout(() => {
      pc.removeEventListener("icegatheringstatechange", onStateChange);
      reject(new Error("WebRTC ICE 收集超时"));
    }, ICE_GATHERING_TIMEOUT_MS);
    const onStateChange = () => {
      if (pc.iceGatheringState !== "complete") return;
      window.clearTimeout(timeout);
      pc.removeEventListener("icegatheringstatechange", onStateChange);
      resolve();
    };
    pc.addEventListener("icegatheringstatechange", onStateChange);
  });
}

function sessionUpdate(config = {}) {
  const configuredTurnDetection =
    typeof config.turn_detection === "object" && config.turn_detection
      ? config.turn_detection
      : {};
  const turnDetectionType =
    typeof config.turn_detection === "string"
      ? config.turn_detection
      : configuredTurnDetection.type;
  return {
    event_id: `event_${globalThis.crypto?.randomUUID?.() || Date.now()}`,
    type: "session.update",
    session: {
      modalities: ["text", "audio"],
      voice: config.voice || "Tina",
      input_audio_format: "pcm",
      output_audio_format: "pcm",
      input_audio_transcription: {
        model: "qwen3-asr-flash-realtime",
      },
      instructions: config.instructions || DEFAULT_INSTRUCTIONS,
      turn_detection: {
        type: turnDetectionType || "semantic_vad",
        threshold: configuredTurnDetection.threshold ?? 0.5,
        prefix_padding_ms: configuredTurnDetection.prefix_padding_ms ?? 500,
        silence_duration_ms:
          configuredTurnDetection.silence_duration_ms ?? 800,
      },
    },
  };
}

export class QwenOmniWebRTCTransport {
  constructor({
    exchangeSdp,
    onState = () => undefined,
    onTranscript = () => undefined,
    onRemoteStream = () => undefined,
    onDiagnostic = () => undefined,
    onError = () => undefined,
    onDisconnected = () => undefined,
    speakerVerifyEnabled = true,
    onSpeakerProgress = () => undefined,
    onSpeakerEnrolled = () => undefined,
    onSpeakerReject = () => undefined,
  }) {
    this.exchangeSdp = exchangeSdp;
    this.onState = onState;
    this.onTranscript = onTranscript;
    this.onRemoteStream = onRemoteStream;
    this.onDiagnostic = onDiagnostic;
    this.onError = onError;
    this.onDisconnected = onDisconnected;
    this.speakerVerifyEnabled = speakerVerifyEnabled;
    this.onSpeakerProgress = onSpeakerProgress;
    this.onSpeakerEnrolled = onSpeakerEnrolled;
    this.onSpeakerReject = onSpeakerReject;
    this.pc = null;
    this.stream = null;
    this.rawStream = null;
    this.speakerGate = null;
    this.commandChannel = null;
    this.clientChannel = null;
    this.preparePromise = null;
    this.session = null;
    this.closed = false;
    this.sessionUpdated = false;
    this.micEnabled = true;
    this.turnId = 0;
    this.generationId = 0;
    this.assistantText = "";
    this.assistantTextSource = "";
    this.responseActive = false;
    this.activeResponseId = "";
    this.activeItemId = "";
    this.userSpeaking = false;
    this.activeUserItemId = "";
    this.userTranscriptIds = new Map();
    this.remoteTrackKeys = new Set();
    this.welcomeRequested = false;
    this.welcomeSuppressed = false;
    this.welcomeTimer = null;
    this.responsePending = false;
    this.responseSuperseded = false;
    this.cancelledResponseIds = new Set();
    this.statsTimer = null;
    this.feedbackGuardTimer = null;
    this.feedbackResponseTimer = null;
    this.feedbackGuardActive = false;
    this.feedbackResponsePending = false;
    this.suppressedUserItemIds = new Set();
  }

  prepare() {
    if (this.preparePromise) return this.preparePromise;
    if (!globalThis.RTCPeerConnection) {
      throw new Error("当前浏览器不支持 WebRTC");
    }
    if (!navigator.mediaDevices?.getUserMedia) {
      throw new Error("当前浏览器无法访问麦克风");
    }

    const pc = new RTCPeerConnection({ iceServers: [] });
    this.pc = pc;
    pc.ontrack = (event) => {
      if (this.closed || event.track?.kind !== "audio") return;
      const stream = event.streams?.[0];
      if (!stream) return;
      const trackKey = event.track?.id || stream.id;
      if (trackKey && this.remoteTrackKeys.has(trackKey)) return;
      if (trackKey) this.remoteTrackKeys.add(trackKey);
      this.#configureRemoteReceiver(event.receiver);
      this.onDiagnostic("track_subscribed");
      this.onRemoteStream(stream);
    };
    pc.ondatachannel = ({ channel }) => this.#bindChannel(channel);
    pc.onconnectionstatechange = () => {
      if (this.closed) return;
      if (pc.connectionState === "connected") {
        this.onDiagnostic("room_connected");
        this.#startStatsSampling();
        return;
      }
      if (["failed", "disconnected", "closed"].includes(pc.connectionState)) {
        this.onDisconnected();
      }
    };

    const mediaPromise = navigator.mediaDevices.getUserMedia({
      audio: {
        autoGainControl: true,
        echoCancellation: true,
        noiseSuppression: true,
        channelCount: 1,
      },
    });
    this.preparePromise = (async () => {
      const rawStream = await mediaPromise;
      if (this.closed) {
        rawStream.getTracks().forEach((track) => track.stop());
        throw new Error("语音会话已结束");
      }
      this.rawStream = rawStream;
      this.speakerGate = createSpeakerGatedStream(rawStream, {
        enabled: this.speakerVerifyEnabled,
        onProgress: (progress) => {
          this.onSpeakerProgress(progress);
          if (progress.state === "pending") {
            this.onState("speaker_enroll");
          }
        },
        onEnrolled: (result) => {
          this.onSpeakerEnrolled(result);
          this.onDiagnostic("speaker_enrolled", "ok", result);
          if (result.reason === "enrolled") {
            this.onState("ready");
          } else {
            this.onState("ready");
          }
        },
        onReject: (detail) => {
          this.onSpeakerReject(detail);
          this.onDiagnostic("speaker_rejected", "ignored", detail);
        },
      });
      const stream = this.speakerGate.stream;
      this.stream = stream;
      for (const track of stream.getAudioTracks()) {
        track.enabled = false;
        pc.addTrack(track, stream);
      }
      this.clientChannel = pc.createDataChannel("oai-events");
      this.#bindChannel(this.clientChannel);
    })();
    return this.preparePromise;
  }

  async connect(session) {
    this.session = session;
    await this.prepare();
    if (this.closed || !this.pc) return;
    const offer = await this.pc.createOffer();
    await this.pc.setLocalDescription(offer);
    await waitForIceGathering(this.pc);
    if (this.closed || !this.pc.localDescription?.sdp) return;
    const answerSdp = await this.exchangeSdp(
      session.session_id,
      this.pc.localDescription.sdp,
    );
    if (this.closed) return;
    await this.pc.setRemoteDescription({ type: "answer", sdp: answerSdp });
  }

  setMicrophoneEnabled(enabled) {
    this.micEnabled = enabled;
    this.#syncMicrophoneTracks();
  }

  #syncMicrophoneTracks() {
    const active =
      this.sessionUpdated && this.micEnabled && !this.feedbackGuardActive;
    this.stream?.getAudioTracks().forEach((track) => {
      track.enabled = active;
    });
  }

  cancelResponse() {
    const channel = this.commandChannel;
    if (!channel || channel.readyState !== "open") {
      throw new Error("Qwen Omni 控制通道尚未就绪");
    }
    if (this.responseActive) {
      this.#cancelActiveResponse();
    } else if (this.responsePending) {
      this.responseSuperseded = true;
    }
    this.onState("interrupted");
  }

  close() {
    if (this.closed) return;
    this.closed = true;
    this.sessionUpdated = false;
    this.#stopStatsSampling();
    this.#clearFeedbackTimers();
    this.#clearWelcomeTimer();
    this.commandChannel?.close();
    if (this.clientChannel !== this.commandChannel) this.clientChannel?.close();
    this.speakerGate?.close?.();
    this.speakerGate = null;
    // Stop the original mic tracks; the gated destination dies with AudioContext.
    const micTracks = this.rawStream?.getTracks?.() || this.stream?.getTracks?.() || [];
    micTracks.forEach((track) => track.stop());
    if (this.pc) {
      this.pc.ontrack = null;
      this.pc.ondatachannel = null;
      this.pc.onconnectionstatechange = null;
      this.pc.close();
    }
    this.pc = null;
    this.stream = null;
    this.rawStream = null;
    this.commandChannel = null;
    this.clientChannel = null;
    this.userSpeaking = false;
    this.activeUserItemId = "";
    this.userTranscriptIds.clear();
    this.suppressedUserItemIds.clear();
    this.cancelledResponseIds.clear();
    this.remoteTrackKeys.clear();
    this.#clearActiveResponse();
  }

  #bindChannel(channel) {
    if (this.closed || !channel) return;
    if (channel.label === "txt") this.commandChannel = channel;
    channel.onmessage = ({ data }) => this.#handleMessage(data, channel);
  }

  #handleMessage(payload, channel) {
    if (this.closed) return;
    let event;
    try {
      event = JSON.parse(payload);
    } catch {
      return;
    }
    if (!event || typeof event.type !== "string") return;

    if (event.type === "session.created") {
      this.commandChannel = channel;
      this.onDiagnostic("omni_session_created");
      // P0-3: surface applied silence/threshold for Flash vs Plus A/B greps.
      const cfg = this.session?.config || {};
      const td =
        typeof cfg.turn_detection === "object" && cfg.turn_detection
          ? cfg.turn_detection
          : {};
      this.onDiagnostic("omni_ab_profile", "ok", {
        ab_profile: cfg.ab_profile || null,
        model: cfg.model || null,
        threshold: td.threshold ?? null,
        silence_duration_ms: td.silence_duration_ms ?? null,
        prefix_padding_ms: td.prefix_padding_ms ?? null,
      });
      if (channel.readyState === "open") {
        channel.send(JSON.stringify(sessionUpdate(this.session?.config)));
      }
      return;
    }
    if (event.type === "session.updated") {
      this.sessionUpdated = true;
      this.setMicrophoneEnabled(this.micEnabled);
      this.onDiagnostic("agent_ready");
      if (this.speakerGate?.state?.() === "pending") {
        this.onState("speaker_enroll");
        this.#waitSpeakerEnrollThenWelcome(channel);
      } else {
        this.onState("ready");
        this.#armWelcome(channel);
      }
      return;
    }
    if (event.type === "input_audio_buffer.speech_started") {
      const itemId = this.#itemId(event);
      if (
        !itemId ||
        this.userTranscriptIds.has(itemId) ||
        this.suppressedUserItemIds.has(itemId)
      ) {
        return;
      }
      if (this.feedbackGuardActive) {
        this.suppressedUserItemIds.add(itemId);
        this.#expectFeedbackResponse();
        this.onDiagnostic("omni_feedback_suppressed");
        return;
      }
      this.#clearExpectedFeedbackResponse();
      this.#suppressWelcome();
      this.turnId += 1;
      this.userTranscriptIds.set(itemId, {
        turn_id: this.turnId,
        generation_id: this.generationId,
      });
      this.activeUserItemId = itemId;
      this.userSpeaking = true;
      if (this.responseActive) {
        this.#cancelActiveResponse();
      } else if (this.responsePending) {
        this.responseSuperseded = true;
      }
      this.onDiagnostic("omni_speech_started");
      this.onState("listening");
      return;
    }
    if (event.type === "input_audio_buffer.speech_stopped") {
      const itemId = this.#itemId(event);
      if (this.suppressedUserItemIds.delete(itemId)) return;
      if (
        itemId !== this.activeUserItemId ||
        !this.userTranscriptIds.has(itemId)
      ) {
        return;
      }
      this.userSpeaking = false;
      this.onDiagnostic("omni_speech_stopped");
      this.onState("thinking");
      return;
    }
    if (
      event.type === "conversation.item.input_audio_transcription.delta"
    ) {
      const ids = this.userTranscriptIds.get(this.#itemId(event));
      if (!ids) return;
      const text = `${event.text || ""}${event.stash || ""}` || event.delta;
      if (text) this.#emitTranscript("user", text, false, false, ids);
      return;
    }
    if (
      event.type === "conversation.item.input_audio_transcription.completed"
    ) {
      const itemId = this.#itemId(event);
      const ids = this.userTranscriptIds.get(itemId);
      if (!ids) return;
      if (itemId === this.activeUserItemId) {
        this.userSpeaking = false;
        this.activeUserItemId = "";
      }
      const text = event.transcript || event.text;
      if (text) this.#emitTranscript("user", text, true, false, ids);
      this.userTranscriptIds.delete(itemId);
      return;
    }
    if (event.type === "response.created") {
      // During enrollment, ignore model replies triggered by registration speech.
      if (this.speakerGate?.state?.() === "pending") {
        this.#sendResponseCancel();
        return;
      }
      const responseId = this.#responseId(event);
      if (!responseId || this.cancelledResponseIds.has(responseId)) return;
      this.responsePending = false;
      if (this.feedbackResponsePending) {
        this.#clearExpectedFeedbackResponse();
        this.#cancelResponseId(responseId);
        this.onState("ready");
        return;
      }
      if (this.userSpeaking || this.responseSuperseded) {
        this.responseSuperseded = false;
        this.#cancelResponseId(responseId);
        return;
      }
      this.generationId += 1;
      this.responseActive = true;
      this.activeResponseId = responseId;
      this.activeItemId = "";
      this.assistantText = "";
      this.assistantTextSource = "";
      this.onDiagnostic("omni_response_created");
      void this.#sampleStats();
      this.onState("thinking");
      return;
    }
    if (event.type === "response.output_item.added") {
      if (!this.#matchesActiveResponse(event)) return;
      this.activeItemId = this.#itemId(event);
      return;
    }
    if (
      event.type === "response.text.delta" ||
      event.type === "response.audio_transcript.delta"
    ) {
      if (!this.#matchesActiveOutput(event)) return;
      const source = event.type.includes("audio_transcript") ? "audio" : "text";
      if (!this.assistantTextSource) this.assistantTextSource = source;
      if (this.assistantTextSource !== source || !event.delta) return;
      if (!this.assistantText) void this.#sampleStats();
      this.assistantText += event.delta;
      this.onState("speaking");
      this.#emitTranscript("assistant", this.assistantText, false, false);
      return;
    }
    if (
      event.type === "response.text.done" ||
      event.type === "response.audio_transcript.done"
    ) {
      if (!this.#matchesActiveOutput(event)) return;
      const source = event.type.includes("audio_transcript") ? "audio" : "text";
      if (this.assistantTextSource && this.assistantTextSource !== source) return;
      const text = event.text || event.transcript || this.assistantText;
      if (text) this.#emitTranscript("assistant", text, true, false);
      return;
    }
    if (event.type === "response.done") {
      const responseId = this.#responseId(event);
      if (this.cancelledResponseIds.delete(responseId)) {
        this.onDiagnostic("omni_cancelled_response_done");
        return;
      }
      if (!this.#matchesActiveResponse(event)) return;
      this.#clearActiveResponse();
      this.#startPostResponseFeedbackGuard();
      this.onDiagnostic("omni_response_done");
      void this.#sampleStats();
      this.onState("ready");
      return;
    }
    if (event.type === "error") {
      this.onError("Qwen3.5-Omni 服务暂时不可用，请稍后再试");
    }
  }

  #emitTranscript(speaker, text, final, heard, ids = undefined) {
    this.onTranscript({
      speaker,
      text,
      final,
      heard,
      turn_id: ids?.turn_id ?? this.turnId,
      generation_id: ids?.generation_id ?? this.generationId,
    });
  }

  #responseId(event) {
    const id = event.response_id || event.response?.id;
    return typeof id === "string" ? id : "";
  }

  #itemId(event) {
    const id = event.item_id || event.item?.id;
    return typeof id === "string" ? id : "";
  }

  #matchesActiveResponse(event) {
    if (!this.responseActive) return false;
    return this.#responseId(event) === this.activeResponseId;
  }

  #matchesActiveOutput(event) {
    if (!this.#matchesActiveResponse(event)) return false;
    const itemId = this.#itemId(event);
    if (!this.activeItemId) this.activeItemId = itemId;
    return itemId === this.activeItemId;
  }

  #clearActiveResponse() {
    this.responsePending = false;
    this.responseActive = false;
    this.activeResponseId = "";
    this.activeItemId = "";
  }

  #cancelActiveResponse() {
    const responseId = this.activeResponseId;
    if (!this.responseActive || !responseId) return;
    this.#cancelResponseId(responseId);
    this.#clearActiveResponse();
  }

  #cancelResponseId(responseId) {
    if (!responseId || this.cancelledResponseIds.has(responseId)) return;
    this.cancelledResponseIds.add(responseId);
    this.#sendResponseCancel();
  }

  #sendResponseCancel() {
    if (!this.commandChannel || this.commandChannel.readyState !== "open") return;
    this.commandChannel.send(JSON.stringify({ type: "response.cancel" }));
    this.onDiagnostic("omni_response_cancelled");
  }

  #waitSpeakerEnrollThenWelcome(channel) {
    const started = performance.now();
    const tick = () => {
      if (this.closed) return;
      const gateState = this.speakerGate?.state?.() || "open";
      if (gateState === "pending" && performance.now() - started < 16000) {
        this.welcomeTimer = window.setTimeout(tick, 200);
        return;
      }
      this.welcomeTimer = null;
      if (gateState === "pending") {
        this.speakerGate?.forceOpen?.();
        this.onSpeakerEnrolled({ reason: "enroll_timeout", speechMs: 0 });
      }
      this.onState("ready");
      this.#armWelcome(channel);
    };
    tick();
  }

  #armWelcome(channel) {
    if (
      this.welcomeRequested ||
      this.welcomeSuppressed ||
      this.welcomeTimer !== null ||
      channel.readyState !== "open"
    ) {
      return;
    }
    this.onDiagnostic("omni_welcome_armed");
    const enrolled = this.speakerGate?.state?.() === "enrolled";
    const welcomeInstructions = enrolled
      ? "请用一句自然中文确认声纹登记成功，并邀请用户直接开口说需求。"
      : WELCOME_INSTRUCTIONS;
    this.welcomeTimer = window.setTimeout(() => {
      this.welcomeTimer = null;
      if (
        this.closed ||
        this.userSpeaking ||
        this.welcomeSuppressed ||
        channel.readyState !== "open"
      ) {
        return;
      }
      this.welcomeRequested = true;
      this.responsePending = true;
      channel.send(
        JSON.stringify({
          type: "response.create",
          response: {
            modalities: ["text", "audio"],
            instructions: welcomeInstructions,
          },
        }),
      );
      this.onDiagnostic("omni_welcome_requested");
      void this.#sampleStats();
    }, WELCOME_QUIET_WINDOW_MS);
  }

  #suppressWelcome() {
    if (this.welcomeSuppressed || this.responseActive) return;
    if (this.welcomeTimer === null && !this.responsePending) return;
    this.welcomeSuppressed = true;
    this.#clearWelcomeTimer();
    this.onDiagnostic("omni_welcome_suppressed");
  }

  #clearWelcomeTimer() {
    if (this.welcomeTimer === null) return;
    window.clearTimeout(this.welcomeTimer);
    this.welcomeTimer = null;
  }

  #configureRemoteReceiver(receiver) {
    if (!receiver || !("jitterBufferTarget" in receiver)) return;
    try {
      receiver.jitterBufferTarget = OMNI_JITTER_BUFFER_TARGET_MS;
      if (receiver.jitterBufferTarget === OMNI_JITTER_BUFFER_TARGET_MS) {
        this.onDiagnostic("omni_playout_buffer_configured");
      }
    } catch {
      // Unsupported receiver tuning must never block the call.
    }
  }

  #startPostResponseFeedbackGuard() {
    if (this.feedbackGuardTimer !== null) {
      window.clearTimeout(this.feedbackGuardTimer);
    }
    this.feedbackGuardActive = true;
    this.#syncMicrophoneTracks();
    this.onDiagnostic("omni_feedback_guard_started");
    this.feedbackGuardTimer = window.setTimeout(() => {
      this.feedbackGuardTimer = null;
      this.feedbackGuardActive = false;
      this.#syncMicrophoneTracks();
    }, POST_RESPONSE_FEEDBACK_GUARD_MS);
  }

  #expectFeedbackResponse() {
    this.feedbackResponsePending = true;
    if (this.feedbackResponseTimer !== null) {
      window.clearTimeout(this.feedbackResponseTimer);
    }
    this.feedbackResponseTimer = window.setTimeout(() => {
      this.feedbackResponseTimer = null;
      this.feedbackResponsePending = false;
      this.suppressedUserItemIds.clear();
    }, FEEDBACK_RESPONSE_TIMEOUT_MS);
  }

  #clearExpectedFeedbackResponse() {
    this.feedbackResponsePending = false;
    if (this.feedbackResponseTimer !== null) {
      window.clearTimeout(this.feedbackResponseTimer);
      this.feedbackResponseTimer = null;
    }
  }

  #clearFeedbackTimers() {
    if (this.feedbackGuardTimer !== null) {
      window.clearTimeout(this.feedbackGuardTimer);
      this.feedbackGuardTimer = null;
    }
    this.feedbackGuardActive = false;
    this.#clearExpectedFeedbackResponse();
  }

  #startStatsSampling() {
    if (this.statsTimer || typeof this.pc?.getStats !== "function") return;
    void this.#sampleStats();
    this.statsTimer = window.setInterval(
      () => void this.#sampleStats(),
      RTC_STATS_INTERVAL_MS,
    );
  }

  #stopStatsSampling() {
    if (this.statsTimer === null) return;
    window.clearInterval(this.statsTimer);
    this.statsTimer = null;
  }

  async #sampleStats() {
    const pc = this.pc;
    if (this.closed || !pc || typeof pc.getStats !== "function") return;
    try {
      const metrics = extractInboundAudioStats(await pc.getStats());
      if (!this.closed && metrics) {
        this.onDiagnostic("webrtc_inbound_audio", "ok", metrics);
      }
    } catch {
      // Stats are diagnostic-only and must never disturb the conversation.
    }
  }
}
