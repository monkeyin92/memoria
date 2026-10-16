const assert = require("node:assert/strict");
const test = require("node:test");

process.env.TZ = "Asia/Shanghai";

const { renderPage } = require("../scripts/mp-harness/render");
const { scenarioData } = require("../scripts/mp-harness/shoot");
const { PAGES } = require("../scripts/mp-harness/pages");

const fx = require("../scripts/mp-harness/fixtures");
const { loadPage } = require("../scripts/mp-harness/runtime");

const visibleText = (html) => html.replace(/<style>[\s\S]*?<\/style>/g, "").replace(/<[^>]+>/g, " ").replace(/\s+/g, " ");

test("the usage screen shows the real quiet window as the card chip, then the offline stories and the privacy rows", async () => {
  const scenario = PAGES.find((item) => item.slug === "device");
  const { data } = await scenarioData(scenario);
  const html = renderPage(scenario.page, data);
  const text = visibleText(html);
  assert.match(
    text,
    /安静时段 孩子睡觉的时间，机器人不陪聊 21:00–07:00 离线故事 不联网也能听 小星星的枕头 月亮不着急 大熊和森林里的悄悄话 隐私说明 管理与导出数据 解绑设备/,
  );
  assert.doesNotMatch(text, /即将上线/);
  assert.match(html, /class="mps-fill" style="width: 33\.3%;"/);
});

test("the 单次上限 slider runs from 10 to 30 and shows a signed 15 minute cap", async () => {
  const profile = {
    ...fx.RUNTIME_PROFILE,
    obligations: [
      { code: "QUIET_HOURS", params: { quiet_hours: ["21:00", "07:00"] } },
      {
        code: "MAX_SESSION_SECONDS",
        params: { max_session_seconds: 900, extras: [["daily_chat_seconds", "1800"]] },
      },
    ],
  };
  const harness = loadPage("pages/device/index", fx.authed({ getRuntimeProfile: async () => profile }));
  try {
    const data = await harness.run();
    assert.equal(data.sessionLimits.maxSessionMinutes, 15);
    assert.equal(data.limitsSliderMin, 10);
    assert.equal(data.limitsSliderMax, 30);
    assert.equal(data.limitsSliderStep, 1);
    const text = visibleText(renderPage("pages/device/index", data));
    assert.match(text, /单次上限 15 分钟 10 分钟 30 分钟/);
  } finally {
    harness.restore();
  }
});

test("releasing the 单次上限 slider past 30 saves 30 minutes with the stored quiet window", async () => {
  const saved = [];
  const harness = loadPage("pages/device/index", fx.authed({ setSessionLimits: async (...args) => saved.push(args) }));
  try {
    await harness.run();
    await harness.page.onLimitsMinutesSlider({ detail: { value: 60 } });
    assert.deepEqual(saved, [["dev_1", { maxSessionMinutes: 30, quietStart: "21:00", quietEnd: "07:00" }]]);
    assert.equal(harness.page.data.limitsSaving, false);
  } finally {
    harness.restore();
  }
});

test("the usage screen counts today's chat against the daily budget the guardian sets beside the per-session cap", async () => {
  const scenario = PAGES.find((item) => item.slug === "device");
  const { data } = await scenarioData(scenario);
  assert.equal(data.trialSettingsVisible, true);
  assert.equal(data.sessionLimits.maxSessionMinutes, 15);
  assert.equal(data.sessionLimits.dailyChatMinutes, 30);
  const text = visibleText(renderPage(scenario.page, data));
  assert.match(text, /18 \/ 30 分钟 今天还可以聊 12 分钟/);
  assert.match(text, /单次上限 15 分钟 10 分钟 30 分钟 每天上限 30 分钟 15 分钟 60 分钟 安静时段/);
  assert.doesNotMatch(text, /每日上限/);
});

