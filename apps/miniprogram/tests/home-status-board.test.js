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
const { deviceStatusSummary } = require("../utils/device-status");

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

test("online status copy is wake-oriented, not in-app chat", () => {
  const ready = deviceStatusSummary({ status: "ready_for_conversation" }, {});
  assert.equal(ready.onlineLabel, "在线，可以唤醒");
  assert.doesNotMatch(ready.onlineLabel, /可直接对话/);
  const offline = deviceStatusSummary({ status: "failed", network: { internet: false } }, {});
  assert.equal(offline.onlineLabel, "离线，请检查电源和网络");
});

test("home page is a status board without rooms or realtime chat", () => {
  const template = fs.readFileSync(path.join(root, "pages/home/index.wxml"), "utf8");
  const script = fs.readFileSync(path.join(root, "pages/home/index.js"), "utf8");
  assert.match(template, /对话在设备上完成/);
  assert.match(template, /card-heading">Memoria/);
  assert.match(template, /陪伴机器人随人移动，不按房间固定/);
  assert.doesNotMatch(template, /客厅|卧室|书房/);
  assert.doesNotMatch(template, /startVoice|startText|sendText|可直接对话/);
  assert.match(template, /wx:if="\{\{!authenticated\}\}"[\s\S]*登录后照看你的机器人/);
  assert.match(script, /subscribeAuthCleared\(\(\) => this\._enterGuestState\(\)\)/);
});

test("device wake word copy tells users it syncs then restarts", () => {
  const template = fs.readFileSync(path.join(root, "pages/device/index.wxml"), "utf8");
  assert.match(template, /在线同步到设备/);
  assert.match(template, /重启后生效/);
  assert.doesNotMatch(template, /下次连接生效/);
  assert.match(template, /不按客厅、卧室这类房间来标记/);
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
});
