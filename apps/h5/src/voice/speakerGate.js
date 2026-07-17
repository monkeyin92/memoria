import {
  cosineSimilarity,
  downsampleTo16k,
  embedPcm,
  float32ToPcm16le,
  speechMsFromPcm,
} from "./speakerEmbed.js";

const DEFAULT_THRESHOLD = 0.62;
const ENROLL_SPEECH_MS = 3500;
const ENROLL_TIMEOUT_MS = 15000;
const VERIFY_WINDOW_MS = 1500;
const MIN_VERIFY_SPEECH_MS = 450;

/**
 * Wrap a microphone MediaStream with enrollment + target-speaker gating.
 * Non-matching speech is silenced so LiveKit/Omni never "hear" a bystander.
 */
export function createSpeakerGatedStream(
  sourceStream,
  {
    enabled = true,
    acceptThreshold = DEFAULT_THRESHOLD,
    enrollSpeechMs = ENROLL_SPEECH_MS,
    enrollTimeoutMs = ENROLL_TIMEOUT_MS,
    onProgress = () => undefined,
    onEnrolled = () => undefined,
    onReject = () => undefined,
  } = {},
) {
  if (!enabled || typeof AudioContext === "undefined") {
    return {
      stream: sourceStream,
      state: () => (enabled ? "open" : "disabled"),
      embedding: () => null,
      close: () => undefined,
      forceOpen: () => undefined,
    };
  }

  const audioCtx = new AudioContext();
  const source = audioCtx.createMediaStreamSource(sourceStream);
  // ScriptProcessor is deprecated but widely available; 4096 ~ 85ms @48k.
  const processor = audioCtx.createScriptProcessor(4096, 1, 1);
  const silence = audioCtx.createGain();
  silence.gain.value = 0;
  const destination = audioCtx.createMediaStreamDestination();

  let ownerEmbedding = null;
  let state = "pending";
  let enrollSpeechMsAccum = 0;
  let enrollElapsedMs = 0;
  let enrollPcm = new Uint8Array(0);
  let rolling = new Float32Array(0);
  let mutedForMismatchUntil = 0;
  let closed = false;

  const appendBytes = (a, b) => {
    const out = new Uint8Array(a.length + b.length);
    out.set(a, 0);
    out.set(b, a.length);
    return out;
  };

  const appendFloat = (a, b) => {
    const out = new Float32Array(a.length + b.length);
    out.set(a, 0);
    out.set(b, a.length);
    return out;
  };

  const maxRolling = Math.floor(audioCtx.sampleRate * 4);

  processor.onaudioprocess = (event) => {
    if (closed) return;
    const input = event.inputBuffer.getChannelData(0);
    const output = event.outputBuffer.getChannelData(0);
    const chunk = new Float32Array(input.length);
    chunk.set(input);

    const down = downsampleTo16k(chunk, audioCtx.sampleRate);
    const pcm = float32ToPcm16le(down);
    const chunkMs = Math.max(1, Math.round((chunk.length * 1000) / audioCtx.sampleRate));

    rolling = appendFloat(rolling, down);
    if (rolling.length > maxRolling) {
      rolling = rolling.subarray(rolling.length - maxRolling);
    }

    if (state === "pending") {
      enrollElapsedMs += chunkMs;
      enrollPcm = appendBytes(enrollPcm, pcm);
      enrollSpeechMsAccum = speechMsFromPcm(enrollPcm);
      onProgress({
        state,
        speechMs: enrollSpeechMsAccum,
        targetMs: enrollSpeechMs,
        elapsedMs: enrollElapsedMs,
      });
      // Pass through during enrollment so cascade Agent can also hear.
      output.set(input);
      if (enrollSpeechMsAccum >= enrollSpeechMs) {
        const emb = embedPcm(enrollPcm, { minSpeechMs: Math.floor(enrollSpeechMs / 2) });
        if (emb) {
          ownerEmbedding = emb;
          state = "enrolled";
          onEnrolled({ reason: "enrolled", speechMs: enrollSpeechMsAccum });
        } else if (enrollElapsedMs >= enrollTimeoutMs) {
          state = "open";
          onEnrolled({ reason: "enroll_embedding_failed", speechMs: enrollSpeechMsAccum });
        }
      } else if (enrollElapsedMs >= enrollTimeoutMs) {
        state = "open";
        onEnrolled({ reason: "enroll_timeout", speechMs: enrollSpeechMsAccum });
      }
      return;
    }

    if (state !== "enrolled" || !ownerEmbedding) {
      output.set(input);
      return;
    }

    const now = performance.now();
    if (now < mutedForMismatchUntil) {
      output.fill(0);
      return;
    }

    // Score last VERIFY_WINDOW_MS of 16 kHz audio.
    const windowSamples = Math.floor((16000 * VERIFY_WINDOW_MS) / 1000);
    const slice =
      rolling.length > windowSamples
        ? rolling.subarray(rolling.length - windowSamples)
        : rolling;
    const windowPcm = float32ToPcm16le(slice);
    const speechMs = speechMsFromPcm(windowPcm);
    if (speechMs < MIN_VERIFY_SPEECH_MS) {
      output.set(input);
      return;
    }
    const emb = embedPcm(windowPcm, { minSpeechMs: MIN_VERIFY_SPEECH_MS });
    if (!emb) {
      output.set(input);
      return;
    }
    const score = cosineSimilarity(ownerEmbedding, emb);
    if (score < acceptThreshold) {
      mutedForMismatchUntil = now + 450;
      output.fill(0);
      onReject({ score, speechMs, reason: "mismatch" });
      return;
    }
    output.set(input);
  };

  source.connect(processor);
  processor.connect(destination);
  // Keep the graph alive without audible local loopback.
  processor.connect(silence);
  silence.connect(audioCtx.destination);

  // Prefer gated track; fall back to original if empty.
  const gated = destination.stream;
  if (!gated.getAudioTracks().length) {
    return {
      stream: sourceStream,
      state: () => state,
      embedding: () => ownerEmbedding,
      close: () => {
        closed = true;
        void audioCtx.close();
      },
      forceOpen: () => {
        state = "open";
      },
    };
  }

  return {
    stream: gated,
    state: () => state,
    embedding: () => ownerEmbedding,
    close: () => {
      closed = true;
      try {
        processor.disconnect();
        source.disconnect();
        silence.disconnect();
      } catch {
        // ignore
      }
      void audioCtx.close();
    },
    forceOpen: () => {
      state = "open";
    },
  };
}
