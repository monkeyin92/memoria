import { useCallback, useEffect, useReducer, useRef, useState } from "react";

import {
  createSession,
  exchangeOmniSdp,
  publishOmniTelemetry,
  notifyRtcRecovered,
  stopResponse,
} from "../api.js";
import { LiveKitCascadeTransport } from "../voice/LiveKitCascadeTransport.js";
import { QwenOmniWebRTCTransport } from "../voice/experimental/QwenOmniWebRTCTransport.js";
import {
  addInboundAudioDeltas,
  extractInboundAudioStats,
} from "../voice/webrtcStats.js";
import {
  initialVoiceSessionState,
  voiceSessionReducer,
} from "../voice/voiceSessionState.js";

const UI_TOPIC = "voice-agent.ui";
const TELEMETRY_TOPIC = "voice-agent.telemetry";
const AGENT_READY_TIMEOUT_MS = 45_000;
const SPEAKER_REJECT_MESSAGE =
  "没有确认到主人声音，已忽略这句话。若是你本人，可在“我的”关闭“过滤明显旁人（实验）”后再说一次。";
const VOICE_EMOTION_LABELS = new Set([
  "neutral",
  "happy",
  "sad",
  "angry",
  "fearful",
  "disgusted",
  "surprised",
]);
/** End-to-end realtime backends (not LiveKit cascade). */
const REALTIME_BACKENDS = new Set(["qwen_omni"]);

function isRealtimeBackend(backend) {
  return REALTIME_BACKENDS.has(backend);
}

function cascadeAudioTrackKey(track) {
  return track.sid || track.mediaStreamTrack?.id || track;
}

function parsePreviewProvenance(value, event) {
  if (value === undefined) return null;
  if (
    !value ||
    typeof value !== "object" ||
    Array.isArray(value) ||
    event.speaker !== "assistant" ||
    event.final !== true ||
    event.heard !== true ||
    typeof value.digital_self_version_id !== "string" ||
    !value.digital_self_version_id ||
    typeof value.manifest_sha256 !== "string" ||
    !/^[a-f0-9]{64}$/i.test(value.manifest_sha256) ||
    value.turn_id !== event.turn_id ||
    value.generation_id !== event.generation_id ||
    !Number.isInteger(event.tool_epoch) ||
    event.tool_epoch !== value.tool_epoch ||
    !Number.isInteger(value.tool_epoch) ||
    value.tool_epoch < 0 ||
    !["fact", "inference", "unknown", "not_applicable"].includes(
      value.epistemic_status,
    ) ||
    !Array.isArray(value.disclosures) ||
    value.disclosures.length > 4 ||
    value.disclosures.some((item) => typeof item !== "string" || !item) ||
    !value.disclosures.includes("digital_identity") ||
    !Array.isArray(value.source_refs) ||
    value.source_refs.length > 12 ||
    (["fact", "inference"].includes(value.epistemic_status) &&
      value.source_refs.length === 0)
  ) {
    return undefined;
  }
  const sourceRefs = value.source_refs.map((source) => {
    if (
      !source ||
      typeof source !== "object" ||
      Array.isArray(source) ||
      typeof source.kind !== "string" ||
      !source.kind ||
      typeof source.item_id !== "string" ||
      !source.item_id ||
      !Array.isArray(source.source_event_ids) ||
      !source.source_event_ids.length ||
      source.source_event_ids.length > 8 ||
      source.source_event_ids.some(
        (eventId) => typeof eventId !== "string" || !eventId,
      )
    ) {
      return null;
    }
    return {
      kind: source.kind,
      item_id: source.item_id,
      source_event_ids: [...source.source_event_ids],
    };
  });
  if (sourceRefs.some((source) => source === null)) return undefined;
  return {
    digital_self_version_id: value.digital_self_version_id,
    manifest_sha256: value.manifest_sha256.toLowerCase(),
    turn_id: value.turn_id,
    generation_id: value.generation_id,
    tool_epoch: value.tool_epoch,
    epistemic_status: value.epistemic_status,
    disclosures: [...value.disclosures],
    source_refs: sourceRefs,
  };
}

const stateLabels = {
  idle: "轻触我，开始聊聊",
  connecting: "正在靠近你…",
  ready: "我在这里",
  speaker_enroll: "请说几句话，登记你的声音",
  listening: "我在认真听",
  thinking: "让我想一想",
  speaking: "正在回应你",
  interrupted: "好，我先停一下",
  reconnecting: "正在重新连接",
  closed: "今天先聊到这里",
};

function mapServerState(state) {
  if (state === "ready") return "ready";
  if (state === "speaker_enroll") return "speaker_enroll";
  // backchannel: short "嗯/我在听" while the user still holds the floor.
  if (
    [
      "listening",
      "user_speaking",
      "eot_pending",
      "backchannel",
    ].includes(state)
  ) {
    return "listening";
  }
  if (["thinking", "thinking_silent", "tool_waiting"].includes(state)) {
    return "thinking";
  }
  if (state === "speaking") return "speaking";
  if (["interrupted", "interruption_pending"].includes(state)) {
    return "interrupted";
  }
  if (state === "recovering") return "reconnecting";
  if (state === "closed") return "closed";
  return null;
}

