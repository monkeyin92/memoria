import { createSpeakerGatedStream } from "../speakerGate.js";
import { VoiceTransport } from "../VoiceTransport.js";
import { extractInboundAudioStats } from "../webrtcStats.js";

const ICE_GATHERING_TIMEOUT_MS = 10_000;
const RTC_STATS_INTERVAL_MS = 5_000;
const OMNI_JITTER_BUFFER_TARGET_MS = 120;
const WELCOME_QUIET_WINDOW_MS = 400;
// P1-7: dynamic post-response feedback guard (replaces fixed 500ms).
const FEEDBACK_GUARD_MIN_MS = 150;
const FEEDBACK_GUARD_MAX_MS = 800;
const FEEDBACK_GUARD_DEFAULT_MS = 280;
const FEEDBACK_RESPONSE_TIMEOUT_MS = 2_000;

/** Compute mute window after assistant audio from recent RTP quality. */
export function computeFeedbackGuardMs(metrics = {}) {
  const concealment = Number(
    metrics.non_silent_concealment_ratio ?? metrics.concealment_ratio ?? 0,
  );
  const jitterMs = Number(metrics.average_jitter_buffer_delay_ms ?? 0);
  const packetsDiscarded = Number(metrics.packets_discarded ?? 0);
  const packetsReceived = Number(metrics.packets_received ?? 0);
  const discardRatio =
    packetsReceived > 0 ? packetsDiscarded / packetsReceived : 0;

  let ms = FEEDBACK_GUARD_DEFAULT_MS;
  // Dirty path (echo / concealment / jitter) → longer mic mute.
  if (concealment >= 0.02 || jitterMs >= 100 || discardRatio >= 0.15) {
    ms = 650;
  } else if (concealment >= 0.005 || jitterMs >= 50 || discardRatio >= 0.08) {
    ms = 420;
  } else if (concealment <= 0.001 && jitterMs > 0 && jitterMs < 35) {
    // Clean path → shorter so 0–300ms barge-in is not swallowed.
    ms = 180;
  }
  return Math.min(FEEDBACK_GUARD_MAX_MS, Math.max(FEEDBACK_GUARD_MIN_MS, ms));
}
const WELCOME_INSTRUCTIONS =
  "请主动用一句自然、温暖的中文向用户打招呼并邀请用户直接开口，不要解释规则，也不要用固定填充词起音。";

const ENROLL_PROMPT_INSTRUCTIONS =
  "用一句简短自然的中文请用户用正常音量连续说大约三四秒来登记声纹。" +
  "可以说：请说——我是主人，请记住我的声音。不要解释技术细节，不要开始闲聊。";

const POST_ENROLL_WELCOME_INSTRUCTIONS =
  "用一句自然中文确认已经记住用户的声音，并邀请用户直接说需求。不要重复登记流程。";

/**
 * Intent-style control classification (not exact-phrase equality).
 * residual length after stripping wait intent ≈ pure stop command.
 *
 * @returns {{ kind: "empty"|"chat"|"interrupt_only"|"interrupt_then_chat", ack: string|null }}
 */
/**
 * Prod ASR often mangles「等一下」into English-ish garbage on Omni Plus, e.g.
 * "Uh, need egg." Treat known mishears + English wait phrases as pure stop.
 */
export function isOmniWaitMishear(text) {
  const raw = String(text || "").trim();
  if (!raw) return false;
  if (/need\s*egg|needegg|and\s*egg|an\s*egg/i.test(raw)) return true;
  if (/^(uh+|um+|er+|ah+)?[,.\s]*(wait|hold(\s*on)?|one\s*sec(ond)?|hang\s*on)\.?$/i.test(raw)) {
    return true;
  }
  if (/^(wait|hold on|one sec|hang on)\b/i.test(raw) && raw.length <= 16) {
    return true;
  }
  return false;
}

