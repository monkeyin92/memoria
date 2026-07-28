const assert = require("node:assert/strict");
const test = require("node:test");

const {
  acceptTranscriptRevision,
  authoritativeTranscript,
} = require("../utils/transcript-events");

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
        turn_revision: 3,
      },
    }),
    {
      speaker: "user",
      text: "这是我真正说的话",
      final: true,
      turnId: 2,
      generationId: 2,
      turnRevision: 3,
      source: "authoritative",
    },
  );
});

test("conversation UI rejects a late transcript revision within one turn", () => {
  const latest = new Map();
  const current = {
    speaker: "assistant",
    turnId: 2,
    generationId: 2,
    turnRevision: 4,
  };
  assert.equal(acceptTranscriptRevision(latest, current), true);
  assert.equal(
    acceptTranscriptRevision(latest, { ...current, turnRevision: 3 }),
    false,
  );
});

test("legacy transcript events can still revise until a numbered revision arrives", () => {
  const latest = new Map();
  const legacy = {
    speaker: "assistant",
    turnId: 2,
    generationId: 2,
    turnRevision: 0,
  };
  assert.equal(acceptTranscriptRevision(latest, legacy), true);
  assert.equal(acceptTranscriptRevision(latest, legacy), true);
  assert.equal(
    acceptTranscriptRevision(latest, { ...legacy, turnRevision: 1 }),
    true,
  );
  assert.equal(acceptTranscriptRevision(latest, legacy), false);
});
