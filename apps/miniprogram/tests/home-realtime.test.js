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

test("speaking state exposes one explicit local-first interrupt control", () => {
  const wxml = fs.readFileSync(
    path.join(__dirname, "../pages/home/index.wxml"),
    "utf8",
  );

  assert.match(
    wxml,
    /wx:if="\{\{status === 'speaking'\}\}"[\s\S]*bindtap="interruptVoice"[\s\S]*轻触打断/,
  );
});

test("explicit interrupt stops local playout before notifying the server", async () => {
  const events = [];
  const originalStopResponse = api.stopResponse;
  api.stopResponse = async (sessionId) => {
    events.push(`server:${sessionId}`);
  };
  const data = {
    active: true,
    status: "speaking",
    statusLabel: "正在回应",
    error: "旧错误",
    interrupting: false,
  };
  const instance = {
    data,
    _session: { session_id: "session-1" },
    _media: {
      interruptPlayback() {
        events.push("local");
      },
    },
    _interrupting: false,
    setData(update) {
      Object.assign(this.data, update);
    },
    _setStatus: page._setStatus,
  };

  try {
    await page.interruptVoice.call(instance);
  } finally {
    api.stopResponse = originalStopResponse;
  }

  assert.deepEqual(events, ["local", "server:session-1"]);
  assert.equal(data.status, "listening");
  assert.equal(data.statusLabel, "我在听");
  assert.equal(data.error, "");
  assert.equal(data.interrupting, false);
  assert.equal(instance._interrupting, false);
});
