# 小程序改版：暖纸色与按需展开

日期：2026-11-04。改动落在 `apps/miniprogram` 的实际 WXML、WXSS 和设备页交互。设计稿不是独立于源码的另一套界面。

本轮回应三个问题：单色背景缺乏层次，大字与说明堆叠，设备页混入其他页面的重复内容。保留五个 tab 和现有 API、权限及数据来源，使用已有图片与试听音频，不增加运行时依赖。

## 视觉与页面职责

背景 `#F8F5EF`、表面 `#FFFEFA`、深绿主卡 `#234B40`、杏色 `#F5E3D0`、淡紫色 `#EAE6F3`。颜色、字号和阴影统一在 `app.wxss` 定义。正文 14px、卡片标题 15px、说明 12–13px，页面标题 26px；仅主标题和实际用量使用较大字号。试听与主要操作保留至少 44px 点击区域。

| 页面 | 当前职责与变化 |
| --- | --- |
| 首页 | 安全提醒优先；深绿卡展示实际回顾，已有星澜插画作为品牌元素；用量与记忆入口采用不同层级的卡片。 |
| 回顾 | 按天组织，使用纸页形状和轻量分区色；保留确认、撤回、原文可见性和权限规则。 |
| 声音 | 五种音色用已有角色插画和分区色识别；保留独立试听、选中状态、保存中防重入和失败恢复。 |
| 设备 | 默认只有硬件状态、音量、使用时长摘要、安静时段和「更多设备设置」。时长滑杆展开后编辑；更多设置包含现有故事目录、网络、使用者资料和维护诊断。 |
| 我的 | 杏色账号卡、紧凑设备摘要与权限入口；删除重复的「设备管理」列表行，设备卡仍可打开设备页。 |

设备页删除重复的声音选择入口、隐私说明、数据导出入口，以及第二个解绑入口。声音通过「声音」tab 进入，隐私通过「我的 → 授权与隐私说明」进入，儿童数据导出仍在现有监护页面。设备唯一解绑入口位于「更多设备设置 → 设备信息与诊断」，其他页面发起的解绑仍可直接打开原抽屉。

使用者备注、按人分配人格、年龄申报及表达风格重置保留在设备的使用者设置中。这些功能具有设备/主体上下文，本轮没有将它们复制到其他页面。设备离线提示、安全模式、使用人待确认及老人安全提醒继续直接显示，不藏进折叠项。折叠只控制展示，不产生保存请求；退出账号会收起已展开的私有设置。

不新增或合成儿童台词、故事、音频。原有离线故事目录原样保留。硬件示意仍是静态 `device-screen`，不表示实时屏幕回传。

## 设计参考与取舍

先阅读用户指定的 `baoyu-design`，再查阅真实产品官网和微信组件来源。第三方界面图片只用于研究，没有放入小程序。

