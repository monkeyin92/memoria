const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const contract = JSON.parse(
  fs.readFileSync(
    path.join(__dirname, "../../../packages/contracts/miniprogram-media.json"),
    "utf8",
  ),
);

const recorder = {
  startCalls: [],
  stopCalls: 0,
  pauseCalls: 0,
  resumeCalls: 0,
  frameListener: null,
  startListener: null,
  errorListener: null,
  interruptionListener: null,
  interruptionEndListener: null,
  onFrameRecorded(listener) {
    this.frameListener = listener;
  },
  onStart(listener) {
    this.startListener = listener;
  },
  onError(listener) {
    this.errorListener = listener;
  },
  onInterruptionBegin(listener) {
    this.interruptionListener = listener;
  },
  onInterruptionEnd(listener) {
    this.interruptionEndListener = listener;
  },
  start(options) {
    this.startCalls.push(options);
  },
  stop() {
    this.stopCalls += 1;
  },
  pause() {
    this.pauseCalls += 1;
  },
  resume() {
    this.resumeCalls += 1;
  },
  reset() {
    this.startCalls = [];
    this.stopCalls = 0;
    this.pauseCalls = 0;
    this.resumeCalls = 0;
    this.frameListener = null;
    this.startListener = null;
    this.errorListener = null;
    this.interruptionListener = null;
    this.interruptionEndListener = null;
  },
};

global.wx = {
  getRecorderManager: () => recorder,
  createWebAudioContext: () => {
    const gain = { value: 1 };
    return {
      state: "running",
      currentTime: 0,
      destination: {},
      createGain: () => ({
        gain,
        connect() {},
      }),
      resume: async () => {},
      close: async () => {},
    };
  },
};

const { MiniProgramMediaSession } = require("../utils/media-gateway");
const { FRAME_TYPE, encodePcmFrame } = require("../utils/media-protocol");
const { PcmJitterPlayer } = require("../utils/pcm-player");

test("gateway header handshake keeps a legacy hello fallback until acknowledged", async () => {
  recorder.reset();
  const sent = [];
  let connectOptions = null;
  const socket = {
    onOpen(listener) {
      this.openListener = listener;
    },
    onMessage(listener) {
      this.messageListener = listener;
    },
    onError(listener) {
      this.errorListener = listener;
    },
    onClose(listener) {
      this.closeListener = listener;
    },
    send(options) {
      sent.push(options.data);
    },
    close() {},
  };
  global.wx.connectSocket = (options) => {
    connectOptions = options;
    return socket;
  };
  const media = new MiniProgramMediaSession(
    {
      media_gateway: {
        websocket_url: "wss://voice.example.com/media",
        ticket: "ticket",
        audio: {
          sample_rate: contract.audio.downlink_sample_rate,
          channels: contract.audio.channels,
          sample_format: contract.audio.sample_format,
          frame_ms: contract.audio.frame_ms,
        },
      },
    },
    {},
  );

  const connecting = media.connect();
  await new Promise((resolve) => setImmediate(resolve));
  const handshake = connectOptions.header;
  assert.deepEqual(
    handshake,
    {
      [contract.header_handshake.protocol_header]: contract.header_handshake.protocol_version,
      [contract.header_handshake.ticket_header]: "ticket",
      [contract.header_handshake.downlink_generation_header]:
        contract.header_handshake.downlink_generation_value,
    },
  );
  assert.equal(connectOptions.url.includes("ticket"), false);
  socket.openListener();
  assert.deepEqual(JSON.parse(sent[0]), {
    type: contract.hello.type,
    protocol_version: contract.hello.protocol_version,
    ticket: "ticket",
    capabilities: contract.hello.capabilities,
  });

  const acknowledgement = {
    type: contract.handshake_ack.type,
    protocol_version: contract.handshake_ack.protocol_version,
    transport: "header",
  };
  assert.deepEqual(
    Object.keys(acknowledgement).sort(),
    [...contract.handshake_ack.required_fields].sort(),
  );
  socket.messageListener({ data: JSON.stringify(acknowledgement) });
  assert.equal(media.ready, false);

  const ready = {
    type: contract.ready.type,
    protocol_version: contract.ready.protocol_version,
    session_id: "session-1",
    audio: {
      sample_rate: contract.audio.downlink_sample_rate,
      channels: contract.audio.channels,
      sample_format: contract.audio.sample_format,
      frame_ms: contract.audio.frame_ms,
      frame_protocol_version: contract.audio.downlink_frame_protocol_versions[1],
    },
  };
  assert.deepEqual(
    Object.keys(ready).sort(),
    [...contract.ready.required_fields].sort(),
  );
  assert.deepEqual(
    Object.keys(ready.audio).sort(),
    [...contract.ready.audio_required_fields].sort(),
  );
  socket.messageListener({ data: JSON.stringify(ready) });

  await connecting;
  await media.close();
});

test("gateway acknowledgement fences generic SocketTask errors while bridge becomes ready", async () => {
  recorder.reset();
  const socket = {
    onMessage(listener) {
      this.messageListener = listener;
    },
    onError(listener) {
      this.errorListener = listener;
    },
    onClose(listener) {
      this.closeListener = listener;
    },
    close() {},
  };
  global.wx.connectSocket = () => socket;
  const media = new MiniProgramMediaSession(
    {
      media_gateway: {
        websocket_url: "wss://voice.example.com/media",
        ticket: "ticket",
        audio: {
          sample_rate: contract.audio.downlink_sample_rate,
          channels: contract.audio.channels,
          sample_format: contract.audio.sample_format,
          frame_ms: contract.audio.frame_ms,
        },
      },
    },
    {},
  );

  const connecting = media.connect();
  const connectionResult = connecting.then(
    () => null,
    (error) => error,
  );
  await new Promise((resolve) => setImmediate(resolve));
  socket.errorListener({ errMsg: "connectSocket:fail connection refused" });
  socket.messageListener({
    data: JSON.stringify({
      type: contract.handshake_ack.type,
      protocol_version: contract.handshake_ack.protocol_version,
      transport: "header",
    }),
  });
  socket.errorListener({ errMsg: "connectSocket:fail connection refused" });
  await new Promise((resolve) => setTimeout(resolve, 130));
  socket.messageListener({
    data: JSON.stringify({
      type: contract.ready.type,
      protocol_version: contract.ready.protocol_version,
      session_id: "session-1",
      audio: {
        sample_rate: contract.audio.downlink_sample_rate,
        channels: contract.audio.channels,
        sample_format: contract.audio.sample_format,
        frame_ms: contract.audio.frame_ms,
        frame_protocol_version: contract.audio.downlink_frame_protocol_versions[1],
      },
    }),
  });

  assert.equal(await connectionResult, null);
  assert.equal(media.ready, true);
  await media.close();
});

