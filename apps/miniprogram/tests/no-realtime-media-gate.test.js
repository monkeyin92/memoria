const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const root = path.join(__dirname, "..");

const PRODUCTION_ROOTS = [
  path.join(root, "app.json"),
  path.join(root, "app.js"),
  path.join(root, "app.wxss"),
  path.join(root, "config.js"),
  path.join(root, "sitemap.json"),
  path.join(root, "pages"),
  path.join(root, "utils"),
];

const EXCLUDED = new Set(["node_modules", "tests", "generated"]);

function productionSources() {
  const files = [];
  for (const entry of PRODUCTION_ROOTS) {
    const stat = fs.statSync(entry);
    if (stat.isFile()) {
      files.push(entry);
    } else {
      const walk = (directory) => {
        for (const name of fs.readdirSync(directory)) {
          if (EXCLUDED.has(name)) continue;
          const target = path.join(directory, name);
          const targetStat = fs.statSync(target);
          if (targetStat.isDirectory()) walk(target);
          else files.push(target);
        }
      };
      walk(entry);
    }
  }
  return files;
}

function matches(files, pattern, description) {
  const hits = [];
  for (const file of files) {
    const source = fs.readFileSync(file, "utf8");
    if (pattern.test(source)) hits.push(file);
  }
  assert.deepEqual(hits, [], `${description} 不得重新进入生产包：${hits.join(", ")}`);
}

test("production package contains no realtime voice surface at all", () => {
  const files = productionSources();
  assert.ok(files.length > 20, "生产包扫描应覆盖 pages 与 utils");

  // §4.5 静态门禁：实时对话、媒体 WSS 与实时音频播放一律禁止。
  // 自定义声音样本允许在「我的」页使用有界录音，不在此一律封死。
  matches(files, /wx\.connectSocket|connectSocket\s*\(/, "媒体 WSS 连接");
  matches(files, /wss:\/\//, "媒体 Gateway WSS 地址");
  matches(files, /MiniProgramMediaSession/, "小程序媒体会话");
  matches(files, /MINIPROGRAM_MEDIA_GATEWAY_URL/, "媒体网关环境变量");
  matches(files, /createWebAudioContext|createInnerAudioContext|InnerAudioContext/, "实时 TTS 播放");
  matches(files, /require\(["'][^"']*(media-gateway|media-protocol|pcm-player|transcript-events)["']\)/, "媒体工具依赖");
  matches(files, /bindtap="(startVoice|startText|stopVoice|retryVoice|sendText)"/, "实时会话入口");
});

test("test sources are explicitly excluded from the WeChat package", () => {
  const privateConfig = path.join(root, "project.config.json");
  const projectConfig = fs.existsSync(privateConfig)
    ? privateConfig
    : path.join(root, "project.config.example.json");
  const project = JSON.parse(
    fs.readFileSync(projectConfig, "utf8"),
  );
  const ignoredFolders = new Set(
    (project.packOptions?.ignore || [])
      .filter((entry) => entry.type === "folder")
      .map((entry) => entry.value),
  );
  assert.ok(ignoredFolders.has("tests"), "tests 必须排除出微信生产包");
});

test("cold start, memory, home and device surfaces never create a RecorderManager", () => {
  const scoped = [
    path.join(root, "app.js"),
    path.join(root, "pages", "home", "index.js"),
    ...fs.readdirSync(path.join(root, "pages", "memory")).map((name) =>
      path.join(root, "pages", "memory", name),
    ),
    ...fs.readdirSync(path.join(root, "pages", "device")).map((name) =>
      path.join(root, "pages", "device", name),
    ),
  ];
  matches(scoped, /getRecorderManager/, "冷启动/首页/回顾/设备页创建 RecorderManager");
});

test("RecorderManager is limited to custom voice samples on the profile page", () => {
  const files = productionSources();
  const hits = files
    .filter((file) => /getRecorderManager|RecorderManager/.test(fs.readFileSync(file, "utf8")))
    .map((file) => path.relative(root, file));
  assert.deepEqual(hits, ["pages/profile/index.js"]);
  const recordHits = files
    .filter((file) => /scope\.record/.test(fs.readFileSync(file, "utf8")))
    .map((file) => path.relative(root, file))
    .sort();
  assert.deepEqual(recordHits, ["app.json", "pages/profile/index.js"]);
});

test("phone voiceprint enrollment page is fully removed", () => {
  // §4.5 / PR-02：声纹录取页面与入口一并删除，页面注册、测试与 API 均不得残留。
  assert.equal(
    fs.existsSync(path.join(root, "pages", "speaker-enrollment")),
    false,
    "speaker-enrollment 页面目录必须删除",
  );
  const appConfig = JSON.parse(fs.readFileSync(path.join(root, "app.json"), "utf8"));
  assert.equal(
    appConfig.pages.includes("pages/speaker-enrollment/index"),
    false,
    "app.json 不得注册 speaker-enrollment 页面",
  );
  assert.equal(
    fs.existsSync(path.join(root, "tests", "speaker-enrollment.test.js")),
    false,
    "speaker-enrollment 测试必须删除",
  );
  const apiSource = fs.readFileSync(path.join(root, "utils", "api.js"), "utf8");
  assert.doesNotMatch(apiSource, /enrollSpeakerProfiles|\/v1\/speakers\/enrollments/, "api.js 不得保留声纹上传函数");
});

test("deleted media modules stay deleted", () => {
  for (const name of [
    "media-gateway.js",
    "media-protocol.js",
    "pcm-player.js",
    "transcript-events.js",
  ]) {
    assert.equal(
      fs.existsSync(path.join(root, "utils", name)),
      false,
      `utils/${name} 已退出生产包，不得重建`,
    );
  }
});

test("control api exposes no miniprogram media session endpoints", () => {
  const apiPath = path.join(root, "utils", "api.js");
  const source = fs.readFileSync(apiPath, "utf8");
  assert.doesNotMatch(source, /createMiniProgramSession|refreshMiniProgramGatewayTicket|stopResponse|notifyRtcRecovered/);
  const api = require(apiPath);
  for (const name of [
    "createMiniProgramSession",
    "refreshMiniProgramGatewayTicket",
    "stopResponse",
    "notifyRtcRecovered",
  ]) {
    assert.equal(api[name], undefined, `api.${name} 必须不存在`);
  }
});

test("home status board has no realtime voice dashboard", () => {
  assert.equal(fs.existsSync(path.join(root, "pages", "home", "index.wxml")), true);
  const appConfig = JSON.parse(fs.readFileSync(path.join(root, "app.json"), "utf8"));
  assert.equal(appConfig.pages.includes("pages/home/index"), true);
  const template = fs.readFileSync(path.join(root, "pages/home/index.wxml"), "utf8");
  const script = fs.readFileSync(path.join(root, "pages/home/index.js"), "utf8");
  assert.doesNotMatch(template, /startVoice|startText|sendText|可直接对话/);
  assert.doesNotMatch(script, /getRecorderManager|startVoice|connectSocket/);
});
