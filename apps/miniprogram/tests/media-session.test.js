const assert = require("node:assert/strict");
const test = require("node:test");

const recorder = {
  onFrameRecorded() {},
  onError() {},
  onInterruptionBegin() {},
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

test("assistant audio controls immediately duck and restore Mini Program playback", () => {
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
  const player = new PcmJitterPlayer();

  await player.resume();
  player.setGain(0.25);

  assert.equal(player.gain, 0.25);
  assert.equal(player.gainNode.gain.value, 0.25);
});