test("gateway acknowledgement has a bounded ready wait and fences a late ready", async () => {
  recorder.reset();
  let closeCalls = 0;
  const socket = {
    onMessage(listener) {
      this.messageListener = listener;
    },
    onError(listener) {
      this.errorListener = listener;
    },
    onClose(listener) {
      this.closeListener = listener;
    },
    close() {
      closeCalls += 1;
    },
  };
  global.wx.connectSocket = () => socket;
  const media = new MiniProgramMediaSession(
    {
      media_gateway: {
        websocket_url: "wss://voice.example.com/media",
        ticket: "ticket",
        audio: {
          sample_rate: contract.audio.downlink_sample_rate,
          channels: contract.audio.channels,
          sample_format: contract.audio.sample_format,
          frame_ms: contract.audio.frame_ms,
        },
      },
    },
    {},
  );

  const connecting = media.connect();
  const connectionResult = connecting.then(
    () => null,
    (error) => error,
  );
  await new Promise((resolve) => setImmediate(resolve));
  socket.messageListener({
    data: JSON.stringify({
      type: contract.handshake_ack.type,
      protocol_version: contract.handshake_ack.protocol_version,
      transport: "header",
    }),
  });
  assert.notEqual(media._gatewayReadyTimer, null);
  media._rejectGatewayReadyTimeout();

  const error = await connectionResult;
  assert.equal(error?.code, "gateway_ready_timeout");
  assert.equal(closeCalls, 1);
  socket.messageListener({
    data: JSON.stringify({
      type: contract.ready.type,
      protocol_version: contract.ready.protocol_version,
      session_id: "session-1",
      audio: {
        sample_rate: contract.audio.downlink_sample_rate,
        channels: contract.audio.channels,
        sample_format: contract.audio.sample_format,
        frame_ms: contract.audio.frame_ms,
        frame_protocol_version: contract.audio.downlink_frame_protocol_versions[1],
      },
    }),
  });
  assert.equal(media.ready, false);
  assert.equal(recorder.startCalls.length, 0);
  await media.close();
});

test("gateway close exposes ticket rejection instead of a generic network close", async () => {
  recorder.reset();
  const socket = {
    onMessage(listener) {
      this.messageListener = listener;
    },
    onError(listener) {
      this.errorListener = listener;
    },
    onClose(listener) {
      this.closeListener = listener;
    },
    close() {},
  };
  global.wx.connectSocket = () => socket;
  const media = new MiniProgramMediaSession(
    {
      media_gateway: {
        websocket_url: "wss://voice.example.com/media",
        ticket: "ticket",
      },
    },
    {},
  );

  const connecting = media.connect();
  await new Promise((resolve) => setImmediate(resolve));
  socket.closeListener({ code: 4401 });

  await assert.rejects(
    connecting,
    (error) =>
      error?.code === "gateway_ticket_rejected" && /凭据已失效/.test(error.message),
  );
  await media.close();
});

test("gateway close code wins when SocketTask error fires first", async () => {
  recorder.reset();
  const socket = {
    onMessage(listener) {
      this.messageListener = listener;
    },
    onError(listener) {
      this.errorListener = listener;
    },
    onClose(listener) {
      this.closeListener = listener;
    },
    close() {},
  };
  global.wx.connectSocket = () => socket;
  const media = new MiniProgramMediaSession(
    { media_gateway: { websocket_url: "wss://voice.example.com/media", ticket: "ticket" } },
    {},
  );

  const connecting = media.connect();
  await new Promise((resolve) => setImmediate(resolve));
  socket.errorListener({ errMsg: "connectSocket:fail connection refused" });
  socket.closeListener({ code: 4401 });

  await assert.rejects(
    connecting,
    (error) =>
      error?.code === "gateway_ticket_rejected" && /凭据已失效/.test(error.message),
  );
  await media.close();
});

test("a generic SocketTask close preserves the prior actionable connection error", async () => {
  recorder.reset();
  const socket = {
    onMessage(listener) {
      this.messageListener = listener;
    },
    onError(listener) {
      this.errorListener = listener;
    },
    onClose(listener) {
      this.closeListener = listener;
    },
    close() {},
  };
  global.wx.connectSocket = () => socket;
  const media = new MiniProgramMediaSession(
    { media_gateway: { websocket_url: "wss://voice.example.com/media", ticket: "ticket" } },
    {},
  );

  const connecting = media.connect();
  await new Promise((resolve) => setImmediate(resolve));
  socket.errorListener({ errMsg: "connectSocket:fail connection refused" });
  socket.closeListener({ code: 1006 });

  await assert.rejects(
    connecting,
    (error) => error?.code === "socket_connection_refused",
  );
  await media.close();
});

test("a late SocketTask onOpen does not send hello after header-authenticated ready", async () => {
  recorder.reset();
  const sent = [];
  const socket = {
    onOpen(listener) {
      this.openListener = listener;
    },
    onMessage(listener) {
      this.messageListener = listener;
    },
    onError(listener) {
      this.errorListener = listener;
    },
    onClose(listener) {
      this.closeListener = listener;
    },
    send(options) {
      sent.push(options.data);
    },
    close() {},
  };
  global.wx.connectSocket = () => socket;
  const media = new MiniProgramMediaSession(
    {
      media_gateway: {
        websocket_url: "wss://voice.example.com/media",
        ticket: "ticket",
        audio: {
          sample_rate: contract.audio.downlink_sample_rate,
          channels: contract.audio.channels,
          sample_format: contract.audio.sample_format,
          frame_ms: contract.audio.frame_ms,
        },
      },
    },
    {},
  );

  const connecting = media.connect();
  await new Promise((resolve) => setImmediate(resolve));
  socket.messageListener({
    data: JSON.stringify({
      type: contract.ready.type,
      protocol_version: contract.ready.protocol_version,
      session_id: "session-1",
      audio: {
        sample_rate: contract.audio.downlink_sample_rate,
        channels: contract.audio.channels,
        sample_format: contract.audio.sample_format,
        frame_ms: contract.audio.frame_ms,
        frame_protocol_version: contract.audio.downlink_frame_protocol_versions[1],
      },
    }),
  });
  await connecting;
  socket.openListener();

  assert.deepEqual(sent, []);
  await media.close();
});

test("microphone state waits for RecorderManager.onStart before sending PCM", async () => {
  recorder.reset();
  const errors = [];
  const sent = [];
  const media = new MiniProgramMediaSession(
    { media_gateway: { audio: { sample_rate: 24000 } } },
    { onError: (message) => errors.push(message) },
  );
  media.ready = true;
  media.socket = {
    send(options) {
      sent.push(options);
    },
    close() {},
  };

  media._startRecording();

  assert.equal(media.recording, false);
  assert.equal(media.recorderStarted, false);
  assert.equal(recorder.startCalls.length, 1);

  recorder.startListener();
  assert.equal(media.recording, true);
  assert.equal(media.recorderStarted, true);

  recorder.frameListener({ frameBuffer: new Uint8Array(64).buffer });

  assert.equal(sent.length, 1);
  assert.equal(sent[0].data instanceof ArrayBuffer, true);
  assert.equal(media.sequence, 1);
  assert.equal(media._firstUplinkFrame, true);
  assert.deepEqual(errors, []);
  await media.close();
});

