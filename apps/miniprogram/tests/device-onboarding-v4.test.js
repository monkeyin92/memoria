const assert = require("node:assert/strict");
const test = require("node:test");

const { loadPage } = require("../scripts/mp-harness/runtime");
const { renderPage } = require("../scripts/mp-harness/render");
const fx = require("../scripts/mp-harness/fixtures");

const PAGE = "pages/device-onboarding/index";
const stripStyle = (html) => html.replace(/<style>[\s\S]*?<\/style>/g, "");
const visibleText = (html) => stripStyle(html).replace(/<[^>]+>/g, " ").replace(/\s+/g, " ");
const count = (body, pattern) => (body.match(pattern) || []).length;

async function renderAt(snapshot, ...events) {
  const harness = loadPage(PAGE, fx.authed());
  await harness.run();
  harness.page._applySnapshot(snapshot);
  for (const [method, event] of events) harness.page[method](event);
  const html = renderPage(PAGE, harness.page.data);
  harness.restore();
  return { body: stripStyle(html), text: visibleText(html) };
}

test("the Wi-Fi step lists networks by signal, never dBm, and marks the tapped one", async () => {
  const { body, text } = await renderAt(fx.onboardingSnapshot("wifi", { wifiNetworks: fx.WIFI_SCAN }), [
    "onWifiNetworkTap",
    { currentTarget: { dataset: { ssid: "Home-2.4G" } } },
  ]);
  assert.match(text, /Home-2\.4G 信号强 ✓ Home-Guest 开放网络 · 信号一般 楼下咖啡店 信号弱/);
  assert.equal(count(body, /wifi-row wifi-selected/g), 1);
  assert.doesNotMatch(text, /-52|WPA2/);
});

test("the stepper marks finished steps done and the Wi-Fi step current while the robot joins", async () => {
  const { body, text } = await renderAt(
    fx.onboardingSnapshot("progress", { network: { phase: "joining", joined: true, ssid: "Home-2.4G", elapsedS: 12 } }),
  );
  assert.equal(count(body, /class="step-item done"/g), 2);
  assert.equal(count(body, /class="step-item current"/g), 1);
  assert.match(text, /3 设置 Wi‑Fi 进行中/);
  assert.match(text, /✓ 网络信息已安全送达机器人 ✓ 机器人连接 Wi‑Fi「Home-2\.4G」 连接 Memoria 云端 设备身份验证通过 已等待 12 秒/);
});

test("the finish card replaces the cancel link with the way into device management", async () => {
  const { body, text } = await renderAt(fx.onboardingSnapshot("complete", { activation: { status: "ready_for_conversation" } }));
  assert.equal(count(body, /class="step-item done"/g), 5);
  assert.match(text, /已就绪 机器人可以开始对话了/);
  assert.match(text, /进入设备管理/);
  assert.doesNotMatch(text, /取消本次启用/);
});
