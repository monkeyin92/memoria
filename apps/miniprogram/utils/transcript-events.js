function authoritativeTranscript(event) {
  if (event?.type !== "ui_event") return null;
  const payload = event.event || {};
  if (payload.type !== "transcript_delta") return null;
  if (payload.speaker !== "user" && payload.speaker !== "assistant") return null;
  if (typeof payload.text !== "string" || !payload.text) return null;
  if (payload.speaker === "user" && payload.final === false) return null;
  return {
    speaker: payload.speaker,
    text: payload.text,
    final: payload.final !== false,
    turnId: payload.turn_id,
    generationId: payload.generation_id,
    turnRevision:
      Number.isInteger(payload.turn_revision) && payload.turn_revision >= 1
      ? payload.turn_revision
      : null,
    source: "authoritative",
  };
}

function assistantStreamingTranscript(event) {
  if (
    event?.type !== "transcription" ||
    !Number.isInteger(event.turn_id) ||
    event.turn_id < 0 ||
    !Number.isInteger(event.generation_id) ||
    event.generation_id < 0 ||
    !Array.isArray(event.segments)
  ) {
    return null;
  }
  const segments = event.segments.filter(
    (segment) => typeof segment?.text === "string" && segment.text,
  );
  if (!segments.length) return null;
  return {
    speaker: "assistant",
    text: segments.map((segment) => segment.text).join(""),
    final: segments.every((segment) => segment.final === true),
    turnId: event.turn_id,
    generationId: event.generation_id,
    source: "display",
  };
}

function acceptTranscriptRevision(latestByTurn, transcript) {
  if (!(latestByTurn instanceof Map)) return true;
  const key = `${transcript.speaker}:${transcript.turnId}:${transcript.generationId}`;
  const latest = latestByTurn.get(key);
  if (
    !Number.isInteger(transcript.turnRevision) ||
    transcript.turnRevision < 1
  ) {
    return latest === undefined;
  }
  if (latest !== undefined && transcript.turnRevision <= latest) return false;
  latestByTurn.set(key, transcript.turnRevision);
  return true;
}

module.exports = {
  acceptTranscriptRevision,
  assistantStreamingTranscript,
  authoritativeTranscript,
};
