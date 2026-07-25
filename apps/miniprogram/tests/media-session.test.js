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
  sent[0].success();
  assert.equal(media._firstUplinkFrame, true);
  assert.deepEqual(errors, []);
  await media.close();
});

test("PCM frame sends stay serialized until SocketTask confirms the prior frame", async () => {
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

  assert.equal(sent.length, 1);
  sent[0].success();
  assert.equal(sent.length, 2);
  sent[1].success();
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

test("PCM player applies gain through one shared WebAudio node", async () => {
  recorder.reset();
  const player = new PcmJitterPlayer();

  await player.resume();
  player.setGain(0.25);

  assert.equal(player.gain, 0.25);
  assert.equal(player.gainNode.gain.value, 0.25);
});