const OMNI_INTERRUPT_PREFIXES = [
  "你可以先听我说",
  "你先别说",
  "不要说了",
  "你听我说",
  "我的意思是",
  "我问的是",
  "等一下",
  "停一下",
  "别说了",
  "别讲了",
  "先别说",
  "你先停",
  "听我说",
  "换一个",
  "等等",
  "等下",
  "停下",
  "暂停",
  "先停",
  "闭嘴",
  "安静",
  "不是",
  "不对",
  "先别",
];
const OMNI_LEADING_FILLERS = [
  "那个",
  "就是",
  "嗯",
  "啊",
  "呃",
  "哦",
  "额",
  "哎",
  "喂",
  "唉",
  "欸",
];
const OMNI_NEGATED_PROPOSITION_PREFIXES = [
  "不是所有",
  "不是每",
  "不是任何",
  "不是因为",
  "不是由于",
  "不是为了",
  "不是说",
  "不对称",
  "不对等",
];
const OMNI_TRAILING_CONTROL_PARTICLES = [
  "可以吗",
  "好吗",
  "好吧",
  "好的",
  "吗",
  "嘛",
  "呢",
  "吧",
  "呀",
  "啊",
  "哦",
  "好",
];
const OMNI_STOP_PREFIXES = new Set([
  "不要说了",
  "你先别说",
  "别说了",
  "别讲了",
  "先别说",
  "你先停",
  "停下",
  "暂停",
  "先停",
  "闭嘴",
  "安静",
  "先别",
]);

function stripOmniLeadingFillers(text) {
  let remainder = text;
  while (remainder) {
    const filler = OMNI_LEADING_FILLERS.find((item) =>
      remainder.startsWith(item));
    if (!filler) return remainder;
    remainder = remainder.slice(filler.length);
  }
  return remainder;
}

function omniInterruptPrefix(text) {
  const normalized = stripOmniLeadingFillers(text);
  if (!normalized) return "";
  if (normalized === "停") return "停";
  if (OMNI_NEGATED_PROPOSITION_PREFIXES.some((prefix) =>
    normalized.startsWith(prefix))) {
    return "";
  }
  return OMNI_INTERRUPT_PREFIXES.find((prefix) =>
    normalized.startsWith(prefix)) || "";
}

function stripOmniTrailingControlParticles(text) {
  let remainder = text;
  while (remainder) {
    const particle = OMNI_TRAILING_CONTROL_PARTICLES.find((item) =>
      remainder.endsWith(item));
    if (!particle) return remainder;
    remainder = remainder.slice(0, -particle.length);
  }
  return remainder;
}

export function classifyOmniControlUtterance(text) {
  const raw = String(text || "").trim();
  if (!raw) return { kind: "empty", ack: null };

  // English / garbled wait (Plus ASR of「等一下」).
  if (isOmniWaitMishear(raw)) {
    return { kind: "interrupt_only", ack: "嗯，你说。" };
  }

  const compact = raw.replace(/[。.!！?？,，、\s「」""''…·~～]/g, "");
  let remainder = stripOmniLeadingFillers(compact);
  let prefix = omniInterruptPrefix(remainder);
  const shortTruncatedWait =
    /^[等停]{1,3}$/.test(remainder) &&
    !/[吗呢么嘛]/.test(remainder);

  if (!prefix && !shortTruncatedWait) {
    return { kind: "chat", ack: null };
  }
  if (shortTruncatedWait && !prefix) {
    prefix = remainder;
    remainder = "";
  } else {
    while (prefix) {
      remainder = remainder.slice(prefix.length);
      const betweenCommands = stripOmniLeadingFillers(remainder);
      const nextPrefix = omniInterruptPrefix(betweenCommands);
      if (!nextPrefix) break;
      remainder = betweenCommands;
      prefix = nextPrefix;
    }
  }
  const residual = stripOmniTrailingControlParticles(remainder);

  // Almost only wait intent left → pure control ack (cascade interrupt_command).
  if (!residual) {
    const hardStop = OMNI_STOP_PREFIXES.has(prefix);
    return {
      kind: "interrupt_only",
      ack: hardStop ? "好的。" : "嗯，你说。",
    };
  }
  // Wait + real content (等一下我想问…) — cancel is enough; model may answer content.
  return { kind: "interrupt_then_chat", ack: null };
}

export function omniInterruptAckPhrase(text) {
  const c = classifyOmniControlUtterance(text);
  return c.ack || "嗯，你说。";
}

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
  // Semantic floor for yield (client also hard-forces a short ack when residual is tiny).
  "若用户明确要你暂停、等等、等一下、停一下、先别说、别说了，你只简短回应“嗯，你说。”或“好的。”，不要继续长篇原话题。",
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
      voice: config.voice || "Liora Mira",
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

