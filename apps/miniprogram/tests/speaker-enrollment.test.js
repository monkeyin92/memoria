const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const root = path.join(__dirname, "..");

test("profile exposes a dedicated four-condition owner voiceprint flow", () => {
  const app = JSON.parse(fs.readFileSync(path.join(root, "app.json"), "utf8"));
  const profile = fs.readFileSync(path.join(root, "pages/profile/index.wxml"), "utf8");

  assert.ok(app.pages.includes("pages/speaker-enrollment/index"));
  assert.match(profile, /bindtap="openSpeakerEnrollment"[\s\S]*主人声纹/);
});

test("owner voiceprint records sequential PCM prototypes and submits only speaker enrollment", async () => {
  const api = require("../utils/api");
  const originalEnroll = api.enrollSpeakerProfiles;
  const previousPage = global.Page;
  const previousWx = global.wx;
  const previousGetApp = global.getApp;
  const listeners = {};
  const recorder = {
    startOptions: null,
    onStart(listener) { listeners.start = listener; },
    onStop(listener) { listeners.stop = listener; },
    onFrameRecorded(listener) { listeners.frame = listener; },
    onError(listener) { listeners.error = listener; },
    onInterruptionBegin(listener) { listeners.interruption = listener; },
    offStart() {},
    offStop() {},
    offFrameRecorded() {},
    offError() {},
    offInterruptionBegin() {},
    start(options) { this.startOptions = options; },
    stop() { listeners.stop?.(); },
  };
  let page;
  let submitted = null;
  let navigatedBack = 0;

  global.Page = (definition) => { page = definition; };
  global.getApp = () => ({ subscribeAuthCleared: () => () => {} });
  global.wx = {
    getRecorderManager: () => recorder,
    arrayBufferToBase64: (buffer) => Buffer.from(buffer).toString("base64"),
    showToast() {},
    navigateBack() { navigatedBack += 1; },
  };
  api.enrollSpeakerProfiles = async (samples) => {
    submitted = samples;
    return { profile_id: "speaker-shadow-2", status: "shadow" };
  };

  const pagePath = require.resolve("../pages/speaker-enrollment/index");
  delete require.cache[pagePath];
  try {
    require(pagePath);
    const instance = {
      ...page,
      data: JSON.parse(JSON.stringify(page.data)),
      setData(update) { Object.assign(this.data, update); },
    };
    page.onLoad.call(instance);
    page.onConsentChange.call(instance, { detail: { value: ["accepted"] } });
    page.startRecording.call(instance, { currentTarget: { dataset: { index: 0 } } });
    listeners.start();
    listeners.frame({ frameBuffer: Uint8Array.from([1, 2, 3, 4]).buffer });
    instance._recordingStartedAt = Date.now() - 3_000;
    page.stopRecording.call(instance);

    assert.deepEqual(recorder.startOptions, {
      duration: 8_000,
      sampleRate: 16_000,
      numberOfChannels: 1,
      encodeBitRate: 24_000,
      format: "PCM",
      frameSize: 1,
    });
    assert.equal(instance.data.recordings[0].ready, true);
    assert.equal(instance.data.recordings[0].sample.scene, "owner-natural");
    assert.equal(instance.data.recordings[0].sample.audio_base64, "AQIDBA==");

    instance.data.recordings = instance.data.recordings.map((item) => ({
      ...item,
      ready: true,
      sample: item.sample || {
        audio_base64: "AQIDBA==",
        sample_rate: 16_000,
        device: "wechat-miniprogram-recorder",
        scene: item.scene,
      },
    }));
    await page.submit.call(instance);

    assert.equal(submitted.length, 4);
    assert.deepEqual(
      submitted.map((sample) => sample.scene),
      ["owner-natural", "owner-soft", "owner-bright", "owner-steady"],
    );
    assert.equal(navigatedBack, 1);
    assert.equal(api.updateProfile, require("../utils/api").updateProfile);
    page.onUnload.call(instance);
  } finally {
    api.enrollSpeakerProfiles = originalEnroll;
    delete require.cache[pagePath];
    if (previousPage === undefined) delete global.Page;
    else global.Page = previousPage;
    if (previousWx === undefined) delete global.wx;
    else global.wx = previousWx;
    if (previousGetApp === undefined) delete global.getApp;
    else global.getApp = previousGetApp;
  }
});