test("PCM frames keep sending when SocketTask omits success callbacks", async () => {
  recorder.reset();
  const sent = [];
  const media = new MiniProgramMediaSession(
    { media_gateway: { audio: { sample_rate: 24000 } } },
    {},
  );
  media.ready = true;
  media.socket = {
    send(options) {
      sent.push(options);
    },
    close() {},
  };

  media._startRecording();
  recorder.startListener();
  recorder.frameListener({ frameBuffer: new Uint8Array(64).buffer });
  recorder.frameListener({ frameBuffer: new Uint8Array(64).buffer });

  assert.equal(sent.length, 2);
  assert.equal(media.sequence, 2);
  await media.close();
});

test("microphone disable wins over a pending RecorderManager start", async () => {
  recorder.reset();
  const sent = [];
  const media = new MiniProgramMediaSession(
    {
      media_gateway: {
        audio: { sample_rate: 24000, frame_ms: 20 },
        playout: { post_playout_guard_ms: 0 },
      },
    },
    {},
  );
  media.ready = true;
  media.socket = {
    send(options) {
      sent.push(options);
    },
    close() {},
  };

  media._startRecording();
  await media.setMicrophoneEnabled(false);
  recorder.startListener();
  recorder.frameListener({ frameBuffer: new Uint8Array(64).buffer });

  assert.equal(recorder.stopCalls, 1);
  assert.equal(media.recording, false);
  assert.equal(sent.length, 0);

  await media.setMicrophoneEnabled(true);
  assert.equal(recorder.startCalls.length, 2);
  recorder.startListener();
  recorder.frameListener({ frameBuffer: new Uint8Array(64).buffer });

  assert.equal(sent.length, 1);
  assert.equal(media.sequence, 1);
  await media.close();
});

test("assistant response pauses uplink until authoritative completion and local playout drain", async () => {
  recorder.reset();
  const originalCreateWebAudioContext = global.wx.createWebAudioContext;
  let scheduledSource = null;
  global.wx.createWebAudioContext = () => ({
    state: "running",
    currentTime: 1,
    destination: {},
    createGain: () => ({
      gain: { value: 1 },
      connect() {},
    }),
    createBuffer(_channels, length, sampleRate) {
      return {
        duration: length / sampleRate,
        getChannelData: () => new Float32Array(length),
      };
    },
    createBufferSource() {
      scheduledSource = {
        buffer: null,
        onended: null,
        connect() {},
        start() {},
        stop() {},
      };
      return scheduledSource;
    },
    resume: async () => {},
    close: async () => {},
  });
  const sent = [];
  const media = new MiniProgramMediaSession(
    {
      media_gateway: {
        audio: { sample_rate: 24000, frame_ms: 20 },
        playout: { post_playout_guard_ms: 0 },
      },
    },
    {},
  );
  media.ready = true;
  media.socket = {
    send(options) {
      sent.push(options.data);
    },
    close() {},
  };

  try {
    await media.player.resume();
    media._startRecording();
    recorder.startListener();
    assert.equal(media.recording, true);

    media._onMessage({
      data: JSON.stringify({
        type: "ui_event",
        event: { type: "assistant_state", state: "thinking", generation_id: 1 },
      }),
    });
    assert.equal(media.recording, false);
    assert.equal(recorder.pauseCalls, 1);

    recorder.frameListener({ frameBuffer: new Uint8Array(64).buffer });
    assert.equal(sent.filter((item) => item instanceof ArrayBuffer).length, 0);

    const frame = new Int16Array(480).buffer;
    for (let sequence = 0; sequence < 4; sequence += 1) {
      media._onMessage({
        data: encodePcmFrame(
          FRAME_TYPE.DOWNLINK_AUDIO,
          sequence,
          Date.now(),
          frame,
        ),
      });
    }
    assert.ok(scheduledSource);

    media._onMessage({
      data: JSON.stringify({
        type: "ui_event",
        event: { type: "assistant_state", state: "listening", generation_id: 1 },
      }),
    });
    assert.equal(media.recording, false);
    assert.equal(recorder.resumeCalls, 0);

    scheduledSource.onended();
    assert.equal(media.recording, true);
    assert.equal(recorder.resumeCalls, 1);
  } finally {
    await media.close();
    global.wx.createWebAudioContext = originalCreateWebAudioContext;
  }
});

test("capture waits for the configured post-playout guard after the last source ends", async () => {
  recorder.reset();
  const originalCreateWebAudioContext = global.wx.createWebAudioContext;
  let scheduledSource = null;
  global.wx.createWebAudioContext = () => ({
    state: "running",
    currentTime: 1,
    destination: {},
    createGain: () => ({
      gain: { value: 1 },
      connect() {},
    }),
    createBuffer(_channels, length, sampleRate) {
      return {
        duration: length / sampleRate,
        getChannelData: () => new Float32Array(length),
      };
    },
    createBufferSource() {
      scheduledSource = {
        buffer: null,
        onended: null,
        connect() {},
        start() {},
        stop() {},
      };
      return scheduledSource;
    },
    resume: async () => {},
    close: async () => {},
  });
  const media = new MiniProgramMediaSession(
    {
      media_gateway: {
        audio: { sample_rate: 24000, frame_ms: 20 },
        playout: { post_playout_guard_ms: 15 },
      },
    },
    {},
  );
  media.ready = true;

  try {
    await media.player.resume();
    media._startRecording();
    recorder.startListener();
    media._onMessage({
      data: JSON.stringify({
        type: "ui_event",
        event: { type: "assistant_state", state: "thinking", generation_id: 1 },
      }),
    });
    const frame = new Int16Array(480).buffer;
    for (let sequence = 0; sequence < 4; sequence += 1) {
      media._onMessage({
        data: encodePcmFrame(
          FRAME_TYPE.DOWNLINK_AUDIO,
          sequence,
          Date.now(),
          frame,
        ),
      });
    }
    media._onMessage({
      data: JSON.stringify({
        type: "ui_event",
        event: { type: "assistant_state", state: "listening", generation_id: 1 },
      }),
    });

    scheduledSource.onended();
    assert.equal(media.recording, false);
    assert.equal(recorder.resumeCalls, 0);

    await new Promise((resolve) => setTimeout(resolve, 25));
    assert.equal(media.recording, true);
    assert.equal(recorder.resumeCalls, 1);
  } finally {
    await media.close();
    global.wx.createWebAudioContext = originalCreateWebAudioContext;
  }
});

test("assistant completion never overrides an explicit microphone mute", async () => {
  recorder.reset();
  const media = new MiniProgramMediaSession(
    { media_gateway: { audio: { sample_rate: 24000, frame_ms: 20 } } },
    {},
  );
  media.ready = true;

  media._startRecording();
  recorder.startListener();
  await media.setMicrophoneEnabled(false);
  media._onMessage({
    data: JSON.stringify({
      type: "ui_event",
      event: { type: "assistant_state", state: "thinking", generation_id: 1 },
    }),
  });
  media._onMessage({
    data: JSON.stringify({
      type: "ui_event",
      event: { type: "assistant_state", state: "listening", generation_id: 1 },
    }),
  });

  assert.equal(media.microphoneEnabled, false);
  assert.equal(media.recording, false);
  assert.equal(recorder.resumeCalls, 0);
  await media.close();
});