| 来源 | 核验材料 | 本轮采用或舍弃的部分 |
| --- | --- | --- |
| [JimLiu/baoyu-design](https://github.com/JimLiu/baoyu-design) | GitHub README、本地完整 SKILL、mobile/hi-fi/prototype 等专项指引 | 先看现有代码与资产，确定页面层级，产出可点击稿并验证；不是套一个示例网页。 |
| [Apple Home](https://www.apple.com/home-app/) | 官网硬件界面图 | 状态与控制分层、紧凑卡片；不复制房间管理、摄像头、电量等本产品没有的数据。 |
| [Headspace](https://www.headspace.com/app) | 官网实际应用截图 | 彩色分区、插画与简短标题结合；保留 Memoria 自有图像，不引入其素材。 |
| [WeUI for 小程序](https://github.com/Tencent/weui-wxss) | 官方仓库说明与 cell/button/dialog 结构 | 设置列表与原生交互习惯；继续使用现有小程序组件，不引入组件库依赖。 |
| [Gentler Streak](https://www.gentler.app/) | 官网年度回顾画面 | 研究色块与信息节奏；其满屏大字叙事不适合家长控制页，因此没有照搬。 |
| [Eve App](https://www.evehome.com/en/eve-app) | 官网产品与设备管理说明 | 用于对照设备管理职责；未据此增加设置能力或虚构数据。 |

以上来源在 2026-11-04 读取。`baoyu-design` 的远端 `HEAD` 为 `3646142d8b4e83a6228cc188f144cd2d2117c482`；远端已将原 HTML 技能放到 `archive/baoyu-design/`。本轮使用会话提供的本地技能，不修改全局安装。

## 预览与证据

- [preview.html](preview.html)：由实际 WXML/WXSS 和现有 fixture 生成，包含五个主页面、设备展开组合及安静时段抽屉；底部 tab 和折叠交互可点击。
- [after/](after/)：最终布局截图。日期、摘要、用量和设备状态来自测试样例。
- [screenshots.json](screenshots.json)、[screenshots.txt](screenshots.txt)：320/375/390/430 宽度和 1.3 倍字体的布局检查。
- [tests.txt](tests.txt)：小程序测试日志。
- [refinement-tests.txt](refinement-tests.txt)：最后一轮样式微调后的针对性回归，包含完整布局矩阵。
- [preview-checks.json](preview-checks.json)：自包含预览的导航、折叠和抽屉交互结果。
- [native-compile.json](native-compile.json)：微信自带原生模板与样式编译器结果。

预览是可离线打开的单文件，不连接真实账号。保存、试听、绑定与权限弹窗必须在微信中验证，预览对这些操作明确显示说明，不伪装保存成功。

最终收口将声音名称与音色说明拆成同级节点，校正选中角标圆角，去掉「我的」页硬件图的裁切底框，并让窄屏的安静时段时间标签跟随正文左对齐。上述微调后更新了 30 张受影响截图和原生编译记录。

| 检查 | 结果与证据 |
| --- | --- |
| 小程序全量测试 | 640 通过，0 失败、0 跳过，见 `tests.txt`；完成于最后一轮样式微调之前。 |
| 微调后回归 | 87 通过，0 失败、0 跳过，见 `refinement-tests.txt`；包含设备、声音、我的页面行为及 180 种布局检查。 |
| 最终截图矩阵 | 20 个场景 × 5 种视图，共 100 项通过。包含设备默认、全部展开、未登录及离线状态。 |
| 自包含预览 | 19 项通过：两套五页导航、五种折叠、收起、安静时段抽屉开关、保存说明；无运行时错误或裂图。 |
| 微信原生编译 | 最终源码的 24 个 WXML、26 个 WXSS 全部通过。 |
| 文案与发布门禁 | 禁用文案检查 80 个文件通过，release gate 通过，`git diff --check` 通过。 |
| 主包体积 | 1940.0 KiB / 2048 KiB；未增加依赖或新的图像、音频资产。 |

主线程检查了实际页面截图。辅助审查使用像素采样、OCR 和 DOM 盒模型核对；其图片工具未能进行多模态直看，因此不作为独立的主观视觉验收。Chrome 布局结果与原生编译结果均不代表已完成手机微信验收。

复跑命令：

```sh
export NODE_PATH="$PWD/.claude/worktrees/mp-brand-pass/apps/miniprogram/node_modules"
export CHROME_PATH='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
export MP_SCREENSHOT_OUTPUT="$PWD/docs/design/parent-ui-20261104"
NODE=/Users/monkeyin/.nvm/versions/node/v22.11.0/bin/node
"$NODE" --test apps/miniprogram/tests/*.test.js
"$NODE" apps/miniprogram/scripts/check-forbidden-ui-copy.js
"$NODE" apps/miniprogram/scripts/check-release-gate.js
"$NODE" apps/miniprogram/scripts/package-size.js
"$NODE" apps/miniprogram/scripts/screenshot-parent-ui.js
"$NODE" apps/miniprogram/scripts/build-parent-preview.js
```

截图借用已有 worktree 的 `puppeteer-core`，没有新增依赖。原生编译器来自 `/Applications/wechatwebdevtools.app/Contents/Resources/app.asar.unpacked/node_modules/wcc-exec/`。

## 交付边界

| 状态 | 2026-11-04 事实 |
| --- | --- |
| code | 实际页面和折叠交互已实现。 |
| wired | 使用原有 Page 数据、设备设置 API 和权限校验。 |
| enabled | 仅当前本地工作树。本轮未上传、提审或发布。11 月 3 日开发版收据不能代表本轮源码已上线。 |
| verified | 全量 640 项测试、微调后 87 项回归及 180 种布局、100 项截图检查、19 项预览交互和 24/26 个原生模板/样式编译均通过。微信运行时、手机和真实后端联调未验收。 |

本轮不改变后端、固件、声学或卡 5 的验收结论。仓库原有无关改动和暂存区保持原样。
