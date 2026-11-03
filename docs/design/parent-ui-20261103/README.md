# 家长端小程序改版与本地验证

本次将 `apps/miniprogram` 从吉祥物展示改为家长查看回顾、提醒和设备设置的界面。任务日期为 2026-11-03。微信开发者工具运行和手机验收尚未完成；上传状态以发布收据为准。

## 布局取舍

| 方案 | 首页重点 | 取舍 |
| --- | --- | --- |
| A | 设备大卡与用量 | 不采用。设备状态不宜盖过家长需要查看的摘要和提醒。 |
| B | 安全提醒与近期回顾 | 采用。设备和用量保留为紧凑卡片，不添加远程唤醒入口。 |

首页顺序为安全提醒、近期回顾、设备与用量、孩子记忆及原有设置。摘要使用现有数据；用量未知时显示“用量暂未统计”，不画进度条，也不冒充零用量。

第三个 tab 改为“声音”，路由和索引不变。音色按纵向列表排列，表达描述优先于风格名称。试听不切换设置，保存中禁止再次选择，保存失败恢复原选项。自定义表达仍走原流程。

页面中的吉祥物图和 `face-badge` 展示已移除。静态 `device-screen` 代表机器人硬件，不表示实时屏幕回传。设备名称按绑定模式显示，不再跟随表达风格变化。

## 视觉方向

浅青灰底 `#F2F6F5`、白色表面 `#FFFFFF`、青绿强调色 `#176B5B`、浅绿选中底 `#E3F0ED`、正文 `#172E32`、辅助文字 `#536A6C`。卡片使用细边框，浮层保留轻阴影。保留中文系统字体，不增加字体或运行时依赖。

图标由 `apps/miniprogram/scripts/build-v4-icons.js` 生成。全局样式、导航配置及图标选中与未选中颜色已统一。安全和隐私文案、登录和绑定权限、服务端契约、音色 ID、现有试听音频均未更改。

## 验证结果

以下结果来自最后一次图标与 token 调整后的运行。

| 检查 | 结果 | 证据 |
| --- | --- | --- |
| 小程序全量测试 | 640 passed，0 failed，0 skipped | `tests.txt` |
| 微信原生 WXML 编译 | 24 个文件通过 | `native-compile.json` |
| 微信原生 WXSS 编译 | 26 个文件通过 | `native-compile.json` |
| 截图布局矩阵 | 17 个场景 × 5 个视图，85 项通过 | `screenshots.txt`、`screenshots.json`、`after/` |
| 禁用文案检查 | 80 个文件通过 | `check:forbidden-copy` 命令输出 |
| 独立代码审查 | PASS，无 P1/P2 问题 | 本会话已完成的审查结果 |
| 差异空白检查 | 通过 | `git diff --check` |

布局矩阵覆盖 320、375、390、430 宽度及 390 宽度 1.3 倍字体。检查横向溢出、胶囊遮挡和底部导航遮挡，包含未登录、离线、未知用量、空回顾、绑定和引导完成状态。截图使用现有 fixture 数据；图片中的日期、摘要和在线状态不是生产数据。

截图由 Chrome 中的 WXML harness 渲染，不等于微信 WebView 验收。已人工查看最终首页与声音页；微信开发者工具实际运行、手机微信验收和真实后端联调尚未完成。原生编译通过只证明模板和样式可编译。

### 复跑本地验证

在仓库根目录运行。当前借用旧设计 worktree 中已有的 `puppeteer-core`，没有安装新依赖。

```sh
export NODE_PATH="$PWD/.claude/worktrees/mp-brand-pass/apps/miniprogram/node_modules"
export CHROME_PATH='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
NODE=/Users/monkeyin/.nvm/versions/node/v22.11.0/bin/node
"$NODE" apps/miniprogram/scripts/build-v4-icons.js
"$NODE" --test apps/miniprogram/tests/*.test.js > docs/design/parent-ui-20261103/tests.txt 2>&1
"$NODE" apps/miniprogram/scripts/check-forbidden-ui-copy.js
"$NODE" apps/miniprogram/scripts/check-release-gate.js
"$NODE" apps/miniprogram/scripts/screenshot-parent-ui.js > docs/design/parent-ui-20261103/screenshots.txt 2>&1
git diff --check -- apps/miniprogram docs/design.md docs/design/parent-ui-20261103
```

原生编译使用微信开发者工具内的 `wcc-exec/wcc` 与 `wcc-exec/wcsc`，位于 `/Applications/wechatwebdevtools.app/Contents/Resources/app.asar.unpacked/node_modules/`。编译输入排除测试、预览和依赖目录，输出写入临时目录并在检查后删除。结果保存在 `native-compile.json`。

若旧 worktree 被回收，先为截图 harness 提供 `puppeteer-core` 并更新 `NODE_PATH`。不要把依赖缺失导致的截图测试跳过当成验证通过。

## 交付边界

- `code` 已实现。未改后端、固件或设备控制。
- `wired` 使用现有 Page 数据和 API，绑定、权限、试听和保存行为通过本地测试。
- `enabled` 状态以发布收据为准，上传开发版本不等同于设为体验版或正式发布。
- `verified` 本地测试、原生编译、Chrome 布局矩阵通过；微信运行时未验收。

原有暂存区保持不变。其二进制 diff 的 SHA-256 为 `f8ef7ee9e9ad0ce70bb3ecba7ca9f7b55c7d445210657dc5a550e10982a3faf2`。

机器人仍无唤醒成功回执。卡 5 safety 和 soak 仍未验收，本次界面改版不改变设备验收状态。

## 图片

- `before/` 保留改版前首页、声音入口和个人页。
- `after/home-390.png` 为最终首页。
- `after/companion-390.png` 为最终声音页。
- `after/` 其余图片对应 `screenshots.json` 的场景与视图。

本轮按 Prove It Works 原则重新生成最终图标、测试结果与截图，没有用继承的旧截图代替最终验证。
