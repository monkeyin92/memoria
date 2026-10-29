const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const root = path.join(__dirname, "..");
const { greetingFor } = require("../utils/greeting");
const { currentUserSummary, deviceStatusSummary } = require("../utils/device-status");

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

test("online status reflects activation readiness", () => {
  const ready = deviceStatusSummary(
    { status: "ready_for_conversation" },
    {},
    { live_runtime: { connected: false, device_id: "dev_1" } },
  );
  assert.equal(ready.online, true);
  assert.equal(ready.onlineLabel, "设备在线");

  const activating = deviceStatusSummary(
    { status: "device_downloading", network: { internet: true } },
    {},
  );
  assert.equal(activating.online, false);
  assert.equal(activating.onlineLabel, "已联网，等待激活");

  const offline = deviceStatusSummary(
    { status: "failed", network: { internet: false } },
    {},
  );
  assert.equal(offline.online, false);
  assert.equal(offline.onlineLabel, "暂时离线");
});

test("home page is a status board without rooms or realtime chat", () => {
  const template = fs.readFileSync(path.join(root, "pages/home/index.wxml"), "utf8");
  const script = fs.readFileSync(path.join(root, "pages/home/index.js"), "utf8");
  assert.match(template, /Memoria|page-title">今天/);
  assert.doesNotMatch(template, /startVoice|startText|sendText|可直接对话/);
  assert.doesNotMatch(template, /先了解怎么用|openHowItWorks|家中的设备/);
  assert.match(template, /wx:if="\{\{!authenticated\}\}"[\s\S]*guestHome\.features/);
  assert.match(template, /home-hero[\s\S]*<face-badge/);
  assert.match(template, /home-today[\s\S]*近期摘要/);
  assert.doesNotMatch(template, /今日摘要/);
  assert.match(template, /home-toggle-card|安静时段/);
  assert.match(template, /unreadSafetyCopy|unreadSafetyAlert/);
  assert.doesNotMatch(template, /status-chips|glass-icon|battery|电量/);
  assert.match(template, /home-usage-chip[\s\S]*usageChipLabel/);
  assert.match(script, /subscribeAuthCleared\(\(\) => this\._enterGuestState\(\)\)/);
  assert.doesNotMatch(script, /待在设备上确认/);
  assert.doesNotMatch(script, /resolveSessionSubject/);
  assert.doesNotMatch(script, /openHowItWorks/);
  assert.doesNotMatch(script, /家中的设备/);
  assert.match(script, /readSubjectLabel/);
  assert.match(script, /buildTrialDeviceCard/);
  assert.match(script, /memoryPreviewItems/);
  assert.match(script, /SUBSCRIPTION_PUSH_ENABLED/);
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
  assert.match(template, /bindtap="editSubjectAlias"/);
  assert.match(template, /此刻是谁在用/);
});

test("profile has no voice-sample entry points after voice-clone removal", () => {
  const template = fs.readFileSync(path.join(root, "pages/profile/index.wxml"), "utf8");
  const script = fs.readFileSync(path.join(root, "pages/profile/index.js"), "utf8");
  assert.match(template, /授权与隐私说明/);
  assert.doesNotMatch(template, /对着麦克风录|上传音频|声音复刻|自定义人格与声音/);
  assert.doesNotMatch(script, /uploadVoiceSample|enrollVoiceClone|getRecorderManager/);
  assert.doesNotMatch(script, /评估/);
  assert.doesNotMatch(template, /评估/);
});

test("home hero state, usage chip and highlight read the device and summary state", () => {
  const { heroState, homeHighlight, usageChipLabel } = require("../utils/trial-device-card");
  assert.deepEqual(heroState({ online: true }), {
    heroState: "awake",
    heroAnimated: true,
    heroMood: "default",
    heroDim: false,
    heroPill: "在线",
    heroPillClass: "pill-online",
    heroLine: "轻点机器人屏幕就能聊",
  });
  const quiet = heroState({ online: true, inQuietHours: true, quietHoursLabel: "21:00 – 次日 07:00" });
  assert.equal(quiet.heroState, "quiet");
  assert.equal(quiet.heroMood, "sleepy");
  assert.equal(quiet.heroLine, "休息中 · 21:00 – 次日 07:00");
  // 离线优先：设备不在线时不管是不是安静时段都显示离线。
  const offline = heroState({ online: false, inQuietHours: true });
  assert.equal(offline.heroState, "offline");
  assert.equal(offline.heroDim, true);
  assert.equal(offline.heroPill, "离线");

  assert.equal(usageChipLabel(18, 30), "今天聊了 18 / 30 分钟");
  assert.equal(usageChipLabel(18, null), "今天聊了 18 分钟 · 未设上限");
  assert.equal(usageChipLabel(null, 30), "用量暂未统计 · 每天上限 30 分钟");
  assert.equal(usageChipLabel(null, null), "用量暂未统计");

  const previews = [{ text: "一起数了星星", dateLabel: "10 月 13 日" }];
  assert.deepEqual(homeHighlight({ todayCount: 2, todayOverview: "聊了恐龙", memoryPreviews: previews }), {
    highlightLabel: "今天聊了 2 次",
    highlightText: "聊了恐龙",
  });
  assert.deepEqual(homeHighlight({ todayCount: 0, memoryPreviews: previews }), {
    highlightLabel: "最近一次 · 10 月 13 日",
    highlightText: "一起数了星星",
  });
  assert.equal(homeHighlight({ memoryPreviews: [{ text: "x", dateLabel: "最近" }] }).highlightLabel, "最近一次");
  assert.equal(homeHighlight().highlightLabel, "今天还没聊");
});

test("trial device card helper uses profile quiet hours and usage limit", () => {
  const { buildTrialDeviceCard } = require("../utils/trial-device-card");
  const profile = {
    valid: true,
    subject_category: "minor",
    obligations: [
      { code: "QUIET_HOURS", params: { quiet_hours: ["21:00", "07:00"] } },
      { code: "MAX_SESSION_SECONDS", params: { max_session_seconds: 1800, extras: [["daily_chat_seconds", "2700"]] } },
    ],
  };
  const unknown = buildTrialDeviceCard({ online: true, runtimeProfile: profile });
  assert.equal(unknown.statusPill, "在线");
  assert.equal(unknown.usageKnown, false);
  assert.equal(unknown.usageUsedMinutes, null);
  assert.equal(unknown.usageLimitMinutes, 45);
  assert.equal(unknown.statusLine, "在线 · 已绑定");
  assert.equal(unknown.quietHoursLabel, "21:00 – 次日 07:00");
  assert.equal(unknown.nightModeOn, true);
  const unset = buildTrialDeviceCard({ online: true, runtimeProfile: { ...profile, obligations: [] } });
  assert.equal(unset.usageLimitMinutes, null);
  assert.equal(unset.quietHoursLabel, "");
  assert.equal(unset.nightModeOn, false);
  const known = buildTrialDeviceCard({
    online: true,
    runtimeProfile: profile,
    usedMinutes: 12,
  });
  assert.equal(known.usageKnown, true);
  assert.equal(known.usageUsedMinutes, 12);
});

test("parent v4 tokens live in app.wxss and pages avoid third-party art", () => {
  const appWxss = fs.readFileSync(path.join(root, "app.wxss"), "utf8");
  assert.match(appWxss, /--mp-bg:\s*#f7f2eb/i);
  assert.match(appWxss, /--mp-accent:\s*#b9481f/i);
  assert.match(appWxss, /--mp-surface:\s*#ffffff/i);
  assert.match(appWxss, /--mp-shadow-card/);
  const surfaces = [
    "pages/home/index.wxml",
    "pages/home/index.wxss",
    "pages/child-memory/index.wxml",
    "pages/child-memory/index.wxss",
    "pages/bind/index.wxml",
    "pages/bind/index.wxss",
    "pages/device/index.wxml",
    "pages/device/index.wxss",
  ];
  for (const relative of surfaces) {
    const source = fs.readFileSync(path.join(root, relative), "utf8");
    assert.doesNotMatch(source, /\/assets\/(companions|mascots)\//, relative);
    assert.doesNotMatch(source, /apple\.com|mi\.com|homekit|sf-symbol/i, relative);
    assert.doesNotMatch(source, /电池|battery/i, relative);
  }
});