export class QwenOmniWebRTCTransport extends VoiceTransport {
  constructor({
    exchangeSdp,
    onState = () => undefined,
    onTranscript = () => undefined,
    onRemoteStream = () => undefined,
    onDiagnostic = () => undefined,
    onError = () => undefined,
    onDisconnected = () => undefined,
    speakerVerifyEnabled = false,
    onSpeakerProgress = () => undefined,
    onSpeakerEnrolled = () => undefined,
    onSpeakerReject = () => undefined,
  }) {
    super();
    this.exchangeSdp = exchangeSdp;
    this.onState = onState;
    this.onTranscript = onTranscript;
    this.onRemoteStream = onRemoteStream;
    this.onDiagnostic = onDiagnostic;
    this.lastInboundMetrics = null;
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
    // Server-side truth: true from response.created until response.done.
    // Local cancel must NOT clear this early — otherwise response.create races.
    this.serverResponseBusy = false;
    /** @type {{kind: string, instructions: string, diagnosticName: string}|null} */
    this.pendingControlledCreate = null;
    this.cancelSettleTimer = null;
    this.statsTimer = null;
    this.feedbackGuardTimer = null;
    this.feedbackResponseTimer = null;
    this.feedbackGuardActive = false;
    this.feedbackResponsePending = false;
    this.suppressedUserItemIds = new Set();
    // "enroll_prompt" | "interrupt_ack" | "post_enroll_welcome" | ""
    this.controlledResponseKind = "";
    this.activeResponseStartedMs = 0;
    this.pendingBargeControl = false;
    this.lastUserPartialText = "";
    this.bargeAckTimer = null;
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

  stopAssistant() {
    this.cancelResponse();
  }

  close() {
    if (this.closed) return;
    this.closed = true;
    this.sessionUpdated = false;
    this.#stopStatsSampling();
    this.#clearFeedbackTimers();
    this.#clearWelcomeTimer();
    this.#clearBargeAckTimer();
    this.#clearCancelSettleTimeout();
    this.pendingControlledCreate = null;
    this.serverResponseBusy = false;
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
        // Cascade: agent speaks enroll prompt first, then user speaks.
        this.onState("speaker_enroll");
        this.#requestEnrollPrompt(channel);
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
      // Always track user item so 等一下 ASR can still force yield ack.
      // Feedback window only suppresses auto-reply echo, not control phrases.
      const underFeedback = this.feedbackGuardActive;
      if (underFeedback) {
        this.suppressedUserItemIds.add(itemId);
        this.#expectFeedbackResponse();
        this.onDiagnostic("omni_feedback_suppressed");
      } else {
        this.#clearExpectedFeedbackResponse();
      }
      const enrolling = this.speakerGate?.state?.() === "pending";
      // Enroll speech must not permanently kill the post-enroll welcome.
      if (!enrolling) this.#suppressWelcome();
      this.turnId += 1;
      this.userTranscriptIds.set(itemId, {
        turn_id: this.turnId,
        generation_id: this.generationId,
      });
      this.activeUserItemId = itemId;
      this.userSpeaking = true;
      // Stop any playing audio when user speaks (including enroll prompt).
      if (this.responseActive) {
        this.#cancelActiveResponse();
        this.controlledResponseKind = "";
        // Barge-in: arm yield path (Flash/Plus). ASR may mangle「等一下」.
        if (!enrolling) this.#armBargeControl();
      } else if (this.responsePending && !this.controlledResponseKind) {
        // Free-form pending create → supersede; keep controlled creates alive.
        this.responseSuperseded = true;
        if (!enrolling) this.#armBargeControl();
      } else if (!enrolling && underFeedback) {
        // Prod: user says「等一下」right as AI ends → speech lands in feedback
        // guard. Auto-reply is cancelled as echo, but we still must yield-ack
        // or the user hears ~5s of dead air (session 01f6e06f ~31–37s).
        this.#armBargeControl();
      }
      this.onDiagnostic("omni_speech_started");
      this.onState(enrolling ? "speaker_enroll" : "listening");
      return;
    }
    if (event.type === "input_audio_buffer.speech_stopped") {
      const itemId = this.#itemId(event);
      // Keep tracking for control even if item was feedback-suppressed.
      this.suppressedUserItemIds.delete(itemId);
      if (
        itemId !== this.activeUserItemId ||
        !this.userTranscriptIds.has(itemId)
      ) {
        return;
      }
      this.userSpeaking = false;
      this.onDiagnostic("omni_speech_stopped");
      this.onState("thinking");
      // If barge-in and we already have a wait-like partial, yield immediately.
      if (this.pendingBargeControl && this.lastUserPartialText) {
        const control = classifyOmniControlUtterance(this.lastUserPartialText);
        if (control.kind === "interrupt_only" && control.ack) {
          this.#requestInterruptAck(control.ack);
        }
      }
      // Fallback: ASR late/garbled — still say 嗯你说 after short wait.
      this.#scheduleBargeAckFallback();
      return;
    }
    if (
      event.type === "conversation.item.input_audio_transcription.delta"
    ) {
      const itemId = this.#itemId(event);
      const ids = this.userTranscriptIds.get(itemId);
      const text = `${event.text || ""}${event.stash || ""}` || event.delta;
      if (text) this.lastUserPartialText = text;
      if (ids && text) this.#emitTranscript("user", text, false, false, ids);
      // Early yield when ASR already looks like pure wait (Flash/Plus same path).
      if (
        text &&
        this.speakerGate?.state?.() !== "pending" &&
        !this.controlledResponseKind
      ) {
        const control = classifyOmniControlUtterance(text);
        if (control.kind === "interrupt_only" && control.ack) {
          this.#requestInterruptAck(control.ack);
        }
      }
      return;
    }
    if (
      event.type === "conversation.item.input_audio_transcription.completed"
    ) {
      const itemId = this.#itemId(event);
      const ids = this.userTranscriptIds.get(itemId);
      if (itemId === this.activeUserItemId) {
        this.userSpeaking = false;
        this.activeUserItemId = "";
      }
      const text = event.transcript || event.text || this.lastUserPartialText;
      this.lastUserPartialText = "";
      this.suppressedUserItemIds.delete(itemId);
      if (ids && text) this.#emitTranscript("user", text, true, false, ids);
      this.userTranscriptIds.delete(itemId);
      // Intent residual: pure wait → forced short ack.
      // Flash and Plus share this path — do not require prior tracking ids.
      if (text && this.speakerGate?.state?.() !== "pending") {
        const control = classifyOmniControlUtterance(text);
        if (control.kind === "interrupt_only" && control.ack) {
          this.#requestInterruptAck(control.ack);
        } else if (
          this.pendingBargeControl &&
          control.kind === "interrupt_then_chat"
        ) {
          // Wait + content after barge: cancel already done; no forced ack.
          this.pendingBargeControl = false;
        } else {
          this.pendingBargeControl = false;
        }
      }
      return;
    }
    if (event.type === "response.created") {
      const responseId = this.#responseId(event);
      if (!responseId || this.cancelledResponseIds.has(responseId)) return;

      // Server has an active response until response.done (even if we cancel).
      this.serverResponseBusy = true;

      // Controlled one-shots (enroll prompt / yield ack / post-enroll) always pass.
      const controlled = this.controlledResponseKind;
      if (controlled) {
        this.controlledResponseKind = "";
        this.responsePending = false;
        this.responseSuperseded = false;
        this.pendingControlledCreate = null;
        this.generationId += 1;
        this.responseActive = true;
        this.activeResponseId = responseId;
        this.activeResponseStartedMs = performance.now();
        this.activeItemId = "";
        this.assistantText = "";
        this.assistantTextSource = "";
        this.onDiagnostic("omni_response_created", "ok", { controlled });
        void this.#sampleStats();
        this.onState("thinking");
        return;
      }

      // Auto VAD / free-form create while we still want a controlled one-shot:
      // cancel and wait for settle, then flush the queued create.
      if (this.pendingControlledCreate) {
        this.#cancelResponseId(responseId);
        this.#armCancelSettleTimeout();
        this.onDiagnostic("omni_auto_response_deferred", "ok", {
          pending: this.pendingControlledCreate.kind,
        });
        return;
      }

      // During enrollment, ignore free-form model replies to registration speech.
      if (this.speakerGate?.state?.() === "pending") {
        this.#cancelResponseId(responseId);
        this.#armCancelSettleTimeout();
        return;
      }
      this.responsePending = false;
      if (this.feedbackResponsePending) {
        this.#clearExpectedFeedbackResponse();
        this.#cancelResponseId(responseId);
        this.#armCancelSettleTimeout();
        // Echo auto-reply cancelled — if user was trying to yield (等一下),
        // still queue short ack after server settles (avoid silent hang).
        if (this.pendingBargeControl) {
          this.#scheduleBargeAckFallback();
        }
        this.onState("ready");
        return;
      }
      if (this.userSpeaking || this.responseSuperseded) {
        this.responseSuperseded = false;
        this.#cancelResponseId(responseId);
        this.#armCancelSettleTimeout();
        if (this.pendingBargeControl) {
          this.#scheduleBargeAckFallback();
        }
        return;
      }
      this.generationId += 1;
      this.responseActive = true;
      this.activeResponseId = responseId;
      this.activeResponseStartedMs = performance.now();
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
        this.#markServerResponseIdle();
        this.#tryFlushPendingControlledCreate();
        return;
      }
      if (!this.#matchesActiveResponse(event)) {
        // Stale done for a previous id: only release busy if nothing is active.
        if (!this.responseActive && this.serverResponseBusy) {
          this.#markServerResponseIdle();
          this.#tryFlushPendingControlledCreate();
        }
        return;
      }
      const elapsedMs = this.activeResponseStartedMs
        ? Math.max(0, performance.now() - this.activeResponseStartedMs)
        : 0;
      const textLen = (this.assistantText || "").trim().length;
      // Prod: ghost free-form responses finish in ~50ms with no text, then user
      // speaks and we cancel — skip feedback mute to avoid mic flash/race.
      const ghost =
        elapsedMs > 0 &&
        elapsedMs < 280 &&
        textLen < 2;
      this.#clearActiveResponse();
      this.#markServerResponseIdle();
      if (ghost) {
        this.onDiagnostic("omni_ghost_response_skipped", "ok", {
          elapsed_ms: Math.round(elapsedMs),
        });
      } else {
        this.#startPostResponseFeedbackGuard();
      }
      this.onDiagnostic("omni_response_done");
      void this.#sampleStats();
      this.onState("ready");
      this.#tryFlushPendingControlledCreate();
      return;
    }
    if (event.type === "error") {
      // Official shape: { type:"error", error:{ type, code, message, param } }
      // https://help.aliyun.com/zh/model-studio/server-events
      const err = event.error && typeof event.error === "object" ? event.error : {};
      const errorType = String(err.type || event.error_type || "").slice(0, 80);
      const code = String(err.code || event.code || "").slice(0, 80);
      const message = String(err.message || event.message || "").slice(0, 240);
      const param = String(err.param || event.param || "").slice(0, 80);
      console.error("[omni] upstream error event", {
        type: errorType,
        code,
        message,
        param,
      });
      // Recoverable race: cancel+create before server settled. Re-queue + cancel.
      if (/already has an active response/i.test(message)) {
        this.onDiagnostic("omni_active_response_conflict", "error", {
          type: errorType,
          code,
          message,
          param,
        });
        this.responsePending = false;
        this.controlledResponseKind = "";
        this.serverResponseBusy = true;
        this.#sendResponseCancel();
        this.#armCancelSettleTimeout();
        // Keep pendingControlledCreate so flush retries after settle.
        return;
      }
      this.onDiagnostic("omni_upstream_error", "error", {
        type: errorType,
        code,
        message,
        param,
      });
      const bits = [code, message].filter(Boolean).join(": ");
      this.onError(
        bits
          ? `Qwen3.5-Omni 上游错误：${bits}`
          : "Qwen3.5-Omni 服务暂时不可用，请稍后再试",
      );
      return;
    }
    if (event.type === "conversation.item.input_audio_transcription.failed") {
      const err = event.error && typeof event.error === "object" ? event.error : {};
      const code = String(err.code || "").slice(0, 80);
      const message = String(err.message || "").slice(0, 240);
      console.error("[omni] transcription failed", {
        code,
        message,
        param: String(err.param || "").slice(0, 80),
      });
      this.onDiagnostic("omni_transcription_failed", "error", {
        type: "transcription_failed",
        code,
        message,
        param: String(err.param || "").slice(0, 80),
      });
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
    this.activeResponseStartedMs = 0;
  }

