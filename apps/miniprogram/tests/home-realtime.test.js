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

test("initial connection retries once with a fresh ticket after connection refused", async () => {
  const originalRefresh = api.refreshMiniProgramGatewayTicket;
  const session = {
    session_id: "session-1",
    media_gateway: { websocket_url: "wss://voice.example.com/media", ticket: "old" },
  };
  const updates = [];
  let attempts = 0;
  const instance = {
    _session: session,
    _media: null,
    data: {},
    setData(update) {
      updates.push(update);
      Object.assign(this.data, update);
    },
    async _endMediaLocally() {
      this._media = null;
    },
    async _connectMedia(nextSession) {
      attempts += 1;
      if (attempts === 1) {
        const error = new Error("connection refused");
        error.code = "socket_connection_refused";
        throw error;
      }
      this._media = { session: nextSession };
    },
  };
  api.refreshMiniProgramGatewayTicket = async (sessionId) => {
    assert.equal(sessionId, session.session_id);
    return { websocket_url: "wss://voice.example.com/media", ticket: "fresh" };
  };

  try {
    const connected = await page._connectInitialMedia.call(instance, session);
    assert.equal(attempts, 2);
    assert.equal(connected.media_gateway.ticket, "fresh");
    assert.equal(updates[0].status, "reconnecting");
  } finally {
    api.refreshMiniProgramGatewayTicket = originalRefresh;
  }
});
