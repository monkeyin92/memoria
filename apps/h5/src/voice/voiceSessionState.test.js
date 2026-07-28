import { describe, expect, it } from "vitest";

import {
  initialVoiceSessionState,
  voiceSessionReducer,
} from "./voiceSessionState.js";

describe("voiceSessionReducer", () => {
  it("moves through explicit lifecycle transitions", () => {
    const connecting = voiceSessionReducer(initialVoiceSessionState, {
      type: "transition",
      status: "connecting",
      attempt: 1,
      generation: 0,
    });
    const listening = voiceSessionReducer(connecting, {
      type: "transition",
      status: "listening",
      attempt: 1,
      generation: 1,
    });
    const speaking = voiceSessionReducer(listening, {
      type: "transition",
      status: "speaking",
      attempt: 1,
      generation: 1,
    });

    expect([connecting.status, listening.status, speaking.status]).toEqual([
      "connecting",
      "listening",
      "speaking",
    ]);
  });

  it("drops stale attempt and generation transitions", () => {
    const current = {
      status: "speaking",
      attempt: 3,
      generation: 7,
    };

    expect(
      voiceSessionReducer(current, {
        type: "transition",
        status: "closed",
        attempt: 2,
        generation: 99,
      }),
    ).toBe(current);
    expect(
      voiceSessionReducer(current, {
        type: "transition",
        status: "listening",
        attempt: 3,
        generation: 6,
      }),
    ).toBe(current);
  });

  it("rejects illegal jumps but allows reset to idle", () => {
    const connecting = {
      status: "connecting",
      attempt: 1,
      generation: 0,
    };
    expect(
      voiceSessionReducer(connecting, {
        type: "transition",
        status: "speaking",
        attempt: 1,
        generation: 0,
      }),
    ).toBe(connecting);
    expect(
      voiceSessionReducer(connecting, {
        type: "reset",
        attempt: 2,
      }),
    ).toEqual({
      status: "idle",
      attempt: 2,
      generation: 0,
    });
  });
});
