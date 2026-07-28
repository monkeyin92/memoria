const requiredMethods = [
  "prepare",
  "connect",
  "setMicrophoneEnabled",
  "stopAssistant",
  "close",
];

export class VoiceTransport {
  prepare() {
    return Promise.resolve();
  }

  async connect(_session) {
    throw new Error("VoiceTransport.connect must be implemented");
  }

  async setMicrophoneEnabled(_enabled) {
    throw new Error("VoiceTransport.setMicrophoneEnabled must be implemented");
  }

  stopAssistant() {
    throw new Error("VoiceTransport.stopAssistant must be implemented");
  }

  close() {
    throw new Error("VoiceTransport.close must be implemented");
  }
}

export function assertVoiceTransport(transport) {
  for (const method of requiredMethods) {
    if (typeof transport?.[method] !== "function") {
      throw new TypeError(`VoiceTransport method is missing: ${method}`);
    }
  }
  return transport;
}