test("explicit input policy outranks assistant state strings and rejects stale epochs", async () => {
  recorder.reset();
  const media = new MiniProgramMediaSession(
    { media_gateway: { audio: { sample_rate: 24000, frame_ms: 20 } } },
    {},
  );
  media.ready = true;

  media._startRecording();
  recorder.startListener();
  assert.equal(media.recording, true);

  media._onMessage({
    data: JSON.stringify({
      type: "ui_event",
      event: {
        type: "input_policy",
        capture_allowed: false,
        policy_epoch: 2,
        reason: "assistant_speaking",
        generation_id: 1,
      },
    }),
  });
  assert.equal(media.recording, false);
  assert.equal(recorder.pauseCalls, 1);

  media._onMessage({
    data: JSON.stringify({
      type: "ui_event",
      event: { type: "assistant_state", state: "listening", generation_id: 1 },
    }),
  });
  media._onMessage({
    data: JSON.stringify({
      type: "ui_event",
      event: {
        type: "input_policy",
        capture_allowed: true,
        policy_epoch: 1,
        reason: "assistant_listening",
        generation_id: 1,
      },
    }),
  });
  assert.equal(media.recording, false);
  assert.equal(recorder.resumeCalls, 0);

  media._onMessage({
    data: JSON.stringify({
      type: "ui_event",
      event: {
        type: "input_policy",
        capture_allowed: true,
        policy_epoch: 3,
        reason: "assistant_listening",
        generation_id: 1,
      },
    }),
  });
  assert.equal(media.recording, true);
  assert.equal(recorder.resumeCalls, 1);
  await media.close();
});

test("input policy rejects the reserved zero epoch", async () => {
  recorder.reset();
  const media = new MiniProgramMediaSession(
    { media_gateway: { audio: { sample_rate: 24000, frame_ms: 20 } } },
    {},
  );
  media.ready = true;
  media._startRecording();
  recorder.startListener();

  media._onMessage({
    data: JSON.stringify({
      type: "ui_event",
      event: {
        type: "input_policy",
        capture_allowed: false,
        policy_epoch: 0,
        reason: "invalid_epoch",
        generation_id: 1,
      },
    }),
  });

  assert.equal(media.inputPolicyReceived, false);
  assert.equal(media.recording, true);
  await media.close();
});

test("synchronous SocketTask send failure does not commit the PCM sequence", async () => {
  recorder.reset();
  const interruptions = [];
  let attempts = 0;
  const media = new MiniProgramMediaSession(
    { media_gateway: { audio: { sample_rate: 24000, frame_ms: 20 } } },
    { onInterrupted: (message) => interruptions.push(message) },
  );
  media.ready = true;
  media.socket = {
    send() {
      attempts += 1;
      throw new Error("socket closed");
    },
    close() {},
  };

  media._startRecording();
  recorder.startListener();
  recorder.frameListener({ frameBuffer: new Uint8Array(64).buffer });
  recorder.frameListener({ frameBuffer: new Uint8Array(64).buffer });

  assert.equal(attempts, 1);
  assert.equal(media.sequence, 0);
  assert.deepEqual(interruptions, ["麦克风音频发送失败，请轻触恢复语音。"]);
  await media.close();
});

test("SocketTask PCM send failure is surfaced instead of being silently ignored", async () => {
  recorder.reset();
  const interruptions = [];
  let sendOptions = null;
  const media = new MiniProgramMediaSession(
    { media_gateway: { audio: { sample_rate: 24000 } } },
    { onInterrupted: (message) => interruptions.push(message) },
  );
  media.ready = true;
  media.socket = {
    send(options) {
      sendOptions = options;
    },
    close() {},
  };

  media._startRecording();
  recorder.startListener();
  recorder.frameListener({ frameBuffer: new Uint8Array(64).buffer });
  sendOptions.fail();

  assert.deepEqual(interruptions, ["麦克风音频发送失败，请轻触恢复语音。"]);
  await media.close();
});

test("unexpected socket close terminalizes local recording before notifying the page", async () => {
  recorder.reset();
  const sent = [];
  const closes = [];
  const socket = {
    onOpen(listener) {
      this.openListener = listener;
    },
    onMessage(listener) {
      this.messageListener = listener;
    },
    onError(listener) {
      this.errorListener = listener;
    },
    onClose(listener) {
      this.closeListener = listener;
    },
    send(options) {
      sent.push(options);
    },
    close() {},
  };
  global.wx.connectSocket = () => socket;
  const media = new MiniProgramMediaSession(
    {
      media_gateway: {
        websocket_url: "wss://voice.example.com/media",
        ticket: "ticket",
        audio: {
          sample_rate: 24000,
          channels: 1,
          sample_format: "s16le",
          frame_ms: 20,
        },
      },
    },
    { onClose: () => closes.push("closed") },
  );

  const connecting = media.connect();
  await new Promise((resolve) => setImmediate(resolve));
  socket.messageListener({
    data: JSON.stringify({
      type: "ready",
      protocol_version: 1,
      session_id: "session-1",
      audio: {
        sample_rate: 24000,
        channels: 1,
        sample_format: "s16le",
        frame_ms: 20,
        frame_protocol_version: 2,
      },
    }),
  });
  await connecting;
  recorder.startListener();

  socket.closeListener();
  recorder.frameListener({ frameBuffer: new Uint8Array(64).buffer });
  socket.closeListener();

  assert.equal(media.ready, false);
  assert.equal(media.recording, false);
  assert.equal(media.recorderStarted, false);
  assert.equal(media.socket, null);
  assert.equal(recorder.stopCalls, 1);
  assert.deepEqual(closes, ["closed"]);
  assert.equal(sent.filter((item) => item.data instanceof ArrayBuffer).length, 0);
  await media.close();
});

test("gateway ready rejects an incompatible downlink audio contract", async () => {
  recorder.reset();
  let closed = 0;
  const socket = {
    onOpen(listener) {
      this.openListener = listener;
    },
    onMessage(listener) {
      this.messageListener = listener;
    },
    onError(listener) {
      this.errorListener = listener;
    },
    onClose(listener) {
      this.closeListener = listener;
    },
    send() {},
    close() {
      closed += 1;
    },
  };
  global.wx.connectSocket = () => socket;
  const media = new MiniProgramMediaSession(
    {
      media_gateway: {
        websocket_url: "wss://voice.example.com/media",
        ticket: "ticket",
        audio: {
          sample_rate: 24000,
          channels: 1,
          sample_format: "s16le",
          frame_ms: 20,
        },
      },
    },
    {},
  );

  const connecting = media.connect();
  await new Promise((resolve) => setImmediate(resolve));
  socket.messageListener({
    data: JSON.stringify({
      type: "ready",
      protocol_version: 1,
      session_id: "session-1",
      audio: {
        sample_rate: 24000,
        channels: 1,
        sample_format: "s16le",
        frame_ms: 40,
        frame_protocol_version: 2,
      },
    }),
  });

  await assert.rejects(connecting, /音频格式不兼容/);
  assert.equal(media.ready, false);
  assert.equal(recorder.startCalls.length, 0);
  assert.equal(closed, 1);
  await media.close();
});

