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
    inputMode: "voice",
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

test("text mode keeps recent turns instead of replacing the conversation", () => {
  const data = {
    inputMode: "text",
    transcript: [
      {
        key: "user:1:1",
        speaker: "user",
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
    speaker: "assistant",
    text: "新的回复",
    final: true,
    source: "authoritative",
    turnId: 1,
    generationId: 1,
  });

  assert.equal(data.transcript.length, 2);
  assert.equal(data.transcript[1].text, "新的回复");
});

test("home exposes one voice start/end action and a separate text alternative", () => {
  const wxml = fs.readFileSync(
    path.join(__dirname, "../pages/home/index.wxml"),
    "utf8",
  );

  assert.equal((wxml.match(/bindtap="startVoice"/g) || []).length, 1);
  assert.equal((wxml.match(/bindtap="stopVoice"/g) || []).length, 1);
  assert.match(wxml, /bindtap="startText"[\s\S]*使用文字对话/);
  assert.match(wxml, /bindinput="onTextDraftInput"/);
  assert.doesNotMatch(wxml, /bindtap="toggleMic"|bindtap="stopPlayback"/);
});

test("text send uses the media gateway without microphone controls", () => {
  const sent = [];
  const data = {
    active: true,
    inputMode: "text",
    textDraft: "  今天星期几？  ",
    error: "",
  };
  const instance = {
    data,
    _media: {
      sendText(text) {
        sent.push(text);
        return true;
      },
    },
    setData(update) {
      Object.assign(this.data, update);
    },
  };

  page.sendText.call(instance);

  assert.deepEqual(sent, ["今天星期几？"]);
  assert.equal(data.textDraft, "");
  assert.equal(data.error, "");
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
    _startConversation: page._startConversation,
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

test("text start skips microphone authorization", async () => {
  const originalIdentity = api.currentIdentity;
  const originalHasAuthenticatedSession = api.hasAuthenticatedSession;
  const originalCreate = api.createMiniProgramSession;
  const originalWx = global.wx;
  let authorizeCalls = 0;
  api.currentIdentity = () => ({ user_id: "user-1" });
  api.hasAuthenticatedSession = () => true;
  api.createMiniProgramSession = async () => ({
    session_id: "session-text",
    media_gateway: {},
  });
  global.wx = {
    authorize() {
      authorizeCalls += 1;
    },
  };
  const data = { connecting: false, active: false, inputMode: "voice" };
  const instance = {
    data,
    setData(update) {
      Object.assign(this.data, update);
    },
    _voiceAttemptId: 0,
    _invalidateVoiceAttempt: page._invalidateVoiceAttempt,
    _isVoiceAttemptCurrent: page._isVoiceAttemptCurrent,
    _startConversation: page._startConversation,
    _startVoiceOnce: page._startVoiceOnce,
    _resetExpression() {},
    async loadProfile() {},
    async _connectInitialMedia(session, inputMode) {
      assert.equal(inputMode, "text");
      return session;
    },
    async _endMediaLocally() {},
  };

  try {
    await page.startText.call(instance);
    assert.equal(authorizeCalls, 0);
    assert.equal(data.inputMode, "text");
    assert.equal(data.micEnabled, false);
    assert.equal(data.active, true);
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

test("assistant expression follows the active speaking fence and clears on completion", () => {
  const data = {
    status: "thinking",
    statusLabel: "正在想",
    expression: "neutral",
  };
  const instance = {
    data,
    _session: { session_id: "session-1" },
    _assistantStateFence: { turnId: 1, generationId: 1 },
    setData(update) {
      Object.assign(this.data, update);
    },
    _activateAssistantExpression: page._activateAssistantExpression,
    _clearAssistantExpression: page._clearAssistantExpression,
  };

  page._onAssistantExpression.call(instance, {
    type: "assistant_expression",
    session_id: "session-1",
    expression: "caring",
    turn_id: 1,
    generation_id: 1,
    tool_epoch: 0,
  });
  assert.equal(instance._pendingAssistantExpression.expression, "caring");

  page._setStatus.call(instance, "speaking", {
    turn_id: 1,
    generation_id: 1,
  });
  assert.equal(data.expression, "caring");

  page._setStatus.call(instance, "listening", {
    turn_id: 1,
    generation_id: 1,
  });
  assert.equal(data.expression, "neutral");

  page._onAssistantExpression.call(instance, {
    type: "assistant_expression",
    session_id: "session-1",
    expression: "happy",
    turn_id: 1,
    generation_id: 1,
    tool_epoch: 0,
  });
  assert.equal(instance._pendingAssistantExpression, null);
});

test("media close and interruption reset a prior assistant expression", async () => {
  const data = { active: true, status: "speaking", statusLabel: "正在回应", error: "" };
  let resetCalls = 0;
  const instance = {
    data,
    _ending: false,
    setData(update) {
      Object.assign(this.data, update);
    },
    _resetExpression() {
      resetCalls += 1;
    },
    async _endMediaLocally() {},
  };

  page._onMediaClosed.call(instance);
  assert.equal(resetCalls, 1);
  await page._onMediaInterrupted.call(instance, "录音被系统中断");
  assert.equal(resetCalls, 2);
});
