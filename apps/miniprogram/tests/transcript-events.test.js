const assert = require("node:assert/strict");
const test = require("node:test");

const { authoritativeTranscript } = require("../utils/transcript-events");

test("conversation UI ignores raw gateway transcription and unconfirmed user interim text", () => {
  assert.equal(
    authoritativeTranscript({
      type: "transcription",
      segments: [{ text: "助手回声", final: true }],
    }),
    null,
  );
  assert.equal(
    authoritativeTranscript({
      type: "ui_event",
      event: {
        type: "transcript_delta",
        speaker: "user",
        text: "助手回声",
        final: false,
      },
    }),
    null,
  );
});

test("conversation UI accepts only Agent-authoritative final user text", () => {
  assert.deepEqual(
    authoritativeTranscript({
      type: "ui_event",
      event: {
        type: "transcript_delta",
        speaker: "user",
        text: "这是我真正说的话",
        final: true,
        turn_id: 2,
        generation_id: 2,
      },
    }),
    {
      speaker: "user",
      text: "这是我真正说的话",
      final: true,
      turnId: 2,
      generationId: 2,
      source: "authoritative",
    },
  );
});
