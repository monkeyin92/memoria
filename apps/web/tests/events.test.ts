import { describe, expect, it } from "vitest";

import {
  parseLiveKitDataEvent,
  shouldAcceptGeneration,
} from "../src/types/events";

const encode = (value: unknown) =>
  new TextEncoder().encode(JSON.stringify(value));

describe("LiveKit data events", () => {
  it("parses assistant state and transcript events", () => {
    expect(
      parseLiveKitDataEvent(
        encode({
          type: "assistant_state",
          session_id: "s",
          state: "speaking",
          phase: "speaking",
          turn_id: 2,
          generation_id: 3,
          at: "2026-07-15T00:00:00Z",
        }),
      ),
    ).toMatchObject({ type: "assistant_state", generation_id: 3 });

    expect(
      parseLiveKitDataEvent(
        encode({
          type: "transcript_delta",
          session_id: "s",
          speaker: "assistant",
          text: "你好",
          final: false,
          heard: true,
          history_eligible: false,
          turn_id: 2,
          generation_id: 3,
          tool_epoch: 0,
        }),
      ),
    ).toMatchObject({ type: "transcript_delta", text: "你好" });
  });

  it("rejects malformed JSON and schema violations", () => {
    expect(parseLiveKitDataEvent(new TextEncoder().encode("{"))).toBeNull();
    expect(
      parseLiveKitDataEvent(
        encode({ type: "transcript_delta", text: "missing fields" }),
      ),
    ).toBeNull();
    expect(
      parseLiveKitDataEvent(
        encode({
          type: "transcript_delta",
          session_id: "s",
          speaker: "user",
          text: "bad generation",
          final: true,
          turn_id: 1,
          generation_id: 1.5,
          unexpected: true,
        }),
      ),
    ).toBeNull();
  });

  it("accepts only equal or newer generations", () => {
    expect(shouldAcceptGeneration(3, 3)).toBe(true);
    expect(shouldAcceptGeneration(3, 4)).toBe(true);
    expect(shouldAcceptGeneration(3, 2)).toBe(false);
  });
});