test("system recorder interruption resumes with an explicit media discontinuity", async () => {
  recorder.reset();
  const sent = [];
  const states = [];
  const interruptions = [];
  const media = new MiniProgramMediaSession(
    { media_gateway: { audio: { sample_rate: 24000, frame_ms: 20 } } },
    {
      onEvent: (event) => states.push(event),
      onInterrupted: (message) => interruptions.push(message),
    },
  );
  media.ready = true;
  media.socket = {
    send(options) {
      sent.push(options.data);
    },
    close() {},
  };

  media._startRecording();
  recorder.startListener();
  recorder.frameListener({ frameBuffer: new Uint8Array(64).buffer });
  assert.equal(media.sequence, 1);

  recorder.interruptionListener();
  recorder.frameListener({ frameBuffer: new Uint8Array(64).buffer });
  assert.equal(media.recording, false);
  assert.equal(recorder.stopCalls, 1);
  assert.equal(media.sequence, 1);

  recorder.interruptionEndListener();
  await new Promise((resolve) => setImmediate(resolve));

  const discontinuity = sent
    .filter((data) => typeof data === "string")
    .map((data) => JSON.parse(data))
    .find((event) => event.type === "uplink_discontinuity");
  assert.deepEqual(discontinuity, {
    type: "uplink_discontinuity",
    next_sequence: 1,
    client_timestamp_ms: discontinuity.client_timestamp_ms,
  });
  assert.deepEqual(
    Object.keys(discontinuity).sort(),
    [...contract.control_events.uplink_discontinuity].sort(),
  );
  assert.equal(Number.isInteger(discontinuity.client_timestamp_ms), true);
  assert.equal(recorder.startCalls.length, 2);

  recorder.startListener();
  recorder.frameListener({ frameBuffer: new Uint8Array(64).buffer });
  assert.equal(media.sequence, 2);
  assert.deepEqual(states, [
    { type: "recorder_state", state: "interrupted" },
    { type: "recorder_state", state: "resumed" },
  ]);
  assert.deepEqual(interruptions, []);
  await media.close();
});

test("system interruption fences microphone toggles until interruption end", async () => {
  recorder.reset();
  const sent = [];
  const states = [];
  const media = new MiniProgramMediaSession(
    { media_gateway: { audio: { sample_rate: 24000, frame_ms: 20 } } },
    { onEvent: (event) => states.push(event) },
  );
  media.ready = true;
  media.socket = {
    send(options) {
      sent.push(options.data);
    },
    close() {},
  };

  media._startRecording();
  recorder.startListener();
  recorder.interruptionListener();
  await media.setMicrophoneEnabled(false);
  await media.setMicrophoneEnabled(true);

  assert.equal(media.recording, false);
  assert.equal(recorder.startCalls.length, 1);
  assert.equal(recorder.resumeCalls, 0);
  assert.equal(sent.length, 0);

  recorder.interruptionEndListener();

  assert.equal(recorder.startCalls.length, 2);
  assert.deepEqual(states, [
    { type: "recorder_state", state: "interrupted" },
    { type: "recorder_state", state: "resumed" },
  ]);
  assert.equal(JSON.parse(sent[0]).type, "uplink_discontinuity");
  await media.close();
});

test("interruption end while muted waits to synchronize and report resumed", async () => {
  recorder.reset();
  const sent = [];
  const states = [];
  const media = new MiniProgramMediaSession(
    { media_gateway: { audio: { sample_rate: 24000, frame_ms: 20 } } },
    { onEvent: (event) => states.push(event) },
  );
  media.ready = true;
  media.socket = {
    send(options) {
      sent.push(options.data);
    },
    close() {},
  };

  media._startRecording();
  recorder.startListener();
  recorder.interruptionListener();
  await media.setMicrophoneEnabled(false);
  recorder.interruptionEndListener();

  assert.equal(recorder.startCalls.length, 1);
  assert.equal(sent.length, 0);
  assert.deepEqual(states, [
    { type: "recorder_state", state: "interrupted" },
  ]);

  await media.setMicrophoneEnabled(true);

  assert.equal(recorder.startCalls.length, 2);
  assert.equal(JSON.parse(sent[0]).type, "uplink_discontinuity");
  assert.deepEqual(states, [
    { type: "recorder_state", state: "interrupted" },
    { type: "recorder_state", state: "resumed" },
  ]);
  await media.close();
});

test("SocketTask connection failures retain the actionable platform error", async () => {
  recorder.reset();
  const socket = {
    onOpen(listener) {
      this.openListener = listener;
    },
    onMessage(listener) {
      this.messageListener = listener;
    },
    onError(listener) {
      this.errorListener = listener;
    },
    onClose(listener) {
      this.closeListener = listener;
    },
    close() {},
  };
  global.wx.connectSocket = () => socket;
  const media = new MiniProgramMediaSession(
    {
      media_gateway: {
        websocket_url: "wss://voice.example.com/media",
        ticket: "ticket",
      },
    },
    {},
  );

  const connecting = media.connect();
  await new Promise((resolve) => setImmediate(resolve));
  socket.errorListener({ errMsg: "connectSocket:fail url not in domain list" });

  await assert.rejects(connecting, /Socket 合法域名未生效/);
  await media.close();
});

test("WebSocket handshake failures are not misreported as certificate errors", async () => {
  recorder.reset();
  const socket = {
    onOpen(listener) {
      this.openListener = listener;
    },
    onMessage(listener) {
      this.messageListener = listener;
    },
    onError(listener) {
      this.errorListener = listener;
    },
    onClose(listener) {
      this.closeListener = listener;
    },
    close() {},
  };
  global.wx.connectSocket = () => socket;
  const media = new MiniProgramMediaSession(
    {
      media_gateway: {
        websocket_url: "wss://voice.example.com/media",
        ticket: "ticket",
      },
    },
    {},
  );

  const connecting = media.connect();
  await new Promise((resolve) => setImmediate(resolve));
  socket.errorListener({ errMsg: "connectSocket:fail WebSocket handshake failed" });

  await assert.rejects(
    connecting,
    (error) =>
      /WebSocket 握手失败/.test(error.message) &&
      /handshake failed/i.test(error.message) &&
      !/证书/.test(error.message),
  );
  await media.close();
});

test("SocketTask connection refused points to the device network path", async () => {
  recorder.reset();
  const socket = {
    onOpen(listener) {
      this.openListener = listener;
    },
    onMessage(listener) {
      this.messageListener = listener;
    },
    onError(listener) {
      this.errorListener = listener;
    },
    onClose(listener) {
      this.closeListener = listener;
    },
    close() {},
  };
  global.wx.connectSocket = () => socket;
  const media = new MiniProgramMediaSession(
    {
      media_gateway: {
        websocket_url: "wss://voice.example.com/media",
        ticket: "ticket",
      },
    },
    {},
  );

  const connecting = media.connect();
  await new Promise((resolve) => setImmediate(resolve));
  socket.errorListener({ errMsg: "connectSocket:fail connection refused" });

  await assert.rejects(
    connecting,
    (error) =>
      error?.code === "socket_connection_refused" &&
      /Wi‑Fi\/移动网络.*VPN\/代理/.test(error.message),
  );
  await media.close();
});

