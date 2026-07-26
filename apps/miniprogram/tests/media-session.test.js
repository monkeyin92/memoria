const assert = require("node:assert/strict");
const test = require("node:test");

const recorder = {
  startCalls: [],
  stopCalls: 0,
  frameListener: null,
  startListener: null,
  errorListener: null,
  interruptionListener: null,
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
  start(options) {
    this.startCalls.push(options);
  },
  stop() {
    this.stopCalls += 1;
  },
  pause() {},
  resume() {},
  reset() {
    this.startCalls = [];
    this.stopCalls = 0;
    this.frameListener = null;
    this.startListener = null;
    this.errorListener = null;
    this.interruptionListener = null;
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
const { FRAME_TYPE } = require("../utils/media-protocol");
const { PcmJitterPlayer } = require("../utils/pcm-player");

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
  assert.equal(Number.isInteger(JSON.parse(sent[0]).client_timestamp_ms), true);
});

test("media session locally fences the active generation before a server interrupt", () => {
  recorder.reset();
  const resets = [];
  const gains = [];
  const sent = [];
  const events = [];
  const media = new MiniProgramMediaSession(
    { media_gateway: { audio: { sample_rate: 24000 } } },
    { onEvent: (event) => events.push(event) },
  );
  media.playbackGenerationId = 7;
  media.socket = {
    send(message) {
      sent.push(message.data);
    },
  };
  media.player = {
    setGain(value) {
      gains.push(value);
    },
    reset(generationId) {
      resets.push(generationId);
    },
  };

  media.interruptPlayback();
  media._onMessage({
    data: JSON.stringify({
      type: "audio_reset",
      generation_id: 7,
      barrier_sequence: 99,
    }),
  });
  media._onMessage({
    data: JSON.stringify({
      type: "ui_event",
      event: {
        type: "assistant_state",
        state: "speaking",
        generation_id: 7,
      },
    }),
  });

  assert.equal(media.playbackGenerationId, 8);
  assert.deepEqual(gains, [1]);
  assert.deepEqual(resets, [8]);
  assert.equal(JSON.parse(sent[0]).type, "playout_interrupt");
  assert.equal(JSON.parse(sent[0]).generation_id, 7);
  assert.deepEqual(events, []);
});

test("PCM player applies gain through one shared WebAudio node", async () => {
  recorder.reset();
  const player = new PcmJitterPlayer();

  await player.resume();
  player.setGain(0.25);

  assert.equal(player.gain, 0.25);
  assert.equal(player.gainNode.gain.value, 0.25);
});

test("PCM player rebases an underflow instead of scheduling a late frame in the past", () => {
  let startedAt = null;
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
          startedAt = at;
        },
      };
    },
  };
  player.nextStartAt = 0.99;

  player.enqueue(new Int16Array(1920).buffer);

  assert.equal(startedAt, 1.08);
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
  for (let sequence = 20; sequence < 24; sequence += 1) {
    player.enqueue(frame, { sequence, generationId: 1 });
  }

  assert.equal(stopped, 1);
  assert.equal(started.length, 2);
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

test("PCM player gain updates cancel pending fade automation", () => {
  const automation = [];
  const player = new PcmJitterPlayer();
  player.context = { currentTime: 3 };
  player.gainNode = {
    gain: {
      value: 1,
      cancelScheduledValues(at) {
        automation.push(["cancel", at]);
      },
    },
  };

  player.setGain(0.25);

  assert.equal(player.gainNode.gain.value, 0.25);
  assert.deepEqual(automation, [["cancel", 3]]);
});
