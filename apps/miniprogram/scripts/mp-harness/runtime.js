/*
 * Runs a real mini program page module under Node with stubbed wx / getApp /
 * Page globals and an injected utils/api fixture, then records every setData
 * payload. Used by tests (function + setData size) and the screenshot harness.
 */
const fs = require("node:fs");
const path = require("node:path");

const ROOT = path.join(__dirname, "..", "..");

function createWx({ storage = {}, systemInfo = {}, onCall, files = {} } = {}) {
  const store = new Map(Object.entries(storage));
  const calls = [];
  const info = {
    windowWidth: 390,
    windowHeight: 844,
    screenWidth: 390,
    screenHeight: 844,
    pixelRatio: 3,
    statusBarHeight: 47,
    safeArea: { top: 47, bottom: 810, left: 0, right: 390, width: 390, height: 763 },
    platform: "ios",
    SDKVersion: "3.6.0",
    ...systemInfo,
  };
  const known = {
    getStorageSync: (key) => (store.has(key) ? store.get(key) : ""),
    setStorageSync: (key, value) => store.set(key, value),
    removeStorageSync: (key) => store.delete(key),
    clearStorageSync: () => store.clear(),
    getSystemInfoSync: () => info,
    getWindowInfo: () => info,
    getDeviceInfo: () => info,
    getAppBaseInfo: () => info,
    getAccountInfoSync: () => ({ miniProgram: { appId: "wx-harness", envVersion: "develop" } }),
    getMenuButtonBoundingClientRect: () => ({
      top: info.statusBarHeight + 4,
      bottom: info.statusBarHeight + 36,
      height: 32,
      left: info.windowWidth - 94,
      right: info.windowWidth - 7,
      width: 87,
    }),
    showModal: (options = {}) => options.success?.({ confirm: false, cancel: true }),
    showActionSheet: (options = {}) => options.fail?.({ errMsg: "cancel" }),
    nextTick: (fn) => setImmediate(fn),
    canIUse: () => true,
    getFileSystemManager: () => ({
      readFileSync(filePath, encoding) {
        const rel = String(filePath || "").replace(/^\//, "");
        if (Object.prototype.hasOwnProperty.call(files, rel)) return files[rel];
        return fs.readFileSync(path.join(ROOT, rel), encoding || "utf8");
      },
    }),
    createInnerAudioContext: () => {
      const audio = {
        src: "",
        play: () => calls.push({ name: "InnerAudioContext.play", args: [audio.src] }),
        stop: () => calls.push({ name: "InnerAudioContext.stop", args: [audio.src] }),
        destroy() {},
        onEnded() {},
        onError() {},
      };
      return audio;
    },
    createSelectorQuery: () => {
      const query = {
        in: () => query,
        select: () => query,
        selectAll: () => query,
        boundingClientRect: () => query,
        fields: () => query,
        exec: (cb) => cb?.([]),
      };
      return query;
    },
  };
  const wx = new Proxy(known, {
    get(target, name) {
      if (typeof name !== "string") return undefined;
      return (...args) => {
        calls.push({ name, args });
        onCall?.(name, args);
        if (name in target) return target[name](...args);
        const options = args[0];
        if (options && typeof options === "object" && (options.fail || options.complete)) {
          const result = { errMsg: `${name}:fail not available in harness` };
          setImmediate(() => {
            options.fail?.(result);
            options.complete?.(result);
          });
        }
        return undefined;
      };
    },
  });
  return { wx, calls, store };
}

function setPath(target, key, value) {
  const parts = key.replace(/\[(\d+)\]/g, ".$1").split(".");
  let cursor = target;
  for (let index = 0; index < parts.length - 1; index += 1) {
    const part = parts[index];
    if (cursor[part] == null || typeof cursor[part] !== "object") {
      cursor[part] = /^\d+$/.test(parts[index + 1]) ? [] : {};
    }
    cursor = cursor[part];
  }
  cursor[parts[parts.length - 1]] = value;
}

function clone(value) {
  return value === undefined ? undefined : JSON.parse(JSON.stringify(value));
}

function payloadBytes(patch) {
  return Buffer.byteLength(JSON.stringify(patch) || "", "utf8");
}

function clearProjectModules() {
  for (const key of Object.keys(require.cache)) {
    if (key.startsWith(ROOT) && !key.includes(`${path.sep}node_modules${path.sep}`) && !key.includes(`${path.sep}scripts${path.sep}`)) {
      delete require.cache[key];
    }
  }
}

function injectModule(relativePath, exportsValue) {
  const resolved = require.resolve(path.join(ROOT, relativePath));
  require.cache[resolved] = { id: resolved, filename: resolved, loaded: true, exports: exportsValue };
}

/*
 * loadPage("pages/home/index", { api, storage, config })
 * Returns { page, setDataLog, wxCalls, run(query) }.
 */
function fixedDate(now) {
  const RealDate = Date;
  const fixed = now instanceof RealDate ? now.getTime() : new RealDate(now).getTime();
  class FixedDate extends RealDate {
    constructor(...args) {
      super(...(args.length ? args : [fixed]));
    }
    static now() {
      return fixed;
    }
  }
  return FixedDate;
}

function loadPage(pagePath, { api = {}, storage = {}, config, systemInfo, app = {}, now, files, effectiveOn } = {}) {
  clearProjectModules();
  const { wx, calls } = createWx({ storage, systemInfo, files });
  let definition = null;
  const previous = {
    wx: global.wx,
    Page: global.Page,
    Component: global.Component,
    getApp: global.getApp,
    getCurrentPages: global.getCurrentPages,
    Date: global.Date,
  };
  if (now) global.Date = fixedDate(now);
  global.wx = wx;
  global.Page = (def) => {
    definition = def;
  };
  global.Component = () => {};
  const appInstance = { globalData: {}, subscribeAuthCleared: () => () => {}, ...app };
  global.getApp = () => appInstance;
  global.getCurrentPages = () => [];

  injectModule("utils/api.js", api);
  if (config) injectModule("config.js", config);
  if (effectiveOn !== undefined) {
    const legalPath = require.resolve(path.join(ROOT, "utils/legal-release.js"));
    const real = require(legalPath);
    injectModule("utils/legal-release.js", {
      EFFECTIVE_ON: effectiveOn,
      consentLocked: real.consentLocked,
      releaseChip: (value) => real.releaseChip(value === undefined ? effectiveOn : value),
    });
  }
  require(path.join(ROOT, pagePath));
  if (!definition) throw new Error(`${pagePath} did not call Page()`);

  const setDataLog = [];
  const tabBar = { data: {}, setData(patch) { Object.assign(this.data, patch); } };
  const page = Object.create(null);
  for (const [key, value] of Object.entries(definition)) {
    page[key] = typeof value === "function" ? value.bind(page) : value;
  }
  page.data = clone(definition.data) || {};
  page.route = pagePath;
  page.setData = (patch, callback) => {
    setDataLog.push({ keys: Object.keys(patch), bytes: payloadBytes(patch) });
    for (const [key, value] of Object.entries(patch)) setPath(page.data, key, clone(value));
    if (typeof callback === "function") callback();
  };
  page.getTabBar = () => tabBar;
  page.selectComponent = () => null;
  page.createSelectorQuery = wx.createSelectorQuery;

  async function settle() {
    let stable = 0;
    let last = -1;
    for (let tick = 0; tick < 400 && stable < 4; tick += 1) {
      await new Promise((resolve) => setImmediate(resolve));
      if (setDataLog.length === last) stable += 1;
      else {
        stable = 0;
        last = setDataLog.length;
      }
    }
  }

  async function run(query = {}) {
    if (typeof page.onLoad === "function") await page.onLoad(query);
    if (typeof page.onShow === "function") await page.onShow();
    if (typeof page.onReady === "function") await page.onReady();
    await settle();
    return page.data;
  }

  function restore() {
    for (const [key, value] of Object.entries(previous)) {
      if (value === undefined) delete global[key];
      else global[key] = value;
    }
  }

  return { page, setDataLog, wxCalls: calls, tabBar, run, settle, restore };
}

module.exports = { ROOT, createWx, loadPage, payloadBytes, setPath };
