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
    source: "authoritative",
  };
}

module.exports = {
  authoritativeTranscript,
};
