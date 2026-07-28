const allowedTransitions = {
  idle: new Set(["connecting", "closed"]),
  connecting: new Set([
    "ready",
    "speaker_enroll",
    "listening",
    "reconnecting",
    "closed",
  ]),
  ready: new Set([
    "speaker_enroll",
    "listening",
    "thinking",
    "speaking",
    "interrupted",
    "reconnecting",
    "closed",
  ]),
  speaker_enroll: new Set([
    "listening",
    "thinking",
    "speaking",
    "interrupted",
    "reconnecting",
    "closed",
  ]),
  listening: new Set([
    "ready",
    "speaker_enroll",
    "thinking",
    "speaking",
    "interrupted",
    "reconnecting",
    "closed",
  ]),
  thinking: new Set([
    "listening",
    "speaking",
    "interrupted",
    "reconnecting",
    "closed",
  ]),
  speaking: new Set([
    "listening",
    "thinking",
    "interrupted",
    "reconnecting",
    "closed",
  ]),
  interrupted: new Set([
    "listening",
    "thinking",
    "speaking",
    "reconnecting",
    "closed",
  ]),
  reconnecting: new Set([
    "connecting",
    "ready",
    "speaker_enroll",
    "listening",
    "thinking",
    "speaking",
    "interrupted",
    "closed",
  ]),
  closed: new Set(["connecting", "idle"]),
};

export const initialVoiceSessionState = Object.freeze({
  status: "idle",
  attempt: 0,
  generation: 0,
});

export function voiceSessionReducer(state, event) {
  if (event?.type === "reset") {
    if (!Number.isInteger(event.attempt) || event.attempt < state.attempt) {
      return state;
    }
    return {
      status: "idle",
      attempt: event.attempt,
      generation: 0,
    };
  }
  if (
    event?.type !== "transition" ||
    !Number.isInteger(event.attempt) ||
    !Number.isInteger(event.generation) ||
    event.attempt < state.attempt ||
    (event.attempt === state.attempt && event.generation < state.generation) ||
    !allowedTransitions[event.status]
  ) {
    return state;
  }
  if (
    event.status !== state.status &&
    !allowedTransitions[state.status]?.has(event.status)
  ) {
    return state;
  }
  return {
    status: event.status,
    attempt: event.attempt,
    generation: event.generation,
  };
}
