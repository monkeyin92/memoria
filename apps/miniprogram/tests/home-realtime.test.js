const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const api = require("../utils/api");

let page;
global.Page = (definition) => {
  page = definition;
};

require("../pages/home/index");

test("realtime panel replaces the previous speaker instead of accumulating rows", () => {
  const data = {
    transcript: [
      {
        speaker: "assistant",
        text: "上一句",
        source: "authoritative",
        turnId: 1,
        generationId: 1,
      },
    ],
  };
  const instance = {
    data,
    setData(update) {
      Object.assign(this.data, update);
    },
  };

  page._appendTranscript.call(instance, {
    speaker: "user",
    text: "这是当前这一句",
    final: true,
    source: "authoritative",
    turnId: 2,
    generationId: 2,
  });

  assert.equal(data.transcript.length, 1);
  assert.equal(data.transcript[0].speaker, "user");
  assert.equal(data.transcript[0].text, "这是当前这一句");
});

test("assistant response states wait for completion instead of exposing an interrupt", () => {
  const wxml = fs.readFileSync(
    path.join(__dirname, "../pages/home/index.wxml"),
    "utf8",
  );

  assert.match(
    wxml,
    /status === 'thinking' \|\| status === 'speaking'[\s\S]*disabled[\s\S]*请等回应结束/,
  );
  assert.doesNotMatch(wxml, /bindtap="interruptVoice"|轻触打断/);
});
