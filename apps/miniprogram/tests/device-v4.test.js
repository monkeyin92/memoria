const assert = require("node:assert/strict");
const test = require("node:test");

process.env.TZ = "Asia/Shanghai";

const { renderPage } = require("../scripts/mp-harness/render");
const { scenarioData } = require("../scripts/mp-harness/shoot");
const { PAGES } = require("../scripts/mp-harness/pages");

const visibleText = (html) => html.replace(/<style>[\s\S]*?<\/style>/g, "").replace(/<[^>]+>/g, " ").replace(/\s+/g, " ");

test("the usage slider names the per-session limit the device enforces, never a daily cap", async () => {
  const scenario = PAGES.find((item) => item.slug === "device");
  const { data } = await scenarioData(scenario);
  assert.equal(data.trialSettingsVisible, true);
  assert.equal(data.sessionLimits.maxSessionMinutes, 30);
  const text = visibleText(renderPage(scenario.page, data));
  assert.match(text, /单次上限 30 分钟/);
  assert.doesNotMatch(text, /每日上限|每天上限/);
});