test("the usage ring fills today's share of the daily budget", async () => {
  const scenario = PAGES.find((item) => item.slug === "device");
  const { data } = await scenarioData(scenario);
  const html = renderPage(scenario.page, data);
  assert.match(html, /class="device-ring"[^>]*style="[^"]*conic-gradient\(#4A52C9 0% 60%, #ECECF2 60% 100%\)/);
});

test("a missing per-session cap displays 15 minutes and the slider rests on 15", async () => {
  const quietOnly = {
    ...fx.RUNTIME_PROFILE,
    obligations: fx.RUNTIME_PROFILE.obligations.filter((item) => item.code === "QUIET_HOURS"),
  };
  const harness = loadPage("pages/device/index", fx.authed({ getRuntimeProfile: async () => quietOnly }));
  try {
    const data = await harness.run();
    assert.equal(data.sessionLimits.maxSessionMinutes, null);
    assert.equal(data.sessionMinuteValue, 15);
    const text = visibleText(renderPage("pages/device/index", data));
    assert.match(text, /15 分钟 · 单次上限/);
    assert.match(text, /单次上限 15 分钟 10 分钟 30 分钟/);
    assert.doesNotMatch(text, /未设置 单次上限|单次上限 未设置/);
  } finally {
    harness.restore();
  }
});

test("a stored cap outside 10 to 30 minutes shows 15 and does not save or prompt", async () => {
  for (const seconds of [2700, 3600, 300]) {
    const profile = {
      ...fx.RUNTIME_PROFILE,
      obligations: [
        { code: "QUIET_HOURS", params: { quiet_hours: ["21:00", "07:00"] } },
        { code: "MAX_SESSION_SECONDS", params: { max_session_seconds: seconds, extras: [] } },
      ],
    };
    const harness = loadPage("pages/device/index", fx.authed({ getRuntimeProfile: async () => profile }));
    try {
      const data = await harness.run();
      assert.equal(data.sessionLimits.maxSessionMinutes, null, `${seconds}s`);
      assert.equal(data.sessionMinuteValue, 15, `${seconds}s`);
      const html = renderPage("pages/device/index", data);
      const text = visibleText(html);
      assert.match(text, /单次上限 15 分钟 10 分钟 30 分钟/);
      assert.match(html, /class="mps-fill" style="width: 25%;"/);
      assert.doesNotMatch(text, /单次上限 (?:5|30|45|60) 分钟/);
      const writes = harness.wxCalls.filter((call) => {
        const options = call.args?.[0];
        return call.name === "request" && options?.method === "PUT" && String(options.url || "").includes("/session-limits");
      });
      assert.deepEqual(writes, []);
      const prompts = harness.wxCalls.filter((call) => call.name === "showModal" || call.name === "showToast");
      assert.deepEqual(prompts, []);
    } finally {
      harness.restore();
    }
  }
});

test("confirming a rebind sends 15 minutes, the same number an old cap displays", async () => {
  const harness = loadPage("pages/bind/index", fx.authed());
  try {
    await harness.run({ claim_id: "claim_old", onboarding_session_id: "onb_old" });
    await harness.page.chooseMode({ currentTarget: { dataset: { mode: "parent_for_child" } } });
    harness.page.setData({ "form.childNickname": "小乐", "form.subjectAlias": "孩子" });
    const request = harness.page._buildRequest();
    assert.equal(request.service_preferences.max_session_minutes, 15);
    assert.equal(harness.page.data.sessionMinuteOptions[harness.page.data.sessionMinuteIndex], 15);
  } finally {
    harness.restore();
  }
});

test("a quiet-only profile keeps the signed cap empty and shows the 15 minute fallback", async () => {
  const quietOnly = {
    ...fx.RUNTIME_PROFILE,
    obligations: fx.RUNTIME_PROFILE.obligations.filter((item) => item.code === "QUIET_HOURS"),
  };
  const harness = loadPage("pages/device/index", fx.authed({ getRuntimeProfile: async () => quietOnly }));
  try {
    const data = await harness.run();
    assert.equal(data.sessionLimits.maxSessionMinutes, null);
    const text = visibleText(renderPage("pages/device/index", data));
    assert.match(text, /15 分钟 · 单次上限 用量暂未统计/);
    assert.match(text, /单次上限 15 分钟 10 分钟 30 分钟/);
    assert.doesNotMatch(text, /未设置 单次上限|单次上限 未设置/);
  } finally {
    harness.restore();
  }
});