test("SocketTask timeout mentioning refusal does not trigger the refusal recovery path", async () => {
  recorder.reset();
  const socket = {
    onMessage(listener) {
      this.messageListener = listener;
    },
    onError(listener) {
      this.errorListener = listener;
    },
    onClose(listener) {
      this.closeListener = listener;
    },
    close() {},
  };
  global.wx.connectSocket = () => socket;
  const media = new MiniProgramMediaSession(
    {
      media_gateway: {
        websocket_url: "wss://voice.example.com/media",
        ticket: "ticket",
      },
    },
    {},
  );

  const connecting = media.connect();
  await new Promise((resolve) => setImmediate(resolve));
  socket.errorListener({ errMsg: "connectSocket:fail timeout after connection refused" });

  await assert.rejects(
    connecting,
    (error) =>
      error?.code !== "socket_connection_refused" && /语音网络连接超时/.test(error.message),
  );
  await media.close();
});

test("a recorder without frames is restarted once before the user is notified", async () => {
  recorder.reset();
  const interruptions = [];
  const media = new MiniProgramMediaSession(
    { media_gateway: { audio: { sample_rate: 24000 } } },
    { onInterrupted: (message) => interruptions.push(message) },
  );
  media.ready = true;

  media._startRecording();
  recorder.startListener();
  media._recoverRecorder();

  await new Promise((resolve) => setTimeout(resolve, 140));
  assert.equal(recorder.stopCalls, 1);
  assert.equal(recorder.startCalls.length, 2);

  recorder.startListener();
  media._recoverRecorder();

  assert.deepEqual(interruptions, ["未收到麦克风音频，请轻触恢复语音。"]);
  await media.close();
});

test("assistant audio controls immediately duck and restore Mini Program playback", () => {
  recorder.reset();
  const gains = [];
  const media = new MiniProgramMediaSession(
    { media_gateway: { audio: { sample_rate: 24000 } } },
    {},
  );
  media.player = {
    reset() {},
    setGain(value) {
      gains.push(value);
    },
  };

  media._onMessage({
    data: JSON.stringify({
      type: "ui_event",
      event: {
        type: "assistant_audio",
        action: "duck",
        gain: 0.25,
      },
    }),
  });
  media._onMessage({
    data: JSON.stringify({
      type: "ui_event",
      event: {
        type: "assistant_audio",
        action: "restore",
        gain: 1,
      },
    }),
  });

  assert.deepEqual(gains, [0.25, 1]);
});

test("button stop flushes local playback and reports the current generation", () => {
  recorder.reset();
  const sent = [];
  let interrupted = 0;
  const media = new MiniProgramMediaSession(
    { media_gateway: { audio: { sample_rate: 24000, frame_ms: 20 } } },
    {},
  );
  media.ready = true;
  media.inputPolicyReceived = true;
  media.serverCaptureAllowed = false;
  media.playbackGenerationId = 4;
  media.player = {
    generationId: 4,
    interrupt() {
      interrupted += 1;
    },
  };
  media.socket = {
    send(options) {
      sent.push(options.data);
    },
  };

  media.stopAssistantPlayback();

  assert.equal(interrupted, 1);
  assert.equal(media._microphoneCaptureEnabled(), false);
  assert.deepEqual(JSON.parse(sent[0]), {
    type: "playout_interrupt",
    generation_id: 4,
    client_timestamp_ms: JSON.parse(sent[0]).client_timestamp_ms,
  });
});

test("media session forwards downlink sequence and reset generation to the PCM player", () => {
  recorder.reset();
  const resets = [];
  const enqueued = [];
  const sent = [];
  const media = new MiniProgramMediaSession(
    { media_gateway: { audio: { sample_rate: 24000 } } },
    {},
  );
  media.socket = {
    send(message) {
      sent.push(message.data);
    },
  };
  media.player = {
    reset(generationId, barrierSequence) {
      resets.push({ generationId, barrierSequence });
    },
    setGain() {},
    enqueue(payload, metadata) {
      enqueued.push({ byteLength: payload.byteLength, metadata });
    },
  };

  media._onMessage({
    data: JSON.stringify({
      type: "audio_reset",
      generation_id: 3,
      barrier_sequence: 7,
    }),
  });
  const downlink = new ArrayBuffer(24 + 960);
  const downlinkView = new DataView(downlink);
  downlinkView.setUint8(0, FRAME_TYPE.DOWNLINK_AUDIO);
  downlinkView.setUint8(1, 2);
  downlinkView.setUint16(2, 0);
  downlinkView.setUint32(4, 7);
  downlinkView.setUint32(8, 4);
  downlinkView.setUint32(12, 0);
  downlinkView.setUint32(16, 123);
  downlinkView.setUint32(20, 960);
  media._onMessage({
    data: downlink,
  });

  assert.deepEqual(resets, [{ generationId: 3, barrierSequence: 7 }]);
  assert.deepEqual(enqueued, [
    { byteLength: 960, metadata: { sequence: 7, generationId: 4 } },
  ]);
  assert.deepEqual(JSON.parse(sent[0]), {
    type: "playout_reset",
    generation_id: 3,
    barrier_sequence: 7,
    client_timestamp_ms: JSON.parse(sent[0]).client_timestamp_ms,
  });
  assert.deepEqual(
    Object.keys(JSON.parse(sent[0])).sort(),
    [...contract.control_events.playout_reset].sort(),
  );
  assert.equal(Number.isInteger(JSON.parse(sent[0]).client_timestamp_ms), true);
});

test("PCM player applies gain through one shared WebAudio node", async () => {
  recorder.reset();
  const player = new PcmJitterPlayer();

  await player.resume();
  player.setGain(0.25);

  assert.equal(player.gain, 0.25);
  assert.equal(player.gainNode.gain.value, 0.25);
});

test("media session keeps first-playback telemetry compatible with a legacy gateway", () => {
  recorder.reset();
  const sent = [];
  const media = new MiniProgramMediaSession(
    { media_gateway: { audio: { sample_rate: 24000, frame_ms: 20 } } },
    {},
  );
  media.ready = true;
  media.socket = {
    send(options) {
      sent.push(options.data);
    },
    close() {},
  };
  media.player.context = {
    state: "running",
    currentTime: 1,
    destination: {},
    createBuffer(_channels, length, sampleRate) {
      return {
        duration: length / sampleRate,
        getChannelData: () => new Float32Array(length),
      };
    },
    createBufferSource() {
      return {
        buffer: null,
        connect() {},
        start() {},
      };
    },
  };
  const frame = new Int16Array(480).buffer;

  for (let sequence = 0; sequence < 4; sequence += 1) {
    media.player.enqueue(frame, { sequence, generationId: 7 });
  }

  const trace = sent
    .filter((data) => typeof data === "string")
    .map((data) => JSON.parse(data))
    .find((event) => event.type === "client_audio_trace");
  assert.equal(trace.name, "first_playback");
  assert.equal(trace.generation_id, 7);
  assert.equal(Number.isInteger(trace.client_timestamp_ms), true);
  assert.deepEqual(
    Object.keys(trace).sort(),
    [...contract.control_events.client_audio_trace.required_fields].sort(),
  );
  assert.deepEqual(
    Object.keys(trace.detail).sort(),
    [
      "pending_audio_ms",
      "queue_lead_ms",
      "scheduled_sources",
    ],
  );
});

