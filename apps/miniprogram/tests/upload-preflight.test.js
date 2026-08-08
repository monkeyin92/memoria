const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const test = require("node:test");
const {
  assertCompatibleMiniprogramCiRuntime,
} = require("../../../scripts/miniprogram_ci_runtime");

const repoRoot = path.resolve(__dirname, "../../..");
const uploadScript = path.join(repoRoot, "scripts/upload_miniprogram_test.js");
const projectConfigPath = path.join(repoRoot, "apps/miniprogram/project.config.json");
const projectConfigExamplePath = path.join(
  repoRoot,
  "apps/miniprogram/project.config.example.json",
);

function ensureCompileProjectConfig() {
  if (fs.existsSync(projectConfigPath)) return () => {};

  const projectConfig = JSON.parse(fs.readFileSync(projectConfigExamplePath, "utf8"));
  projectConfig.appid = "wx20a3a044b52fcbb7";
  fs.writeFileSync(projectConfigPath, `${JSON.stringify(projectConfig, null, 2)}\n`);
  return () => fs.rmSync(projectConfigPath, { force: true });
}

function runDryRunWithPreload(preloadSource, { guardPlatformWrite = false } = {}) {
  const temporaryDirectory = fs.mkdtempSync(
    path.join(os.tmpdir(), "memoria-miniprogram-upload-test-"),
  );
  const privateKeyPath = path.join(temporaryDirectory, "private.key");
  const preloadPath = path.join(temporaryDirectory, "broken-web-storage.cjs");
  const vendorLoadPath = path.join(temporaryDirectory, "vendor-loads.txt");
  const platformWritePath = path.join(temporaryDirectory, "platform-writes.txt");
  fs.writeFileSync(privateKeyPath, "local-test-placeholder\n", { mode: 0o600 });
  const platformGuard = guardPlatformWrite
    ? `
const fs = require("node:fs");
const Module = require("node:module");
const originalLoad = Module._load;
Module._load = function(request, parent, isMain) {
  const loaded = originalLoad.call(this, request, parent, isMain);
  if (request !== "miniprogram-ci") return loaded;
  fs.appendFileSync(process.env.MEMORIA_MINIPROGRAM_VENDOR_LOADS, "loaded\\n");
  return new Proxy(loaded, {
    get(target, property, receiver) {
      if (property === "upload" || property === "innerUpload") {
        return () => {
          fs.appendFileSync(
            process.env.MEMORIA_MINIPROGRAM_PLATFORM_WRITES,
            String(property) + "\\n",
          );
          throw new Error("compile-only dry-run crossed the platform write boundary");
        };
      }
      return Reflect.get(target, property, receiver);
    },
  });
};
`
    : "";
  fs.writeFileSync(
    preloadPath,
    `Object.defineProperty(process.versions, "node", { value: "24.16.0" });\n${preloadSource}\n${platformGuard}`,
  );
  const cleanupProjectConfig = ensureCompileProjectConfig();

  try {
    const result = spawnSync(
      process.execPath,
      [
        "--require",
        preloadPath,
        uploadScript,
        "--version",
        "0.0.0-preflight",
        "--desc",
        "本地编译预检",
        "--dry-run",
      ],
      {
        cwd: repoRoot,
        encoding: "utf8",
        env: {
          ...process.env,
          MINIPROGRAM_CI_PRIVATE_KEY: privateKeyPath,
          MEMORIA_MINIPROGRAM_VENDOR_LOADS: vendorLoadPath,
          MEMORIA_MINIPROGRAM_PLATFORM_WRITES: platformWritePath,
        },
        timeout: 30_000,
      },
    );
    result.vendorLoads = fs.existsSync(vendorLoadPath)
      ? fs.readFileSync(vendorLoadPath, "utf8").trim().split("\n").filter(Boolean)
      : [];
    result.platformWrites = fs.existsSync(platformWritePath)
      ? fs.readFileSync(platformWritePath, "utf8").trim().split("\n").filter(Boolean)
      : [];
    return result;
  } finally {
    cleanupProjectConfig();
    fs.rmSync(temporaryDirectory, { force: true, recursive: true });
  }
}

function combinedOutput(result) {
  return `${result.stdout || ""}${result.stderr || ""}`;
}

test("upload runtime gate rejects incompatible Node and storage before vendor code", async () => {
  await assert.rejects(
    assertCompatibleMiniprogramCiRuntime(null, "19.9.0"),
    /Node\.js 19\.9\.0 不受小程序发布工具支持/,
  );
  await assert.doesNotReject(assertCompatibleMiniprogramCiRuntime(null, "20.0.0"));
  await assert.doesNotReject(assertCompatibleMiniprogramCiRuntime(null, "24.16.0"));
  await assert.rejects(
    assertCompatibleMiniprogramCiRuntime(null, "25.0.0"),
    /Node\.js 25\.0\.0 不受小程序发布工具支持/,
  );
  const probedKeys = [];
  await assertCompatibleMiniprogramCiRuntime(
    { getItem(key) { probedKeys.push(key); return null; } },
    "24.16.0",
  );
  assert.deepEqual(probedKeys, [
    "inspectCompiler",
    "compilerInMainProcess",
    "compilerNotInWorker",
  ]);

  const brokenStorageSources = [
    'Object.defineProperty(globalThis, "localStorage", {' +
      ' value: {}, configurable: true, writable: true });',
    'Object.defineProperty(globalThis, "localStorage", {' +
      ' value: { getItem() { throw new Error("broken sync storage"); } },' +
      ' configurable: true, writable: true });',
    'Object.defineProperty(globalThis, "localStorage", {' +
      ' value: { getItemAsync() { return Promise.reject(new Error("broken async storage")); } },' +
      ' configurable: true, writable: true });',
    'Object.defineProperty(globalThis, "localStorage", {' +
      ' value: { getItemAsync: "truthy", getItem() { return null; } },' +
      ' configurable: true, writable: true });',
  ];

  for (const preloadSource of brokenStorageSources) {
    const result = runDryRunWithPreload(preloadSource, { guardPlatformWrite: true });
    const output = combinedOutput(result);

    assert.notEqual(result.status, 0, output);
    assert.match(output, /当前 Node\.js Web Storage 与 miniprogram-ci 不兼容/);
    assert.match(output, /Node 24\.16\.0/);
    assert.doesNotMatch(output, /getItem is not a function/);
    assert.doesNotMatch(output, /servicewechat\.com\/wxa\/ci\/upload/);
    assert.deepEqual(result.vendorLoads, [], output);
    assert.deepEqual(result.platformWrites, [], output);
  }
});

test("upload dry-run executes the real compiler and stops before platform upload", () => {
  const result = runDryRunWithPreload(
    'Object.defineProperty(globalThis, "localStorage", {' +
      ' value: undefined, configurable: true, writable: true });',
    { guardPlatformWrite: true },
  );
  const output = combinedOutput(result);

  assert.equal(result.status, 0, output);
  assert.match(output, /编译预检通过：\d+ 个文件/);
  assert.match(
    output,
    /(?:预检通过：未上传代码|编译、配置与凭据预检通过，但工作区未干净：禁止真实上传)/,
  );
  assert.doesNotMatch(output, /servicewechat\.com\/wxa\/ci\/upload/);
  assert.doesNotMatch(output, /getItem is not a function/);
  assert.ok(result.vendorLoads.length > 0, output);
  assert.deepEqual(result.platformWrites, [], output);
});
