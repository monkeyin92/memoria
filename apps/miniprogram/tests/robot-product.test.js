const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const { loadPage } = require("../scripts/mp-harness/runtime");
const { renderPage } = require("../scripts/mp-harness/render");
const { shippedFiles } = require("../scripts/package-size");
const { companions, companionById, defaultCompanionId } = require("../utils/companions");
const fx = require("../scripts/mp-harness/fixtures");

const root = path.join(__dirname, "..");
const visibleText = (html) => html.replace(/<head>[\s\S]*?<\/head>/g, "").replace(/<[^>]+>/g, " ").replace(/\s+/g, " ");

test("legacy role keys resolve to one default voice presentation without body metadata", () => {
  assert.equal(companions.length, 1);
  for (const identifier of [defaultCompanionId, "taoxi", "mianmian", "axu", "xuanmo", "cu_0123", "unknown"]) {
    const companion = companionById(identifier);
    assert.equal(companion.voiceId, "warm_companion");
    assert.equal(companion.voiceName, "默认声音");
    assert.equal(companion.name, "Memoria");
    assert.equal(companion.image, undefined);
    assert.equal(companion.bodyStyleId, undefined);
  }
});

test("pages render the robot through face-badge and device-screen, not loose asset paths", () => {
  for (const page of require("../app.json").pages) {
    const template = fs.readFileSync(path.join(root, `${page}.wxml`), "utf8");
    assert.doesNotMatch(template, /\/assets\/(?:mascots|companions|robots)\//, page);
  }
  const home = fs.readFileSync(path.join(root, "pages/home/index.wxml"), "utf8");
  const companion = fs.readFileSync(path.join(root, "pages/companion/index.wxml"), "utf8");
  assert.match(home, /<face-badge/);
  assert.match(home, /<device-screen/);
  assert.match(companion, /<face-badge/);
});

test("the production package keeps retired companion art and old voice samples out", () => {
  const shipped = shippedFiles();
  assert.equal(shipped.some((file) => /^assets\/(?:companions|robots|voices)\//.test(file)), false);
  assert.equal(shipped.some((file) => /^components\/face-badge\//.test(file)), true);
  assert.equal(shipped.some((file) => /^components\/device-screen\//.test(file)), true);
  assert.equal(shipped.some((file) => /^assets\/mascots\/starlight\//.test(file)), true);
});

test("the home status card keeps device identity, connection state, and working device navigation", async () => {
  for (const online of [true, false]) {
    const harness = loadPage("pages/home/index", fx.authed({
      ...(online
        ? {}
        : {
            getActivationStatus: async () => ({ status: "failed", network: { internet: false } }),
          }),
      getDeviceDiagnostics: async () => ({ live_runtime: { connected: online } }),
    }));
    try {
      const data = await harness.run();
      assert.equal(data.online, online);
      const html = renderPage("pages/home/index", data, { eventHandlers: true });
      const text = visibleText(html);
      assert.ok(text.includes(`${data.devicePlaceName} · ${online ? "在线" : "离线"}`));
      assert.match(text, /今日聊天用量/);
      assert.match(html, /data-handler="openDevice"/);
      harness.page.openDevice();
      assert.equal(harness.wxCalls.filter((call) => call.name === "switchTab").at(-1)?.args[0].url, "/pages/device/index");
    } finally {
      harness.restore();
    }
  }
});

test("the companion tab lists the default voice with a sample preview and the current pick", async () => {
  const harness = loadPage("pages/companion/index", fx.guest());
  try {
    const data = await harness.run();
    const text = visibleText(renderPage("pages/companion/index", data));
    assert.match(text, /Memoria/);
    assert.match(text, /已选/);
    assert.match(text, /试听/);
    assert.doesNotMatch(text, /星澜|桃喜|绵绵|阿序|玄墨/);
    assert.equal(typeof harness.page.chooseCompanion, "function");
    assert.equal(typeof harness.page.togglePreview, "function");
    assert.equal(require("../utils/tab-routes").isTabRoute("/pages/companion/index"), true);
  } finally {
    harness.restore();
  }
});

test("binding has no role picker or prerecorded sample and submits the default key", async () => {
  const harness = loadPage("pages/bind/index", fx.authed(fx.bindApi));
  try {
    await harness.run(fx.BIND_QUERY);
    await harness.page.chooseMode({ currentTarget: { dataset: { mode: "parent_for_child" } } });
    await harness.settle();
    const text = visibleText(renderPage("pages/bind/index", harness.page.data));
    assert.match(text, /默认声音 所有机器人共用/);
    assert.doesNotMatch(text, /试听|已选择|选这个|星澜|桃喜/);
    assert.equal(harness.page.onPersonaTap, undefined);
    harness.page.setData({ "form.childNickname": "乐乐" });
    await harness.page.goToReview();
    assert.equal(harness.page._buildRequest().persona_selection, "starlight");
    assert.match(visibleText(renderPage("pages/bind/index", harness.page.data)), /声音 默认声音/);
  } finally {
    harness.restore();
  }
});

test("custom expression management remains reachable from device assignment without changing voices", async () => {
  const harness = loadPage("pages/device/index", fx.authed());
  try {
    await harness.run();
    harness.page.setData({ personaSheetVisible: true, sheetOpen: true, personaSaving: false });
    const html = renderPage("pages/device/index", harness.page.data, { eventHandlers: true });
    assert.match(html, /data-handler="openCustomPersona"/);
    assert.match(visibleText(html), /创建或管理自定义表达/);
    harness.page.openCustomPersona();
    assert.equal(harness.page.data.personaSheetVisible, false);
    assert.equal(harness.page.data.sheetOpen, false);
    assert.equal(harness.wxCalls.filter((call) => call.name === "navigateTo").at(-1)?.args[0].url, "/pages/persona-custom/index");
    const count = harness.wxCalls.filter((call) => call.name === "navigateTo").length;
    harness.page.setData({ personaSaving: true });
    harness.page.openCustomPersona();
    assert.equal(harness.wxCalls.filter((call) => call.name === "navigateTo").length, count);
  } finally {
    harness.restore();
  }
});