test("media session enables adaptive playback telemetry after a v2 ready advertisement", () => {
  recorder.reset();
  const sent = [];
  const media = new MiniProgramMediaSession(
    { media_gateway: { audio: { sample_rate: 24000, frame_ms: 20 } } },
    {},
  );
  assert.equal(
    media._acceptReadyAudioContract({
      protocol_version: 1,
      audio: {
        sample_rate: 24000,
        channels: 1,
        sample_format: "s16le",
        frame_ms: 20,
        frame_protocol_version: 2,
      },
      client_audio_trace_version:
        contract.control_events.client_audio_trace.protocol_version,
    }),
    true,
  );
  media.ready = true;
  media.socket = {
    send(options) {
      sent.push(JSON.parse(options.data));
    },
    close() {},
  };

  assert.equal(
    media._sendClientAudioTrace({
      name: "miniprogram_playback_lead_adjusted",
      generationId: 7,
      detail: {
        queue_lead_ms: 120,
        target_lead_ms: 120,
        underflow_count: 1,
      },
    }),
    true,
  );
  assert.deepEqual(sent, [
    {
      type: "client_audio_trace",
      name: "miniprogram_playback_lead_adjusted",
      generation_id: 7,
      client_timestamp_ms: sent[0].client_timestamp_ms,
      detail: {
        queue_lead_ms: 120,
        target_lead_ms: 120,
        underflow_count: 1,
      },
    },
  ]);
});

test("PCM player rebases an underflow instead of scheduling a late frame in the past", () => {
  let startedAt = null;
  let existingStopped = 0;
  const traces = [];
  const player = new PcmJitterPlayer({
    sampleRate: 24000,
    minLeadSeconds: 0.08,
    onTrace: (event) => traces.push(event),
  });
  player.context = {
    state: "running",
    currentTime: 1,
    destination: {},
    createBuffer(_channels, length, sampleRate) {
      return {
        duration: length / sampleRate,
        getChannelData: () => new Float32Array(length),
      };
    },
    createBufferSource() {
      return {
        buffer: null,
        connect() {},
        start(at) {
          startedAt = at;
        },
      };
    },
  };
  player.nextStartAt = 0.99;
  player.sources.add({
    stop() {
      existingStopped += 1;
    },
  });

  const frame = new Int16Array(480).buffer;
  for (let sequence = 0; sequence < 4; sequence += 1) {
    player.enqueue(frame, { sequence, generationId: 1 });
  }

  assert.equal(startedAt, 1.1);
  assert.equal(existingStopped, 0);
  assert.equal(traces[0].name, "miniprogram_playback_underrun");
  assert.equal(traces[0].generationId, 1);
  assert.ok(traces.some(({ name }) => name === "first_playback"));
});

test("PCM player raises target lead after underflow and decays it after a stable window", () => {
  const started = [];
  const traces = [];
  const player = new PcmJitterPlayer({
    minLeadSeconds: 0.1,
    maxAdaptiveLeadSeconds: 0.18,
    leadStepSeconds: 0.02,
    leadStableSeconds: 30,
    onTrace: (event) => traces.push(event),
  });
  player.context = {
    state: "running",
    currentTime: 1,
    destination: {},
    createBuffer(_channels, length, sampleRate) {
      return {
        duration: length / sampleRate,
        getChannelData: () => new Float32Array(length),
      };
    },
    createBufferSource() {
      return {
        buffer: null,
        connect() {},
        start(at) {
          started.push(at);
        },
      };
    },
  };
  player.nextStartAt = 0.99;
  const frame = new Int16Array(480).buffer;

  for (let sequence = 0; sequence < 4; sequence += 1) {
    player.enqueue(frame, { sequence, generationId: 1 });
  }

  assert.equal(player.targetLeadSeconds, 0.12);
  assert.equal(started[0], 1.12);

  player.context.currentTime = 32;
  player.nextStartAt = 32.2;
  for (let sequence = 4; sequence < 8; sequence += 1) {
    player.enqueue(frame, { sequence, generationId: 1 });
  }

  assert.equal(player.targetLeadSeconds, 0.1);
  const leadChanges = traces.filter(
    ({ name }) => name === "miniprogram_playback_lead_adjusted",
  );
  assert.deepEqual(
    leadChanges.map(({ detail }) => detail.target_lead_ms),
    [120, 100],
  );
});

test("PCM player batches four continuous 20ms frames into one 80ms source", () => {
  const started = [];
  const player = new PcmJitterPlayer({ sampleRate: 24000, minLeadSeconds: 0.08 });
  player.context = {
    state: "running",
    currentTime: 1,
    destination: {},
    createBuffer(_channels, length, sampleRate) {
      return {
        duration: length / sampleRate,
        getChannelData: () => new Float32Array(length),
      };
    },
    createBufferSource() {
      return {
        buffer: null,
        connect() {},
        start(at) {
          started.push({ at, duration: this.buffer.duration });
        },
      };
    },
  };
  const frame = new Int16Array(480).buffer;

  for (let sequence = 0; sequence < 4; sequence += 1) {
    player.enqueue(frame, { sequence, generationId: 1 });
  }

  assert.deepEqual(started, [{ at: 1.08, duration: 0.08 }]);
});

test("PCM player reports active playback until every scheduled source ends", () => {
  const states = [];
  let source = null;
  const player = new PcmJitterPlayer({
    onPlaybackStateChange: (active) => states.push(active),
  });
  player.context = {
    state: "running",
    currentTime: 1,
    destination: {},
    createBuffer(_channels, length, sampleRate) {
      return {
        duration: length / sampleRate,
        getChannelData: () => new Float32Array(length),
      };
    },
    createBufferSource() {
      source = {
        buffer: null,
        onended: null,
        connect() {},
        start() {},
        stop() {},
      };
      return source;
    },
  };
  const frame = new Int16Array(480).buffer;

  for (let sequence = 0; sequence < 4; sequence += 1) {
    player.enqueue(frame, { sequence, generationId: 1 });
  }

  assert.deepEqual(states, [true]);
  source.onended();
  assert.deepEqual(states, [true, false]);
});

test("PCM player rejects payloads that are not one fixed PCM16/20ms frame", () => {
  const player = new PcmJitterPlayer({ sampleRate: 24000 });
  player.context = { state: "running" };

  assert.throws(
    () => player.enqueue(new ArrayBuffer(959), { sequence: 0, generationId: 1 }),
    /960-byte/,
  );
  assert.throws(
    () => player.enqueue(new ArrayBuffer(962), { sequence: 0, generationId: 1 }),
    /960-byte/,
  );
});

