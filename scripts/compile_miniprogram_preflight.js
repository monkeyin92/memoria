#!/usr/bin/env node

const fs = require("node:fs");
const path = require("node:path");
const { createRequire } = require("node:module");

const { assertCompatibleMiniprogramCiRuntime } = require("./miniprogram_ci_runtime");

async function main() {
  await assertCompatibleMiniprogramCiRuntime();
  const projectPath = path.resolve(process.argv[2] || "");
  const configPath = path.join(projectPath, "project.config.json");
  if (!projectPath || !fs.existsSync(configPath)) {
    throw new Error("缺少小程序项目或 project.config.json");
  }

  const projectConfig = JSON.parse(fs.readFileSync(configPath, "utf8"));
  const requireFromProject = createRequire(path.join(projectPath, "package.json"));
  const ci = requireFromProject("miniprogram-ci");
  const project = new ci.Project({
    appid: projectConfig.appid,
    type: "miniProgram",
    projectPath,
    privateKey: "compile-only-placeholder-not-a-credential",
    attr: async () => ci.DefaultProjectAttr,
  });
  const compiled = await ci.getCompiledResult({
    project,
    setting: projectConfig.setting,
  });
  const fileCount = Object.keys(compiled).length;
  if (fileCount === 0) throw new Error("小程序编译结果为空");
  console.log(`MEMORIA_MINIPROGRAM_COMPILE_FILES=${fileCount}`);
}

main().then(
  () => process.exit(0),
  (error) => {
    console.error(`小程序编译预检失败：${error.stack || error.message}`);
    process.exit(1);
  },
);
