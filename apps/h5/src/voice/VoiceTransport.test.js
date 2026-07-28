import { describe, expect, it, vi } from "vitest";

import { QwenOmniWebRTCTransport } from "./experimental/QwenOmniWebRTCTransport.js";
import { VoiceTransport, assertVoiceTransport } from "./VoiceTransport.js";

describe("VoiceTransport", () => {
  it("is the shared lifecycle contract for the Omni transport", () => {
    const transport = new QwenOmniWebRTCTransport({
      exchangeSdp: vi.fn(),
    });

    expect(transport).toBeInstanceOf(VoiceTransport);
    expect(assertVoiceTransport(transport)).toBe(transport);
    expect(typeof transport.prepare).toBe("function");
    expect(typeof transport.connect).toBe("function");
    expect(typeof transport.setMicrophoneEnabled).toBe("function");
    expect(typeof transport.stopAssistant).toBe("function");
    expect(typeof transport.close).toBe("function");
  });

  it("rejects incomplete transport implementations", () => {
    expect(() => assertVoiceTransport({ connect() {} })).toThrow(
      /VoiceTransport method/,
    );
  });
});
