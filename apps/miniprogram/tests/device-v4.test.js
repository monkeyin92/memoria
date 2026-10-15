const assert = require("node:assert/strict");
const test = require("node:test");

process.env.TZ = "Asia/Shanghai";

const { renderPage } = require("../scripts/mp-harness/render");
const { scenarioData } = require("../scripts/mp-harness/shoot");
const { PAGES } = require("../scripts/mp-harness/pages");

const fx = require("../scripts/mp-harness/fixtures");
const { loadPage } = require("../scripts/mp-harness/runtime");

const visibleText = (html) => html.replace(/<style>[\s\S]*?<\/style>/g, "").replace(/<[^>]+>/g, " ").replace(/\s+/g, " ");

test("the usage screen shows the real quiet window as the card chip and the privacy rows below", async () => {
  const scenario = PAGES.find((item) => item.slug === "device");
  const { data } = await scenarioData(scenario);
  const html = renderPage(scenario.page, data);
  const text = visibleText(html);
  assert.match(text, /安静时段 孩子睡觉的时间，机器人不陪聊 21:00–07:00 隐私说明 管理与导出数据 解绑设备/);
  assert.doesNotMatch(text, /即将上线/);
  assert.match(html, /class="mps-fill" style="width: 33\.3%;"/);
});

test("releasing the 单次上限 slider at 60 saves 60 minutes with the stored quiet window", async () => {
  const saved = [];
  const harness = loadPage("pages/device/index", fx.authed({ setSessionLimits: async (...args) => saved.push(args) }));
  try {
    await harness.run();
    await harness.page.onLimitsMinutesSlider({ detail: { value: 60 } });
    assert.deepEqual(saved, [["dev_1", { maxSessionMinutes: 60, quietStart: "21:00", quietEnd: "07:00" }]]);
    assert.equal(harness.page.data.limitsSaving, false);
  } finally {
    harness.restore();
  }
});

test("the usage slider names the per-session limit the device enforces, never a daily cap", async () => {
  const scenario = PAGES.find((item) => item.slug === "device");
  const { data } = await scenarioData(scenario);
  assert.equal(data.trialSettingsVisible, true);
  assert.equal(data.sessionLimits.maxSessionMinutes, 30);
  const text = visibleText(renderPage(scenario.page, data));
  assert.match(text, /单次上限 30 分钟/);
  assert.doesNotMatch(text, /每日上限|每天上限/);
});

test("the usage screen reads 未设置 when the signed profile has quiet hours but no cap", async () => {
  const quietOnly = {
    ...fx.RUNTIME_PROFILE,
    obligations: fx.RUNTIME_PROFILE.obligations.filter((item) => item.code === "QUIET_HOURS"),
  };
  const harness = loadPage("pages/device/index", fx.authed({ getRuntimeProfile: async () => quietOnly }));
  try {
    const data = await harness.run();
    assert.equal(data.sessionLimits.maxSessionMinutes, null);
    const text = visibleText(renderPage("pages/device/index", data));
    assert.match(text, /未设置 单次上限 用量暂未统计/);
    assert.match(text, /单次上限 未设置 15 分钟 60 分钟/);
    assert.doesNotMatch(text, /\b30\b/);
  } finally {
    harness.restore();
  }
});
