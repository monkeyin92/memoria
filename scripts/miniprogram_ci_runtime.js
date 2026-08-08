"use strict";

async function assertCompatibleMiniprogramCiRuntime(
  storage = globalThis.localStorage,
  nodeVersion = process.versions.node,
) {
  const nodeMajor = Number.parseInt(nodeVersion.split(".")[0], 10);
  if (!Number.isInteger(nodeMajor) || nodeMajor < 20 || nodeMajor >= 25) {
    throw new Error(
      `当前 Node.js ${nodeVersion} 不受小程序发布工具支持；请使用 Node 24.16.0`,
    );
  }
  if (storage == null) return;

  const compilerStorageKeys = [
    "inspectCompiler",
    "compilerInMainProcess",
    "compilerNotInWorker",
  ];
  try {
    const getItemAsync = storage.getItemAsync;
    if (getItemAsync) {
      if (typeof getItemAsync !== "function") throw new TypeError();
      for (const key of compilerStorageKeys) {
        await getItemAsync.call(storage, key);
      }
      return;
    }
    const getItem = storage.getItem;
    if (typeof getItem !== "function") throw new TypeError();
    for (const key of compilerStorageKeys) {
      await getItem.call(storage, key);
    }
    return;
  } catch {
    // Fall through to the deterministic compatibility error below. The vendor
    // otherwise fails later with an implementation-specific storage exception.
  }

  throw new Error(
    "当前 Node.js Web Storage 与 miniprogram-ci 不兼容；请使用 Node 24.16.0 运行小程序发布命令",
  );
}

module.exports = { assertCompatibleMiniprogramCiRuntime };
