#!/usr/bin/env node

const fs = require("node:fs");
const path = require("node:path");
const { createRequire } = require("node:module");

const repoRoot = path.resolve(__dirname, "..");
const projectPath = path.join(repoRoot, "apps", "miniprogram");
const configPath = path.join(projectPath, "project.config.json");
const defaultKeyPath = path.join(repoRoot, "private.wx20a3a044b52fcbb7.key");
const expectedAppid = "wx20a3a044b52fcbb7";

function usage(exitCode = 0) {
  console[exitCode ? "error" : "log"](`用法：
  npm --prefix apps/miniprogram run upload:test -- --version <版本号> --desc <说明> [--robot 1] [--dry-run]

只上传微信小程序开发版本（体验版）；不会提交审核或正式发布。`);
  process.exit(exitCode);
}

function fail(message) {
  throw new Error(message);
}

function parseOptions(argv) {
  if (argv.length === 1 && ["--help", "-h"].includes(argv[0])) usage();

  const valueOptions = new Set(["--version", "--desc", "--robot"]);
  const values = new Map();
  let dryRun = false;
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (["--help", "-h"].includes(argument)) fail("帮助参数不能与上传参数同时使用");
    if (argument === "--dry-run") {
      if (dryRun) fail("--dry-run 不能重复");
      dryRun = true;
      continue;
    }
    if (!valueOptions.has(argument)) fail(`未知参数：${argument}`);
    if (values.has(argument)) fail(`${argument} 不能重复`);
    const value = argv[index + 1];
    if (!value || value.startsWith("--")) fail(`${argument} 缺少值`);
    values.set(argument, value);
    index += 1;
  }
  return {
    version: values.get("--version"),
    desc: values.get("--desc"),
    robot: values.get("--robot"),
    dryRun,
  };
}

async function main() {
  const options = parseOptions(process.argv.slice(2));
  const { version, desc, dryRun } = options;
  const robot = Number(options.robot || 1);
  const privateKeyPath = path.resolve(process.env.MINIPROGRAM_CI_PRIVATE_KEY || defaultKeyPath);

  if (!version || !desc) usage(1);
  if (!Number.isInteger(robot) || robot < 1 || robot > 30) fail("--robot 必须是 1 到 30 的整数");
  if (!fs.existsSync(configPath)) fail(`缺少真实项目配置：${configPath}`);
  if (!fs.existsSync(privateKeyPath)) fail(`缺少代码上传密钥：${privateKeyPath}`);
  fs.accessSync(privateKeyPath, fs.constants.R_OK);

  const keyMode = fs.statSync(privateKeyPath).mode & 0o777;
  if (keyMode !== 0o600) fail(`上传密钥权限必须为 600，当前为 ${keyMode.toString(8)}`);

  const projectConfig = JSON.parse(fs.readFileSync(configPath, "utf8"));
  if (projectConfig.compileType !== "miniprogram") fail("project.config.json 不是小程序项目");
  if (projectConfig.appid !== expectedAppid) fail("project.config.json AppID 与 Memoria 发布目标不一致");

  console.log(`小程序：${projectConfig.appid}`);
  console.log(`版本：${version}`);
  console.log(`CI 机器人：${robot}`);
  const worktreeStatus = require("node:child_process")
    .execFileSync("git", ["status", "--short"], { cwd: repoRoot, encoding: "utf8" })
    .trim();
  console.log(`工作区状态：${worktreeStatus || "干净"}`);
  const requireFromProject = createRequire(path.join(projectPath, "package.json"));
  const ci = requireFromProject("miniprogram-ci");
  if (dryRun) {
    console.log(
      worktreeStatus
        ? "配置与凭据预检通过，但工作区未干净：禁止真实上传。"
        : "预检通过：未上传代码。",
    );
    return;
  }
  if (worktreeStatus) fail("真实上传要求 Git 工作区干净，请先提交并复核源码");

  const project = new ci.Project({
    appid: projectConfig.appid,
    type: "miniProgram",
    projectPath,
    privateKeyPath,
  });
  const result = await ci.upload({
    project,
    version,
    desc,
    robot,
    setting: projectConfig.setting,
    onProgressUpdate: (progress) => console.log(JSON.stringify(progress)),
  });
  console.log("体验版上传成功。");
  console.log(JSON.stringify(result));
}

main().catch((error) => {
  console.error(`体验版上传失败：${error.message}`);
  process.exit(1);
});