test("PCM player keeps the prior batch tail when concealing the next sequence gap", () => {
  const rendered = [];
  const player = new PcmJitterPlayer();
  player.context = {
    state: "running",
    currentTime: 1,
    destination: {},
    createBuffer(_channels, length, sampleRate) {
      const channel = new Float32Array(length);
      rendered.push(channel);
      return {
        duration: length / sampleRate,
        getChannelData: () => channel,
      };
    },
    createBufferSource() {
      return {
        buffer: null,
        connect() {},
        start() {},
        stop() {},
      };
    },
  };
  const first = new Int16Array(480);
  first.fill(16000);
  const next = new Int16Array(480);
  next.fill(8000);

  for (let sequence = 0; sequence < 4; sequence += 1) {
    player.enqueue(first.buffer, { sequence, generationId: 1 });
  }
  for (const sequence of [5, 6, 7]) {
    player.enqueue(next.buffer, { sequence, generationId: 1 });
  }

  assert.equal(rendered.length, 2);
  assert.ok(rendered[1][0] > 0.45);
  assert.equal(rendered[1][479], 0);
});

test("PCM player schedules its first batch with the configured lead at time zero", () => {
  let startedAt = null;
  const player = new PcmJitterPlayer({ minLeadSeconds: 0.08 });
  player.context = {
    state: "running",
    currentTime: 0,
    destination: {},
    createBuffer(_channels, length, sampleRate) {
      return {
        duration: length / sampleRate,
        getChannelData: () => new Float32Array(length),
      };
    },
    createBufferSource() {
      return {
        buffer: null,
        connect() {},
        start(at) {
          startedAt = at;
        },
      };
    },
  };
  const frame = new Int16Array(480).buffer;

  for (let sequence = 0; sequence < 4; sequence += 1) {
    player.enqueue(frame, { sequence, generationId: 1 });
  }

  assert.equal(startedAt, 0.08);
});

test("PCM player conceals small sequence gaps but still stops old generations", () => {
  const started = [];
  let stopped = 0;
  const player = new PcmJitterPlayer();
  player.context = {
    state: "running",
    currentTime: 1,
    destination: {},
    createBuffer(_channels, length, sampleRate) {
      return {
        duration: length / sampleRate,
        getChannelData: () => new Float32Array(length),
      };
    },
    createBufferSource() {
      return {
        buffer: null,
        connect() {},
        start(at) {
          started.push(at);
        },
        stop() {
          stopped += 1;
        },
      };
    },
  };
  const frame = new Int16Array(480).buffer;

  for (let sequence = 0; sequence < 4; sequence += 1) {
    player.enqueue(frame, { sequence, generationId: 1 });
  }
  player.reset(2);
  for (let sequence = 4; sequence < 8; sequence += 1) {
    player.enqueue(frame, { sequence, generationId: 1 });
  }
  assert.equal(stopped, 1);
  assert.equal(started.length, 1);

  for (let sequence = 4; sequence < 8; sequence += 1) {
    player.enqueue(frame, { sequence, generationId: 2 });
  }
  assert.equal(started.length, 2);

  for (let sequence = 10; sequence < 14; sequence += 1) {
    player.enqueue(frame, { sequence, generationId: 2 });
  }

  assert.equal(stopped, 1);
  assert.equal(started.length, 3);
});

test("PCM player hard-resets after a sequence gap exceeds bounded concealment", () => {
  const started = [];
  const traces = [];
  let stopped = 0;
  const player = new PcmJitterPlayer({
    onTrace: (event) => traces.push(event),
  });
  player.context = {
    state: "running",
    currentTime: 1,
    destination: {},
    createBuffer(_channels, length, sampleRate) {
      return {
        duration: length / sampleRate,
        getChannelData: () => new Float32Array(length),
      };
    },
    createBufferSource() {
      return {
        buffer: null,
        connect() {},
        start(at) {
          started.push(at);
        },
        stop() {
          stopped += 1;
        },
      };
    },
  };
  const frame = new Int16Array(480).buffer;

  for (let sequence = 0; sequence < 4; sequence += 1) {
    player.enqueue(frame, { sequence, generationId: 1 });
  }
  for (let sequence = 20; sequence < 24; sequence += 1) {
    player.enqueue(frame, { sequence, generationId: 1 });
  }

  assert.equal(stopped, 1);
  assert.equal(started.length, 2);
  const hardReset = traces.find(
    ({ name }) => name === "miniprogram_playback_hard_reset",
  );
  assert.equal(hardReset.detail.missing_frames, 16);
});

test("PCM player rejects old generations and sequences below a reset barrier", () => {
  const started = [];
  const player = new PcmJitterPlayer();
  player.context = {
    state: "running",
    currentTime: 1,
    destination: {},
    createBuffer(_channels, length, sampleRate) {
      return {
        duration: length / sampleRate,
        getChannelData: () => new Float32Array(length),
      };
    },
    createBufferSource() {
      return {
        buffer: null,
        connect() {},
        start(at) {
          started.push(at);
        },
        stop() {},
      };
    },
  };
  const frame = new Int16Array(480).buffer;

  player.reset(2, 20);
  player.enqueue(frame, { sequence: 19, generationId: 2 });
  for (let sequence = 20; sequence < 24; sequence += 1) {
    player.enqueue(frame, { sequence, generationId: 1 });
  }
  assert.deepEqual(started, []);

  for (let sequence = 20; sequence < 24; sequence += 1) {
    player.enqueue(frame, { sequence, generationId: 2 });
  }
  assert.equal(started.length, 1);
});

test("PCM player fades an interrupted source before stopping it", () => {
  const automation = [];
  let stoppedAt = null;
  const player = new PcmJitterPlayer();
  player.context = { currentTime: 2 };
  player.gainNode = {
    gain: {
      value: 1,
      cancelScheduledValues(at) {
        automation.push(["cancel", at]);
      },
      setValueAtTime(value, at) {
        automation.push(["set", value, at]);
      },
      linearRampToValueAtTime(value, at) {
        automation.push(["ramp", value, at]);
      },
    },
  };
  player.sources.add({
    stop(at) {
      stoppedAt = at;
    },
  });

  player.reset(2, 10);
  player._scheduleFadeIn(2.08);

  assert.equal(stoppedAt, 2.005);
  assert.deepEqual(automation, [
    ["cancel", 2],
    ["set", 1, 2],
    ["ramp", 0, 2.005],
    ["set", 0, 2.005],
    ["cancel", 2.08],
    ["set", 0, 2.08],
    ["ramp", 1, 2.09],
  ]);
});

test("PCM player gain updates use a short ramp when WebAudio automation is available", () => {
  const automation = [];
  const player = new PcmJitterPlayer();
  player.context = { currentTime: 3 };
  player.gainNode = {
    gain: {
      value: 1,
      cancelScheduledValues(at) {
        automation.push(["cancel", at]);
      },
      setValueAtTime(value, at) {
        automation.push(["set", value, at]);
      },
      linearRampToValueAtTime(value, at) {
        automation.push(["ramp", value, at]);
      },
    },
  };

  player.setGain(0.25);

  assert.equal(player.gain, 0.25);
  assert.deepEqual(automation, [
    ["cancel", 3],
    ["set", 1, 3],
    ["ramp", 0.25, 3.01],
  ]);
});