  #markServerResponseIdle() {
    this.serverResponseBusy = false;
    this.#clearCancelSettleTimeout();
  }

  #clearCancelSettleTimeout() {
    if (this.cancelSettleTimer === null) return;
    window.clearTimeout(this.cancelSettleTimer);
    this.cancelSettleTimer = null;
  }

  /**
   * If response.done never arrives after cancel, force-release so queued
   * controlled creates (interrupt ack / welcome) are not stuck forever.
   */
  #armCancelSettleTimeout() {
    this.#clearCancelSettleTimeout();
    this.cancelSettleTimer = window.setTimeout(() => {
      this.cancelSettleTimer = null;
      if (this.closed) return;
      if (!this.serverResponseBusy && !this.pendingControlledCreate) return;
      this.onDiagnostic("omni_cancel_settle_timeout");
      this.serverResponseBusy = false;
      this.responsePending = false;
      // Drop local active bookkeeping — server is assumed idle after timeout.
      if (this.responseActive) this.#clearActiveResponse();
      this.#tryFlushPendingControlledCreate();
    }, 900);
  }

  #cancelActiveResponse() {
    const responseId = this.activeResponseId;
    if (!this.responseActive || !responseId) return;
    this.#cancelResponseId(responseId);
    // Keep serverResponseBusy=true until response.done — do not race create.
    this.#clearActiveResponse();
    this.#armCancelSettleTimeout();
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

  /**
   * Queue a controlled one-shot (enroll / interrupt ack / welcome).
   * Never response.create while the server still has an active response.
   */
  #requestControlledResponse(kind, instructions, diagnosticName) {
    const channel = this.commandChannel;
    if (!channel || channel.readyState !== "open" || this.closed) return false;
    // Latest controlled create wins (e.g. delta then completed for same wait).
    this.pendingControlledCreate = { kind, instructions, diagnosticName };
    if (this.responseActive) {
      this.#cancelActiveResponse();
      this.onDiagnostic("omni_controlled_create_queued", "ok", {
        kind,
        reason: "cancel_active",
      });
      return true;
    }
    if (this.serverResponseBusy) {
      // Already cancelled locally or mid-flight — wait for settle, nudge cancel.
      this.#sendResponseCancel();
      this.#armCancelSettleTimeout();
      this.onDiagnostic("omni_controlled_create_queued", "ok", {
        kind,
        reason: "server_busy",
      });
      return true;
    }
    if (this.responsePending && !this.controlledResponseKind) {
      // Free-form create in flight without id yet — supersede when it arrives.
      this.responseSuperseded = true;
      this.onDiagnostic("omni_controlled_create_queued", "ok", {
        kind,
        reason: "supersede_pending",
      });
      return true;
    }
    return this.#tryFlushPendingControlledCreate();
  }

  #tryFlushPendingControlledCreate() {
    const pending = this.pendingControlledCreate;
    const channel = this.commandChannel;
    if (!pending || this.closed) return false;
    if (!channel || channel.readyState !== "open") return false;
    if (this.serverResponseBusy || this.responseActive) return false;
    // Already sent a controlled create and waiting for response.created.
    if (this.responsePending && this.controlledResponseKind) return false;

    this.responseSuperseded = false;
    this.userSpeaking = false;
    this.controlledResponseKind = pending.kind;
    this.responsePending = true;
    // Keep pendingControlledCreate until response.created for conflict retry.
    channel.send(
      JSON.stringify({
        type: "response.create",
        response: {
          modalities: ["text", "audio"],
          instructions: pending.instructions,
        },
      }),
    );
    this.onDiagnostic(pending.diagnosticName, "ok", { kind: pending.kind });
    this.onState(
      pending.kind === "enroll_prompt" ? "speaker_enroll" : "thinking",
    );
    return true;
  }

  /** Cascade-like: agent speaks enroll prompt first. */
  #requestEnrollPrompt(_channel) {
    this.#requestControlledResponse(
      "enroll_prompt",
      ENROLL_PROMPT_INSTRUCTIONS,
      "omni_enroll_prompt_requested",
    );
  }

  /**
   * Yield after pure wait intent (等等 / 停一下 family), matching cascade.
   */
  #clearBargeAckTimer() {
    if (this.bargeAckTimer === null) return;
    window.clearTimeout(this.bargeAckTimer);
    this.bargeAckTimer = null;
  }

  #armBargeControl() {
    this.pendingBargeControl = true;
    this.#clearBargeAckTimer();
  }

  /**
   * After barge-in, if final ASR never looks like wait (or is English garbage),
   * still force a short yield so silence does not feel like a hang.
   * Only when residual is short / wait-like — not for full questions.
   */
  #scheduleBargeAckFallback() {
    if (!this.pendingBargeControl) return;
    this.#clearBargeAckTimer();
    this.bargeAckTimer = window.setTimeout(() => {
      this.bargeAckTimer = null;
      if (!this.pendingBargeControl || this.closed) return;
      if (this.controlledResponseKind === "interrupt_ack") return;
      const text = this.lastUserPartialText || "";
      const control = classifyOmniControlUtterance(text);
      if (control.kind === "interrupt_only" && control.ack) {
        this.#requestInterruptAck(control.ack);
        return;
      }
      // Empty / ultra-short after barge while AI was talking → treat as stop.
      const compact = text.replace(/[。.!！?？,，、\s「」""'']/g, "");
      if (!text || compact.length <= 4 || isOmniWaitMishear(text)) {
        this.#requestInterruptAck("嗯，你说。");
      } else {
        this.pendingBargeControl = false;
      }
    }, 700);
  }

  #requestInterruptAck(phrase) {
    // Avoid double-firing from delta + completed for the same wait phrase.
    if (this.controlledResponseKind === "interrupt_ack") return;
    if (this.pendingControlledCreate?.kind === "interrupt_ack") return;
    if (
      this.responseActive &&
      this.assistantText &&
      /嗯，?你说|好的/.test(this.assistantText)
    ) {
      return;
    }
    this.pendingBargeControl = false;
    this.#clearBargeAckTimer();
    const ack = (phrase || "").trim() || "嗯，你说。";
    this.#requestControlledResponse(
      "interrupt_ack",
      `用户只是让你暂停、把说话权还给他。你必须只说这句短确认，一个字都不要多：${ack}`,
      "omni_interrupt_ack_requested",
    );
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
      // Enroll speech must not permanently kill post-enroll welcome.
      this.welcomeSuppressed = false;
      this.welcomeRequested = false;
      this.onState("ready");
      this.#armWelcome(channel, { forcePostEnroll: true });
    };
    tick();
  }

  #armWelcome(channel, { forcePostEnroll = false } = {}) {
    if (
      this.welcomeRequested ||
      this.welcomeSuppressed ||
      this.welcomeTimer !== null ||
      channel.readyState !== "open"
    ) {
      return;
    }
    this.onDiagnostic("omni_welcome_armed");
    const enrolled =
      forcePostEnroll || this.speakerGate?.state?.() === "enrolled";
    const welcomeInstructions = enrolled
      ? POST_ENROLL_WELCOME_INSTRUCTIONS
      : WELCOME_INSTRUCTIONS;
    const quietMs = enrolled ? 250 : WELCOME_QUIET_WINDOW_MS;
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
      this.#requestControlledResponse(
        enrolled ? "post_enroll_welcome" : "welcome",
        welcomeInstructions,
        "omni_welcome_requested",
      );
      void this.#sampleStats();
    }, quietMs);
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
    const guardMs = computeFeedbackGuardMs(this.lastInboundMetrics || {});
    this.feedbackGuardActive = true;
    this.#syncMicrophoneTracks();
    this.onDiagnostic("omni_feedback_guard_started", "ok", {
      guard_ms: guardMs,
      dynamic: true,
      concealment_ratio:
        this.lastInboundMetrics?.non_silent_concealment_ratio ??
        this.lastInboundMetrics?.concealment_ratio ??
        null,
      average_jitter_buffer_delay_ms:
        this.lastInboundMetrics?.average_jitter_buffer_delay_ms ?? null,
    });
    this.feedbackGuardTimer = window.setTimeout(() => {
      this.feedbackGuardTimer = null;
      this.feedbackGuardActive = false;
      this.#syncMicrophoneTracks();
    }, guardMs);
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
        this.lastInboundMetrics = metrics;
        this.onDiagnostic("webrtc_inbound_audio", "ok", metrics);
      }
    } catch {
      // Stats are diagnostic-only and must never disturb the conversation.
    }
  }
}
