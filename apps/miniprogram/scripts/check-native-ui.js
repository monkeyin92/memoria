const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const { shippedFiles } = require("./package-size");

const root = path.resolve(__dirname, "..");
const output = path.resolve(process.argv[2] || path.join(root, "../../docs/design/robot-ui-20261104/native-compile.json"));
const compilers = process.env.WECHAT_COMPILER_DIR || "/Applications/wechatwebdevtools.app/Contents/Resources/app.asar.unpacked/node_modules/wcc-exec";
const temporary = fs.mkdtempSync(path.join(os.tmpdir(), "memoria-native-ui-"));
const results = [];

try {
  for (const [compiler, suffix] of [["wcc", ".wxml"], ["wcsc", ".wxss"]]) {
    const files = shippedFiles().filter((file) => file.endsWith(suffix)).sort();
    if (!files.length) throw new Error(`No ${suffix} files in upload package`);
    const destination = path.join(temporary, `${compiler}.js`);
    const completed = spawnSync(path.join(compilers, compiler), ["-o", destination, ...files], { cwd: root, encoding: "utf8" });
    if (completed.error) throw completed.error;
    const result = {
      compiler,
      files: files.length,
      status: completed.status,
      stderr: completed.stderr,
      outputBytes: fs.existsSync(destination) ? fs.statSync(destination).size : 0,
    };
    results.push(result);
    console.log(`${compiler}: ${result.files} files, status ${result.status}, ${result.outputBytes} bytes`);
  }
  fs.mkdirSync(path.dirname(output), { recursive: true });
  fs.writeFileSync(output, `${JSON.stringify({ date: new Date().toISOString(), results }, null, 2)}\n`);
  if (results.some((result) => result.status !== 0 || !result.outputBytes)) process.exitCode = 1;
} finally {
  fs.rmSync(temporary, { recursive: true, force: true });
}
