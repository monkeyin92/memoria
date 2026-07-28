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

test("assistant response states expose button stop without restoring voice barge-in", () => {
  const wxml = fs.readFileSync(
    path.join(__dirname, "../pages/home/index.wxml"),
    "utf8",
  );

  assert.match(
    wxml,
    /status === 'thinking' \|\| status === 'speaking'[\s\S]*bindtap="stopPlayback"[\s\S]*停止播放/,
  );
  assert.doesNotMatch(wxml, /bindtap="interruptVoice"|轻触打断/);
});

test("button stop clears local audio before requesting the server generation cancel", async () => {
  const originalStopResponse = api.stopResponse;
  const order = [];
  api.stopResponse = async (sessionId) => {
    order.push(`server:${sessionId}`);
  };
  const data = { active: true, error: "" };
  const instance = {
    data,
    _session: { session_id: "session-1" },
    _media: {
      stopAssistantPlayback() {
        order.push("local");
      },
    },
    setData(update) {
      Object.assign(this.data, update);
    },
  };

  try {
    await page.stopPlayback.call(instance);
    assert.deepEqual(order, ["local", "server:session-1"]);
    assert.equal(data.error, "");
  } finally {
    api.stopResponse = originalStopResponse;
  }
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

test("voice start is single-flight before setData reflects connecting", async () => {
  const originalIdentity = api.currentIdentity;
  const originalHasAuthenticatedSession = api.hasAuthenticatedSession;
  const originalCreate = api.createMiniProgramSession;
  const originalWx = global.wx;
  let releaseSession;
  let createCalls = 0;
  const sessionReady = new Promise((resolve) => {
    releaseSession = resolve;
  });
  api.currentIdentity = () => ({ user_id: "user-1" });
  api.hasAuthenticatedSession = () => true;
  api.createMiniProgramSession = async () => {
    createCalls += 1;
    await sessionReady;
    return { session_id: "session-1", media_gateway: {} };
  };
  global.wx = {
    authorize({ success }) {
      success();
    },
  };
  const instance = {
    data: { connecting: false, active: false },
    setData() {},
    _resetExpression() {},
    _voiceAttemptId: 0,
    _invalidateVoiceAttempt: page._invalidateVoiceAttempt,
    _isVoiceAttemptCurrent: page._isVoiceAttemptCurrent,
    _startVoiceOnce: page._startVoiceOnce,
    async loadProfile() {},
    async _connectInitialMedia(session) {
      return session;
    },
    async _endMediaLocally() {},
  };

  try {
    const first = page.startVoice.call(instance);
    const second = page.startVoice.call(instance);
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(createCalls, 1);
    releaseSession();
    await Promise.all([first, second]);
    await page.startVoice.call(instance);
    assert.equal(createCalls, 2);
  } finally {
    api.currentIdentity = originalIdentity;
    api.hasAuthenticatedSession = originalHasAuthenticatedSession;
    api.createMiniProgramSession = originalCreate;
    global.wx = originalWx;
  }
});

test("guest transition fences a late voice start and clears private transcript state", async () => {
  let mediaClosed = 0;
  const data = {
    authenticated: true,
    active: true,
    connecting: true,
    transcript: [{ speaker: "user", text: "私密内容" }],
  };
  const instance = {
    data,
    _voiceAttemptId: 1,
    _session: { session_id: "session-1" },
    setData(update) {
      Object.assign(this.data, update);
    },
    _invalidateVoiceAttempt: page._invalidateVoiceAttempt,
    _resetExpression() {},
    async _endMediaLocally() {
      mediaClosed += 1;
    },
  };

  page._enterGuestState.call(instance);
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(instance._voiceAttemptId, 2);
  assert.equal(instance._session, null);
  assert.equal(mediaClosed, 1);
  assert.equal(data.authenticated, false);
  assert.equal(data.active, false);
  assert.deepEqual(data.transcript, []);
});
