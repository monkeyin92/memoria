const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

async function withMiniprogramBuild(projectPath, consentUnlock, callback) {
  const source = fs.readFileSync(path.join(projectPath, "utils/legal-release.js"), "utf8");
  const declaration = "const INTERNAL_TEST_CONSENT_UNLOCK = false;";
  if (source.split(declaration).length !== 2) {
    throw new Error("源码 INTERNAL_TEST_CONSENT_UNLOCK 必须为 false");
  }
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "memoria-miniprogram-build-"));
  const buildPath = path.join(directory, "project");
  try {
    fs.cpSync(projectPath, buildPath, {
      recursive: true,
      filter(file) {
        const name = path.basename(file);
        if (name === "node_modules" || name === ".git" || name.startsWith(".env")) return false;
        if (fs.lstatSync(file).isSymbolicLink()) throw new Error(`构建输入不允许符号链接：${file}`);
        return true;
      },
    });
    if (consentUnlock) {
      fs.writeFileSync(path.join(buildPath, "utils/legal-release.js"), source.replace(
        declaration, "const INTERNAL_TEST_CONSENT_UNLOCK = true;",
      ));
    }
    return await callback(buildPath);
  } finally {
    fs.rmSync(directory, { recursive: true, force: true });
  }
}

module.exports = { withMiniprogramBuild };