function parseEvent(payload) {
  try {
    const event = JSON.parse(new TextDecoder().decode(payload));
    if (!event || typeof event.type !== "string") return null;
    if (event.type === "assistant_state") {
      if (
        typeof event.session_id !== "string" ||
        typeof event.state !== "string" ||
        !Number.isInteger(event.turn_id) ||
        !Number.isInteger(event.generation_id)
      ) {
        return null;
      }
      return event;
    }
    if (event.type === "transcript_delta") {
      if (
        typeof event.session_id !== "string" ||
        !event.session_id ||
        !["user", "assistant"].includes(event.speaker) ||
        typeof event.text !== "string" ||
        typeof event.final !== "boolean" ||
        !Number.isInteger(event.turn_id) ||
        !Number.isInteger(event.generation_id) ||
        (event.turn_revision !== undefined &&
          (!Number.isInteger(event.turn_revision) || event.turn_revision < 1))
      ) {
        return null;
      }
      const previewProvenance = parsePreviewProvenance(
        event.preview_provenance,
        event,
      );
      if (previewProvenance === undefined) return null;
      return {
        ...event,
        history_eligible: event.history_eligible === true,
        preview_provenance: previewProvenance,
      };
    }
    if (event.type === "audio_trace") {
      if (
        typeof event.session_id !== "string" ||
        typeof event.name !== "string" ||
        typeof event.status !== "string" ||
        !Number.isInteger(event.turn_id) ||
        !Number.isInteger(event.generation_id)
      ) {
        return null;
      }
      return event;
    }
    if (event.type === "assistant_audio") {
      if (
        typeof event.session_id !== "string" ||
        !["duck", "restore"].includes(event.action) ||
        typeof event.gain !== "number" ||
        event.gain < 0 ||
        event.gain > 1 ||
        !Number.isInteger(event.turn_id) ||
        !Number.isInteger(event.generation_id)
      ) {
        return null;
      }
      return event;
    }
    if (event.type === "emotion_observation") {
      if (
        typeof event.session_id !== "string" ||
        !VOICE_EMOTION_LABELS.has(event.label) ||
        event.persist !== false ||
        !Number.isInteger(event.turn_id) ||
        !Number.isInteger(event.generation_id) ||
        !Number.isInteger(event.expires_after_ms) ||
        event.expires_after_ms <= 0 ||
        event.expires_after_ms > 60_000
      ) {
        return null;
      }
      return event;
    }
  } catch {
    return null;
  }
  return null;
}

