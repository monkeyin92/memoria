const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const root = path.join(__dirname, "..");
const {
  encodeCustomPersona,
  parseCustomPersona,
} = require("../utils/custom-persona");
const { greetingFor } = require("../utils/greeting");
const { currentUserSummary, deviceStatusSummary } = require("../utils/device-status");

test("custom persona round-trips through the profile bio marker", () => {
  const encoded = encodeCustomPersona({ name: "小黑", text: "说话短一点，陪我散步。" });
  assert.match(encoded, /\[memoria\.custom_persona\.v1\]/);
  assert.deepEqual(parseCustomPersona(encoded), {
    active: true,
    name: "小黑",
    text: "说话短一点，陪我散步。",
  });
  assert.equal(parseCustomPersona("喜欢散步").active, false);
  assert.equal(encodeCustomPersona({ name: "", text: "x" }), "");
});

test("home greeting bands do not invent a room or a master title", () => {
  assert.equal(greetingFor(new Date(2026, 8, 5, 7)), "早上好");
  assert.equal(greetingFor(new Date(2026, 8, 5, 15)), "下午好");
  assert.equal(greetingFor(new Date(2026, 8, 5, 21)), "晚上好");
  assert.equal(greetingFor(new Date(2026, 8, 5, 23)), "夜深了");
});

test("online status copy is device-oriented, not in-app chat", () => {
  const ready = deviceStatusSummary({ status: "ready_for_conversation" }, {});
  assert.equal(ready.onlineLabel, "设备在线");
  assert.doesNotMatch(ready.onlineLabel, /可直接对话/);
  const offline = deviceStatusSummary({ status: "failed", network: { internet: false } }, {});
  assert.equal(offline.onlineLabel, "暂时离线");
});

test("online status distinguishes activation readiness from live connection", () => {
  // 设备已激活，但未连接（关机或断网）：必须显示暂时离线
  const poweredOff = deviceStatusSummary(
    { status: "ready_for_conversation" },
    {},
    { live_runtime: { connected: false, device_id: "dev_1" } },
  );
  assert.equal(poweredOff.online, false);
  assert.equal(poweredOff.onlineLabel, "暂时离线");

  // 设备已激活，且 WebSocket 已连接：显示设备在线
  const connected = deviceStatusSummary(
    { status: "ready_for_conversation" },
    {},
    { live_runtime: { connected: true, stream_epoch: 2 } },
  );
  assert.equal(connected.online, true);
  assert.equal(connected.onlineLabel, "设备在线");

  // 诊断接口失败或 Edge 不可用时，fail closed，不谎报在线
  const diagUnavailable = deviceStatusSummary(
    { status: "ready_for_conversation" },
    {},
    null,
  );
  assert.equal(diagUnavailable.online, false);
  assert.equal(diagUnavailable.onlineLabel, "状态待同步");

  // diagnostics 内部 live_runtime 为 null（Edge 未连上）
  const edgeDown = deviceStatusSummary(
    { status: "ready_for_conversation" },
    {},
    { live_runtime: null },
  );
  assert.equal(edgeDown.online, false);
  assert.equal(edgeDown.onlineLabel, "状态待同步");

  // 设备未完成激活，即使连接上也不能算在线
  const activating = deviceStatusSummary(
    { status: "device_downloading", network: { internet: true } },
    {},
    { live_runtime: { connected: true } },
  );
  assert.equal(activating.online, false);
  assert.equal(activating.onlineLabel, "已联网，等待激活");
});

test("home page is a status board without rooms or realtime chat", () => {
  const template = fs.readFileSync(path.join(root, "pages/home/index.wxml"), "utf8");
  const script = fs.readFileSync(path.join(root, "pages/home/index.js"), "utf8");
  assert.match(template, /欢迎来到 Memoria|page-title">\{\{greeting\}\}/);
  assert.doesNotMatch(template, /startVoice|startText|sendText|可直接对话/);
  assert.match(template, /wx:if="\{\{!authenticated\}\}"[\s\S]*登录后查看设备和今天/);
  assert.match(script, /subscribeAuthCleared\(\(\) => this\._enterGuestState\(\)\)/);
  assert.doesNotMatch(script, /待在设备上确认/);
  assert.doesNotMatch(script, /resolveSessionSubject/);
  assert.match(script, /readSubjectLabel/);
});

test("current user on home is the bind-time remark, not the WeChat name", () => {
  assert.equal(currentUserSummary({ subjectLabel: "老爸" }), "老爸");
  assert.equal(currentUserSummary({ subjectLabel: "亲爱的儿子" }), "亲爱的儿子");
  assert.equal(currentUserSummary({}), "未设置");
  assert.equal(currentUserSummary({ subjectLabel: "   " }), "未设置");
});

test("device wake word copy tells users it syncs then restarts", () => {
  const template = fs.readFileSync(path.join(root, "pages/device/index.wxml"), "utf8");
  assert.match(template, /在线同步到设备/);
  assert.match(template, /重启后生效/);
  assert.doesNotMatch(template, /下次连接生效/);
  assert.match(template, /不按客厅、卧室这类房间来标记/);
  assert.match(template, /使用者备注/);
  assert.match(template, /保存备注/);
  assert.match(template, /此刻是谁在用/);
});

test("profile supports catalog and custom persona plus voice sample", () => {
  const template = fs.readFileSync(path.join(root, "pages/profile/index.wxml"), "utf8");
  const script = fs.readFileSync(path.join(root, "pages/profile/index.js"), "utf8");
  assert.match(template, /人格与声音/);
  assert.match(template, /自定义人格与声音/);
  assert.match(template, /对着麦克风录/);
  assert.match(template, /上传音频/);
  assert.match(script, /encodeCustomPersona/);
  assert.match(script, /enrollVoiceClone/);
  assert.match(script, /getRecorderManager/);
  assert.match(script, /readyForDevice:\s*true/);
  assert.match(script, /readyVoiceForDevice/);
  assert.match(script, /正在生成自定义声音，大约一分钟/);
  assert.doesNotMatch(script, /等待服务端评估/);
  assert.doesNotMatch(template, /评估通过前/);
});