export function useVoiceSession({
  userId,
  onFinalTranscript,
  voiceReplyEnabled = true,
  voiceBackend = "cascade",
  learningTaskId = null,
  interactionMode = "companion",
  previewGrantId = null,
  legacyGrantId = null,
}) {
  const [session, setSession] = useState(null);
  const [voiceSessionState, dispatchVoiceSession] = useReducer(
    voiceSessionReducer,
    initialVoiceSessionState,
  );
  const uiState = voiceSessionState.status;
  const [micEnabled, setMicEnabledState] = useState(true);
  const [transcripts, setTranscripts] = useState([]);
  const [error, setError] = useState("");
  const [audioBlocked, setAudioBlocked] = useState(false);
  const [audioDiagnostics, setAudioDiagnostics] = useState([]);
  const [emotionHint, setEmotionHint] = useState(null);
  const roomRef = useRef(null);
  const cascadeTransportRef = useRef(null);
  const omniTransportRef = useRef(null);
  const audioContainerRef = useRef(null);
  const sessionRef = useRef(null);
  const attemptRef = useRef(0);
  const initialReadyRef = useRef(false);
  const recoveryInFlightRef = useRef(false);
  const recoveryEpochRef = useRef(0);
  const roomConnectedRef = useRef(false);
  const generationRef = useRef(0);
  const turnRef = useRef(0);
  const micEnabledRef = useRef(true);
  const voiceReplyEnabledRef = useRef(voiceReplyEnabled);
  const reconnectTimerRef = useRef(null);
  const agentReadyTimerRef = useRef(null);
  const intentionalEndRef = useRef(false);
  const persistedRef = useRef(new Set());
  const transcriptRevisionRef = useRef(new Map());
  const finalTranscriptRef = useRef(onFinalTranscript);
  const audioDiagnosticsRef = useRef([]);
  const traceStartedAtRef = useRef(0);
  const firstPlaybackRef = useRef(false);
  const audioGainRef = useRef(1);
  const cascadeAudioElementsRef = useRef(new Map());
  const omniAudioElementRef = useRef(null);
  const statsTrackRef = useRef(null);
  const statsTimerRef = useRef(null);
  const statsBaselineRef = useRef(null);
  const pendingEmotionRef = useRef(new Map());
  const latestAcceptedUserTurnRef = useRef(0);
  const emotionTimerRef = useRef(null);
  const setUiState = useCallback((status, options = {}) => {
    dispatchVoiceSession({
      type: "transition",
      status,
      attempt: options.attempt ?? attemptRef.current,
      generation: options.generation ?? generationRef.current,
    });
  }, []);
  const resetUiState = useCallback((attempt) => {
    dispatchVoiceSession({ type: "reset", attempt });
  }, []);

  useEffect(() => {
    finalTranscriptRef.current = onFinalTranscript;
  }, [onFinalTranscript]);

  useEffect(() => {
    voiceReplyEnabledRef.current = voiceReplyEnabled;
    const elements = audioContainerRef.current?.querySelectorAll("audio") || [];
    elements.forEach((element) => {
      element.muted = !voiceReplyEnabled;
    });
    if (!voiceReplyEnabled) setAudioBlocked(false);
  }, [voiceReplyEnabled]);

  const clearEmotionHint = useCallback(() => {
    if (emotionTimerRef.current !== null) {
      window.clearTimeout(emotionTimerRef.current);
      emotionTimerRef.current = null;
    }
    setEmotionHint(null);
  }, []);

  const resetEmotionState = useCallback(() => {
    clearEmotionHint();
    pendingEmotionRef.current.clear();
    latestAcceptedUserTurnRef.current = 0;
  }, [clearEmotionHint]);

  const activateEmotionHint = useCallback((event) => {
    if (emotionTimerRef.current !== null) {
      window.clearTimeout(emotionTimerRef.current);
    }
    setEmotionHint({
      label: event.label,
      turnId: event.turn_id,
      generationId: event.generation_id,
    });
    emotionTimerRef.current = window.setTimeout(() => {
      emotionTimerRef.current = null;
      setEmotionHint(null);
    }, event.expires_after_ms);
  }, []);

  const applyTranscript = useCallback((line, { authoritative = true } = {}) => {
    if (line.generation_id < generationRef.current) return;
    const key = `${line.speaker}:${line.turn_id}:${line.generation_id}`;
    const revision =
      Number.isInteger(line.turn_revision) && line.turn_revision >= 1
      ? line.turn_revision
      : null;
    if (authoritative) {
      const latestRevision = transcriptRevisionRef.current.get(key);
      if (revision === null) {
        if (latestRevision !== undefined) return;
      } else {
        if (latestRevision !== undefined && revision <= latestRevision) return;
        transcriptRevisionRef.current.set(key, revision);
      }
    }
    generationRef.current = Math.max(
      generationRef.current,
      line.generation_id,
    );
    turnRef.current = line.turn_id;

    if (
      authoritative &&
      line.speaker === "user" &&
      line.final &&
      line.text.trim()
    ) {
      setError((current) =>
        current === SPEAKER_REJECT_MESSAGE ? "" : current,
      );
      clearEmotionHint();
      latestAcceptedUserTurnRef.current = line.turn_id;
      const pendingEmotion = pendingEmotionRef.current.get(line.turn_id);
      pendingEmotionRef.current.clear();
      if (
        pendingEmotion &&
        pendingEmotion.generation_id === line.generation_id
      ) {
        activateEmotionHint(pendingEmotion);
      }
    }

    setTranscripts((current) => {
      const index = current.findIndex((item) => item.key === key);
      if (index >= 0 && !authoritative && current[index].authoritative) {
        return current;
      }
      const next = {
        key,
        speaker: line.speaker,
        text: line.text,
        final: line.final,
        heard: line.heard,
        turnId: line.turn_id,
        generationId: line.generation_id,
        turnRevision: revision,
        toolEpoch: Number.isInteger(line.tool_epoch)
          ? line.tool_epoch
          : line.preview_provenance?.tool_epoch ?? null,
        previewProvenance: line.preview_provenance || null,
        source_refs: line.preview_provenance?.source_refs || [],
        epistemic_status:
          line.preview_provenance?.epistemic_status || null,
        disclosures: line.preview_provenance?.disclosures || [],
        version_id:
          line.preview_provenance?.digital_self_version_id || null,
        manifest_sha256:
          line.preview_provenance?.manifest_sha256 || null,
        authoritative,
      };
      if (index < 0) return [...current.slice(-11), next];
      const copy = [...current];
      copy[index] = next;
      return copy;
    });

    const shouldPersist =
      authoritative &&
      line.final &&
      line.text.trim() &&
      line.history_eligible === true &&
      (line.speaker === "user" || line.heard === true);
    const persistKey = `${line.speaker}:${line.turn_id}:${line.generation_id}`;
    if (shouldPersist && !persistedRef.current.has(persistKey)) {
      persistedRef.current.add(persistKey);
      finalTranscriptRef.current?.({
        speaker: line.speaker,
        text: line.text.trim(),
        history_eligible: true,
        turn_id: line.turn_id,
        generation_id: line.generation_id,
      });
    }
  }, [activateEmotionHint, clearEmotionHint]);

  const publishAudioDiagnostic = useCallback((room, event) => {
    if (
      !roomConnectedRef.current ||
      !event.session_id ||
      typeof room?.localParticipant?.publishData !== "function"
    ) {
      return;
    }
    const payload = new TextEncoder().encode(JSON.stringify(event));
    void room.localParticipant
      .publishData(payload, { reliable: true, topic: TELEMETRY_TOPIC })
      .catch(() => undefined);
  }, []);

  const recordAudioDiagnostic = useCallback(
    (name, status = "ok", detail = undefined) => {
      const sessionId = sessionRef.current?.session_id || "";
      const event = {
        type: "audio_trace",
        source: "client",
        session_id: sessionId,
        name,
        status,
        turn_id: turnRef.current,
        generation_id: generationRef.current,
        elapsed_ms: Math.max(
          0,
          Math.round(performance.now() - traceStartedAtRef.current),
        ),
        ...(detail ? { detail } : {}),
      };
      audioDiagnosticsRef.current = [
        ...audioDiagnosticsRef.current.slice(-49),
        event,
      ];
      setAudioDiagnostics(audioDiagnosticsRef.current);
      publishAudioDiagnostic(roomRef.current, event);
      if (isRealtimeBackend(sessionRef.current?.voice_backend)) {
        const payload = {
          name,
          elapsed_ms: event.elapsed_ms,
          turn_id: event.turn_id,
          generation_id: event.generation_id,
          ...(name === "webrtc_inbound_audio" && detail
            ? { metrics: detail }
            : {}),
          // Send only bounded classifications; provider message bodies may contain text.
          ...(name === "omni_upstream_error" ||
          name === "omni_transcription_failed"
            ? {
                error_type: String(detail?.type || detail?.error_type || "").slice(
                  0,
                  80,
                ) || null,
                error_code: String(detail?.code || detail?.error_code || "").slice(
                  0,
                  80,
                ) || null,
                error_param: String(detail?.param || detail?.error_param || "").slice(
                  0,
                  80,
                ) || null,
              }
            : {}),
        };
        void publishOmniTelemetry(sessionId, payload).catch(() => undefined);
      }
    },
    [publishAudioDiagnostic],
  );

  const activateAudioElement = useCallback(
    (element, isCurrent = () => true) => {
      if (!isCurrent() || !audioContainerRef.current) return;
      element.autoplay = true;
      element.playsInline = true;
      element.muted = !voiceReplyEnabledRef.current;
      element.volume = audioGainRef.current;
      audioContainerRef.current.append(element);
      recordAudioDiagnostic("audio_attached", "ok", {
        muted: element.muted,
      });

      element.addEventListener(
        "playing",
        () => {
          if (isCurrent()) recordAudioDiagnostic("playing");
        },
        { once: true },
      );
      const onTimeUpdate = () => {
        if (
          !isCurrent() ||
          firstPlaybackRef.current ||
          element.currentTime <= 0
        ) {
          return;
        }
        firstPlaybackRef.current = true;
        element.removeEventListener("timeupdate", onTimeUpdate);
        recordAudioDiagnostic("first_playback", "ok", {
          current_time_s: Number(element.currentTime.toFixed(3)),
        });
      };
      element.addEventListener("timeupdate", onTimeUpdate);
      element.addEventListener(
        "error",
        () => {
          if (isCurrent()) recordAudioDiagnostic("media_error", "error");
        },
        { once: true },
      );

      if (!element.muted) {
        try {
          const playback = element.play();
          playback
            ?.then(() => {
              if (!isCurrent()) return;
              setAudioBlocked(false);
              recordAudioDiagnostic("play_resolved");
            })
            .catch((caught) => {
              if (!isCurrent()) return;
              setAudioBlocked(true);
              recordAudioDiagnostic("play_rejected", "error", {
                error: caught?.name || "unknown",
              });
            });
        } catch (caught) {
          if (isCurrent()) {
            setAudioBlocked(true);
            recordAudioDiagnostic("play_rejected", "error", {
              error: caught?.name || "unknown",
            });
          }
        }
      }
    },
    [recordAudioDiagnostic],
  );

  const attachAudio = useCallback(
    (track, isCurrent = () => true) => {
      if (!isCurrent() || track.kind !== "audio") return;
      const key = cascadeAudioTrackKey(track);
      if (cascadeAudioElementsRef.current.has(key)) return;
      const element = track.attach();
      cascadeAudioElementsRef.current.set(key, element);
      activateAudioElement(element, isCurrent);
    },
    [activateAudioElement],
  );

  const attachOmniAudio = useCallback(
    (stream, isCurrent = () => true) => {
      if (!isCurrent()) return;
      const existing = omniAudioElementRef.current;
      if (existing) {
        if (existing.srcObject !== stream) existing.srcObject = stream;
        return;
      }
      const element = document.createElement("audio");
      element.srcObject = stream;
      omniAudioElementRef.current = element;
      activateAudioElement(element, isCurrent);
    },
    [activateAudioElement],
  );

  const stopStatsSampling = useCallback(() => {
    if (statsTimerRef.current !== null) {
      window.clearInterval(statsTimerRef.current);
      statsTimerRef.current = null;
    }
    statsTrackRef.current = null;
    statsBaselineRef.current = null;
  }, []);

  const startStatsSampling = useCallback(
    (track, isCurrent) => {
      stopStatsSampling();
      if (typeof track?.getRTCStatsReport !== "function") return;
      statsTrackRef.current = track;
      const sample = async () => {
        if (!isCurrent() || statsTrackRef.current !== track) return;
        try {
          const metrics = extractInboundAudioStats(
            await track.getRTCStatsReport(),
          );
          if (isCurrent() && statsTrackRef.current === track && metrics) {
            statsBaselineRef.current ||= metrics;
            recordAudioDiagnostic(
              "webrtc_inbound_audio",
              "ok",
              addInboundAudioDeltas(metrics, statsBaselineRef.current),
            );
          }
        } catch {
          // Stats are diagnostic-only and must never disturb playback.
        }
      };
      void sample();
      statsTimerRef.current = window.setInterval(() => void sample(), 5_000);
    },
    [recordAudioDiagnostic, stopStatsSampling],
  );

  const clearReconnectTimer = useCallback(() => {
    if (reconnectTimerRef.current !== null) {
      window.clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
  }, []);

  const clearAgentReadyTimer = useCallback(() => {
    if (agentReadyTimerRef.current !== null) {
      window.clearTimeout(agentReadyTimerRef.current);
      agentReadyTimerRef.current = null;
    }
  }, []);

  const disconnectRoom = useCallback(async (room) => {
    if (!room) {
      clearReconnectTimer();
      clearAgentReadyTimer();
      setAudioBlocked(false);
      return;
    }
    const isCurrent = roomRef.current === room;
    const transport =
      cascadeTransportRef.current?.room === room
        ? cascadeTransportRef.current
        : null;
    if (isCurrent) {
      clearReconnectTimer();
      clearAgentReadyTimer();
      roomRef.current = null;
      cascadeTransportRef.current = null;
      initialReadyRef.current = false;
      recoveryInFlightRef.current = false;
      recoveryEpochRef.current += 1;
      roomConnectedRef.current = false;
      stopStatsSampling();
      cascadeAudioElementsRef.current.clear();
      audioContainerRef.current?.replaceChildren();
      setAudioBlocked(false);
    }
    try {
      if (transport) await transport.close();
      else await room.disconnect();
    } catch {
      // The local refs are already cleared, so the user can always retry.
    }
  }, [clearAgentReadyTimer, clearReconnectTimer, stopStatsSampling]);

  const disconnectOmni = useCallback((transport) => {
    if (!transport) return;
    const isCurrent = omniTransportRef.current === transport;
    if (isCurrent) {
      clearReconnectTimer();
      clearAgentReadyTimer();
      omniTransportRef.current = null;
      const audioElement = omniAudioElementRef.current;
      if (audioElement) {
        audioElement.pause();
        audioElement.srcObject = null;
        audioElement.remove();
        omniAudioElementRef.current = null;
      }
      audioContainerRef.current?.replaceChildren();
      setAudioBlocked(false);
    }
    transport.close();
  }, [clearAgentReadyTimer, clearReconnectTimer]);

  const failReconnect = useCallback(
    async (room, message) => {
      if (roomRef.current !== room) return;
      attemptRef.current += 1;
      intentionalEndRef.current = true;
      setError(message);
      resetEmotionState();
      const disconnecting = disconnectRoom(room);
      sessionRef.current = null;
      setSession(null);
      setUiState("closed");
      await disconnecting;
    },
    [disconnectRoom, resetEmotionState],
  );

  const failOmni = useCallback(
    (transport, message) => {
      if (omniTransportRef.current !== transport) return;
      attemptRef.current += 1;
      intentionalEndRef.current = true;
      setError(message);
      resetEmotionState();
      disconnectOmni(transport);
      sessionRef.current = null;
      setSession(null);
      setUiState("closed");
    },
    [disconnectOmni, resetEmotionState],
  );

  const resumeAudio = useCallback((enabled = voiceReplyEnabledRef.current) => {
    voiceReplyEnabledRef.current = enabled;
    const cascadeTransport = cascadeTransportRef.current;
    const room = cascadeTransport?.room || null;
    const omniTransport = omniTransportRef.current;
    const isCurrent = () =>
      cascadeTransport
        ? cascadeTransportRef.current === cascadeTransport
        : omniTransportRef.current === omniTransport;
    let unlockPromise = Promise.resolve();
    if (enabled && cascadeTransport) {
      try {
        unlockPromise = cascadeTransport.resumeAudio();
      } catch {
        unlockPromise = Promise.reject(new Error("audio unlock failed"));
      }
    }
    setAudioBlocked(false);
    return (async () => {
      try {
        await unlockPromise;
        if (!isCurrent()) return false;
        recordAudioDiagnostic("audio_unlock", "ok", {
          can_playback_audio: room?.canPlaybackAudio ?? null,
        });
        const activeEnabled = voiceReplyEnabledRef.current;
        const elements = audioContainerRef.current?.querySelectorAll("audio") || [];
        await Promise.all(
          [...elements].map((element) => {
            element.muted = !activeEnabled;
            return element.muted ? Promise.resolve() : element.play();
          }),
        );
        return true;
      } catch {
        if (isCurrent()) {
          setAudioBlocked(true);
          recordAudioDiagnostic("audio_unlock", "error");
        }
        return false;
      }
    })();
  }, [recordAudioDiagnostic]);

  const start = useCallback((overrides = {}) => {
    if (cascadeTransportRef.current || omniTransportRef.current) return null;
    if (!userId) {
      setError("匿名身份尚未就绪，请稍后再试");
      return null;
    }
    const attempt = attemptRef.current + 1;
    attemptRef.current = attempt;
    intentionalEndRef.current = false;
    initialReadyRef.current = false;
    recoveryInFlightRef.current = false;
    recoveryEpochRef.current += 1;
    roomConnectedRef.current = false;
    resetEmotionState();
    setError("");
    setAudioBlocked(false);
    setUiState("connecting", { attempt, generation: 0 });
    setTranscripts([]);
    generationRef.current = 0;
    turnRef.current = 0;
    micEnabledRef.current = true;
    setMicEnabledState(true);
    persistedRef.current.clear();
    transcriptRevisionRef.current.clear();
    traceStartedAtRef.current = performance.now();
    firstPlaybackRef.current = false;
    audioGainRef.current = 1;
    audioDiagnosticsRef.current = [];
    setAudioDiagnostics([]);

    const selectedInteractionMode =
      overrides.interactionMode || interactionMode;
    const selectedBackend =
      selectedInteractionMode === "companion" && isRealtimeBackend(voiceBackend)
        ? voiceBackend
        : "cascade";
    const selectedPreviewGrantId =
      overrides.previewGrantId || previewGrantId;
    const selectedLegacyGrantId =
      overrides.legacyGrantId || legacyGrantId;
    const selectedLearningTaskId =
      selectedInteractionMode === "companion" ? learningTaskId : null;
    const sessionOptions = selectedInteractionMode === "self_preview"
      ? {
          interactionMode: selectedInteractionMode,
          previewGrantId: selectedPreviewGrantId,
        }
      : selectedInteractionMode === "legacy"
        ? {
            interactionMode: selectedInteractionMode,
            legacyGrantId: selectedLegacyGrantId,
          }
        : null;
    if (isRealtimeBackend(selectedBackend)) {
      let transport;
      const isCurrent = () =>
        attemptRef.current === attempt &&
        omniTransportRef.current === transport;
      const transportOptions = {
        // Registration onboarding + formal SpeakerAuthority own voiceprints.
        // A browser-local gate would ask the user to enroll again per session.
        speakerVerifyEnabled: false,
        onState: (state) => {
          if (!isCurrent()) return;
          if (state === "ready" || state === "speaker_enroll") {
            initialReadyRef.current = true;
            clearAgentReadyTimer();
          }
          setUiState(state);
        },
        onTranscript: (line) => {
          if (!isCurrent() || (line.speaker === "user" && !line.final)) return;
          applyTranscript(line, { authoritative: true });
        },
        onRemoteStream: (stream) => attachOmniAudio(stream, isCurrent),
        onDiagnostic: (name, status = "ok", detail = undefined) => {
          if (!isCurrent()) return;
          const element = omniAudioElementRef.current;
          if (
            element &&
            ["omni_speech_started", "omni_response_cancelled"].includes(name)
          ) {
            element.muted = true;
          } else if (element && name === "omni_response_created") {
            element.muted = !voiceReplyEnabledRef.current;
          }
          recordAudioDiagnostic(name, status, detail);
        },
        onSpeakerProgress: () => {
          if (isCurrent()) setUiState("speaker_enroll");
        },
        onSpeakerEnrolled: (result) => {
          if (!isCurrent()) return;
          recordAudioDiagnostic("speaker_enrolled", "ok", result);
        },
        onSpeakerReject: (detail) => {
          if (!isCurrent()) return;
          recordAudioDiagnostic("speaker_rejected", "ignored", detail);
        },
        onError: (message) => {
          if (isCurrent()) setError(message);
        },
        onDisconnected: () => {
          if (!isCurrent()) return;
          attemptRef.current += 1;
          const wasIntentional = intentionalEndRef.current;
          disconnectOmni(transport);
          sessionRef.current = null;
          resetEmotionState();
          setSession(null);
          setUiState("closed");
          if (!wasIntentional) {
            setError("连接已经断开，轻触吉祥物可以重新开始");
          }
        },
      };
      transport = new QwenOmniWebRTCTransport({
        exchangeSdp: exchangeOmniSdp,
        ...transportOptions,
      });
      omniTransportRef.current = transport;
      let preparation;
      try {
        preparation = transport.prepare();
      } catch (caught) {
        preparation = Promise.reject(caught);
      }
      const creation = sessionOptions
        ? createSession(
            userId,
            selectedBackend,
            selectedLearningTaskId,
            sessionOptions,
          )
        : createSession(userId, selectedBackend, selectedLearningTaskId);
      const prepared = preparation.then(
        () => ({ ok: true }),
        (caught) => ({ ok: false, caught }),
      );
      return (async () => {
        try {
          const created = await creation;
          const preparationResult = await prepared;
          if (!preparationResult.ok) throw preparationResult.caught;
          if (!isCurrent()) {
            disconnectOmni(transport);
            return;
          }
          if (
            created.voice_backend &&
            created.voice_backend !== selectedBackend
          ) {
            throw new Error("服务端返回了不匹配的语音模式");
          }
          sessionRef.current = created;
          setSession(created);
          recordAudioDiagnostic("session_created");
          await transport.connect(created);
          if (!isCurrent()) return;
          if (!initialReadyRef.current) {
            agentReadyTimerRef.current = window.setTimeout(() => {
              failOmni(transport, "连接超时，请轻触吉祥物再试");
            }, AGENT_READY_TIMEOUT_MS);
          }
          return created;
        } catch (caught) {
          if (isCurrent()) {
            const denied =
              caught instanceof DOMException && caught.name === "NotAllowedError";
            failOmni(
              transport,
              denied
                ? "需要麦克风权限，才能听见你说话"
                : caught instanceof Error
                  ? caught.message
                  : "暂时无法开始 Qwen3.5-Omni-Flash 对话",
            );
          } else {
            disconnectOmni(transport);
          }
          return null;
        }
      })();
    }

    let created = null;
    let transport;
    const isCurrent = () =>
      attemptRef.current === attempt &&
      cascadeTransportRef.current === transport;
    const onTrackSubscribed = (track) => {
      if (!isCurrent()) return;
      if (track.kind === "audio") {
        recordAudioDiagnostic("track_subscribed");
      }
      attachAudio(track, isCurrent);
      if (track.kind === "audio") startStatsSampling(track, isCurrent);
    };
    const onTrackUnsubscribed = (track) => {
      if (!isCurrent()) return;
      const key = cascadeAudioTrackKey(track);
      const element = cascadeAudioElementsRef.current.get(key);
      cascadeAudioElementsRef.current.delete(key);
      track.detach().forEach((detached) => detached.remove());
      element?.remove();
      if (statsTrackRef.current === track) stopStatsSampling();
    };
    const onDataReceived = (payload, participant, _kind, topic) => {
      if (!isCurrent() || !participant?.isAgent || topic !== UI_TOPIC) return;
      const event = parseEvent(payload);
      if (!event) return;
      if (event.type === "audio_trace") {
        if (event.session_id !== sessionRef.current?.session_id) return;
        if (event.generation_id < generationRef.current) return;
        audioDiagnosticsRef.current = [
          ...audioDiagnosticsRef.current.slice(-49),
          { ...event, source: event.source || "agent" },
        ];
        setAudioDiagnostics(audioDiagnosticsRef.current);
        if (
          event.name === "target_speaker_rejected" &&
          event.detail?.reason === "target_non_owner"
        ) {
          setError(SPEAKER_REJECT_MESSAGE);
        }
        return;
      }
      if (event.type === "assistant_audio") {
        if (event.session_id !== sessionRef.current?.session_id) return;
        if (event.generation_id < generationRef.current) return;
        audioGainRef.current = Math.min(1, Math.max(0, event.gain));
        const elements =
          audioContainerRef.current?.querySelectorAll("audio") || [];
        elements.forEach((element) => {
          element.volume = audioGainRef.current;
        });
        recordAudioDiagnostic(
          event.action === "duck" ? "playback_ducked" : "playback_restored",
          "ok",
          { gain: audioGainRef.current },
        );
        return;
      }
      if (event.type === "emotion_observation") {
        if (!initialReadyRef.current) return;
        if (event.session_id !== sessionRef.current?.session_id) return;
        if (event.generation_id < generationRef.current) return;
        const acceptedTurn = latestAcceptedUserTurnRef.current;
        if (event.turn_id < acceptedTurn || event.turn_id > acceptedTurn + 1) {
          return;
        }
        if (event.turn_id === acceptedTurn && acceptedTurn > 0) {
          activateEmotionHint(event);
        } else {
          pendingEmotionRef.current.set(event.turn_id, event);
        }
        return;
      }
      if (event.type === "assistant_state") {
        if (event.session_id !== sessionRef.current?.session_id) return;
        if (event.generation_id < generationRef.current) return;
        const mappedState = mapServerState(event.state);
        if (!mappedState) return;
        if (!initialReadyRef.current) {
          if (event.state !== "ready" && event.state !== "speaker_enroll") {
            return;
          }
          initialReadyRef.current = true;
          recordAudioDiagnostic("agent_ready");
        }
        clearAgentReadyTimer();
        clearReconnectTimer();
        generationRef.current = event.generation_id;
        turnRef.current = event.turn_id;
        setUiState(mappedState);
        return;
      }
      if (!initialReadyRef.current) return;
      if (event.speaker === "user" && !event.final) return;
      if (event.session_id !== sessionRef.current?.session_id) return;
      applyTranscript(event, { authoritative: true });
    };
    const onTranscriptionReceived = (segments, participant) => {
      if (!isCurrent() || !initialReadyRef.current) return;
      if (
        sessionRef.current?.interaction?.interaction_mode ===
        "self_preview"
      ) {
        return;
      }
      if (!participant?.isAgent || !segments.length) return;
      const text = segments.map((segment) => segment.text).join("");
      if (!text) return;
      applyTranscript(
        {
          speaker: "assistant",
          text,
          final: segments.every((segment) => segment.final),
          heard: true,
          turn_id: turnRef.current,
          generation_id: generationRef.current,
        },
        { authoritative: false },
      );
    };
    const onReconnecting = () => {
      if (!isCurrent()) return;
      recoveryEpochRef.current += 1;
      recoveryInFlightRef.current = false;
      roomConnectedRef.current = false;
      if (!initialReadyRef.current) {
        setUiState("connecting");
        return;
      }
      setUiState("reconnecting");
      clearReconnectTimer();
      reconnectTimerRef.current = window.setTimeout(() => {
        void failReconnect(transport.room, "重新连接超时，请轻触吉祥物再试");
      }, 10_000);
    };
    const onReconnected = () => {
      if (!isCurrent() || !created) return;
      roomConnectedRef.current = true;
      if (!initialReadyRef.current || recoveryInFlightRef.current) return;
      recoveryInFlightRef.current = true;
      const recoveryEpoch = recoveryEpochRef.current;
      const isRecoveryCurrent = () =>
        isCurrent() && recoveryEpochRef.current === recoveryEpoch;
      reconnectTimerRef.current ||= window.setTimeout(() => {
        void failReconnect(transport.room, "重新连接超时，请轻触吉祥物再试");
      }, 10_000);
      generationRef.current += 1;
      void (async () => {
        try {
          await notifyRtcRecovered(created.session_id);
          if (!isRecoveryCurrent()) return;
          let appliedMicState = micEnabledRef.current;
          await transport.setMicrophoneEnabled(appliedMicState);
          if (!isRecoveryCurrent()) return;
          if (appliedMicState !== micEnabledRef.current) {
            appliedMicState = micEnabledRef.current;
            await transport.setMicrophoneEnabled(appliedMicState);
            if (!isRecoveryCurrent()) return;
          }
          if (voiceReplyEnabledRef.current) {
            try {
              await transport.resumeAudio();
              if (!isRecoveryCurrent()) return;
            } catch {
              if (isRecoveryCurrent()) setAudioBlocked(true);
            }
          }
        } catch {
          if (isRecoveryCurrent()) {
            await failReconnect(
              transport.room,
              "重新连接失败，请轻触吉祥物再试",
            );
          }
        }
      })();
    };
    const onDisconnected = () => {
      if (!isCurrent()) return;
      attemptRef.current += 1;
      const wasIntentional = intentionalEndRef.current;
      void disconnectRoom(transport.room);
      resetEmotionState();
      if (!wasIntentional) {
        setError("连接已经断开，轻触吉祥物可以重新开始");
      }
      setSession(null);
      sessionRef.current = null;
      setUiState("closed");
    };
    transport = new LiveKitCascadeTransport({
      stopResponse,
      onTrackSubscribed,
      onTrackUnsubscribed,
      onDataReceived,
      onTranscriptionReceived,
      onReconnecting,
      onReconnected,
      onDisconnected,
    });
    cascadeTransportRef.current = transport;
    const room = transport.room;
    roomRef.current = room;
    let liveKitUnlockPromise = Promise.resolve();
    if (voiceReplyEnabledRef.current) {
      try {
        liveKitUnlockPromise = transport
          .prepare({ unlockAudio: true })
          .then(() => {
            if (isCurrent()) {
              recordAudioDiagnostic("audio_unlock", "ok", {
                can_playback_audio: room.canPlaybackAudio ?? null,
              });
            }
          })
          .catch((caught) => {
            if (isCurrent()) {
              setAudioBlocked(true);
              recordAudioDiagnostic("audio_unlock", "error", {
                error: caught?.name || "unknown",
              });
            }
          });
      } catch {
        setAudioBlocked(true);
      }
    }
    return (async () => {
      try {
        created = await (sessionOptions
          ? createSession(
              userId,
              selectedBackend,
              selectedLearningTaskId,
              sessionOptions,
            )
          : createSession(userId, selectedBackend, selectedLearningTaskId));
        if (!isCurrent()) {
          await disconnectRoom(room);
          return;
        }
        sessionRef.current = created;
        setSession(created);
        recordAudioDiagnostic("session_created");
        await transport.connect(created, {
          getMicrophoneEnabled: () => micEnabledRef.current,
          isCurrent,
        });
        if (!isCurrent()) {
          await disconnectRoom(room);
          return;
        }
        roomConnectedRef.current = true;
        for (const diagnostic of audioDiagnosticsRef.current) {
          publishAudioDiagnostic(room, {
            ...diagnostic,
            session_id: created.session_id,
          });
        }
        recordAudioDiagnostic("room_connected");
        if (!initialReadyRef.current) {
          clearAgentReadyTimer();
          agentReadyTimerRef.current = window.setTimeout(() => {
            void failReconnect(room, "连接超时，请轻触吉祥物再试");
          }, AGENT_READY_TIMEOUT_MS);
        }
        await liveKitUnlockPromise;
        if (!isCurrent()) {
          await disconnectRoom(room);
          return;
        }
        if (voiceReplyEnabledRef.current) {
          try {
            await transport.resumeAudio();
            if (isCurrent()) {
              setAudioBlocked(false);
              recordAudioDiagnostic("audio_unlock", "ok", {
                can_playback_audio: room.canPlaybackAudio ?? null,
              });
            }
          } catch (caught) {
            if (isCurrent()) {
              setAudioBlocked(true);
              recordAudioDiagnostic("audio_unlock", "error", {
                error: caught?.name || "unknown",
              });
            }
          }
        }
        return created;
      } catch (caught) {
        const current = isCurrent();
        if (current) {
          attemptRef.current += 1;
          sessionRef.current = null;
          resetEmotionState();
          setSession(null);
          const denied =
            caught instanceof DOMException && caught.name === "NotAllowedError";
          setError(
            denied
              ? "需要麦克风权限，才能听见你说话"
              : caught instanceof Error
                ? caught.message
                : "暂时无法开始对话",
          );
          setUiState("closed");
        }
        await disconnectRoom(room);
        return null;
      }
    })();
  }, [
    applyTranscript,
    activateEmotionHint,
    attachAudio,
    attachOmniAudio,
    clearAgentReadyTimer,
    clearReconnectTimer,
    disconnectOmni,
    disconnectRoom,
    failOmni,
    failReconnect,
    interactionMode,
    legacyGrantId,
    learningTaskId,
    previewGrantId,
    publishAudioDiagnostic,
    recordAudioDiagnostic,
    resetEmotionState,
    startStatsSampling,
    stopStatsSampling,
    userId,
    voiceBackend,
  ]);

  const toggleMic = useCallback(async () => {
    const cascadeTransport = cascadeTransportRef.current;
    const omniTransport = omniTransportRef.current;
    if (!cascadeTransport && !omniTransport) return;
    const next = !micEnabledRef.current;
    micEnabledRef.current = next;
    setMicEnabledState(next);
    if (omniTransport) {
      try {
        await omniTransport.setMicrophoneEnabled(next);
      } catch {
        if (omniTransportRef.current === omniTransport) {
          micEnabledRef.current = !next;
          setMicEnabledState(!next);
          setError("麦克风切换失败");
        }
      }
      return;
    }
    if (!roomConnectedRef.current) return;
    try {
      await cascadeTransport.setMicrophoneEnabled(next);
    } catch {
      if (cascadeTransportRef.current === cascadeTransport) {
        micEnabledRef.current = !next;
        setMicEnabledState(!next);
        setError("麦克风切换失败");
      }
    }
  }, []);

  const stopAssistant = useCallback(async () => {
    const cascadeTransport = cascadeTransportRef.current;
    const omniTransport = omniTransportRef.current;
    const sessionId = sessionRef.current?.session_id;
    if ((!cascadeTransport && !omniTransport) || !sessionId) return;
    try {
      if (omniTransport) {
        omniTransport.stopAssistant();
        return;
      }
      await cascadeTransport.stopAssistant();
    } catch {
      if (
        (omniTransport
          ? omniTransportRef.current === omniTransport
          : cascadeTransportRef.current === cascadeTransport) &&
        sessionRef.current?.session_id === sessionId
      ) {
        setError("暂时无法停止回答");
      }
    }
  }, []);

  const end = useCallback(async () => {
    attemptRef.current += 1;
    intentionalEndRef.current = true;
    const room = roomRef.current;
    const omniTransport = omniTransportRef.current;
    sessionRef.current = null;
    resetEmotionState();
    setSession(null);
    setAudioBlocked(false);
    setUiState("closed");
    disconnectOmni(omniTransport);
    await disconnectRoom(room);
  }, [disconnectOmni, disconnectRoom, resetEmotionState]);

  const reset = useCallback(async () => {
    attemptRef.current += 1;
    intentionalEndRef.current = true;
    const room = roomRef.current;
    const omniTransport = omniTransportRef.current;
    sessionRef.current = null;
    resetEmotionState();
    generationRef.current = 0;
    turnRef.current = 0;
    micEnabledRef.current = true;
    persistedRef.current.clear();
    audioDiagnosticsRef.current = [];
    setSession(null);
    resetUiState(attemptRef.current);
    setMicEnabledState(true);
    setTranscripts([]);
    setError("");
    setAudioBlocked(false);
    setAudioDiagnostics([]);
    disconnectOmni(omniTransport);
    await disconnectRoom(room);
  }, [disconnectOmni, disconnectRoom, resetEmotionState, resetUiState]);

  useEffect(
    () => () => {
      attemptRef.current += 1;
      intentionalEndRef.current = true;
      if (emotionTimerRef.current !== null) {
        window.clearTimeout(emotionTimerRef.current);
      }
      disconnectOmni(omniTransportRef.current);
      void disconnectRoom(roomRef.current);
    },
    [disconnectOmni, disconnectRoom],
  );

  return {
    session,
    uiState,
    statusLabel: stateLabels[uiState] || stateLabels.ready,
    micEnabled,
    transcripts,
    latestTranscript: transcripts.at(-1) || null,
    error,
    audioBlocked,
    audioDiagnostics,
    emotionHint,
    audioContainerRef,
    start,
    resumeAudio,
    toggleMic,
    stopAssistant,
    end,
    reset,
  };
}
