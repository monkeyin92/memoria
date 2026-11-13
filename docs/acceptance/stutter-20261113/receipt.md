# 2026-11-13 播放卡顿候选 build34

状态：build34 已定向 OTA 并确认启动。设备接收 priority=4，全程队列满拒绝数为 0；仍发现服务端 866 ms 发送间隔及设备 795 ms 供音等待，卡顿尚未全部解决。

## 已有设备证据

已部署 build33 的 session `154ab015-e824-43f0-9a01-79aedec0cb9a`，generation 3：服务端 1009 帧、max_gap 26 ms，设备播放 969 帧、解码队列满拒绝 40 帧；供音等待 7 次，最长 1944 ms、累计 3150 ms。969 + 40 = 1009；播放结束回执不能证明连续播放。

新增 build33 基线 session `d3a00fdc-f7d1-49df-a3a9-b6765c4eb590`、stream 3193，generation 2：563 帧完整播放，无队列满日志；供音等待 7 次，最长 304 ms、累计 788 ms。服务端 max_gap 89 ms。串口先确认麦克风及 listening，随后才播放故事查询。两轮动态回答不同，不能据此计算改善比例。

## 候选边界

`0047` overlay 将队列满分支的串口日志替换为原子累计，现有主任务每 10 秒在锁外报告 `media decode queue stats dropped_total=...`。队列仍为 120，原拒绝和停止语义保留。真实函数 Host 回归中，旧实现调用阻塞串口时消费者取不到锁；修复后串口不被调用、消费者可以取锁，拒绝数守恒。

仅目标板、WiFi、WSS 的专属接收任务，每连接 attempt 将基础优先级至少设为 4，保留原来更高的优先级。attempt 检查及设置与 retirement 同锁串行化；先于状态锁采样优先级，避免将临时继承的高优先级误判为基础优先级。Host 继承回归在修前实际得到锁释放后 priority=1，修后为 4。未调整窗口、WiFi RX buffer、起播水位或超时。

队列内持锁日志是已复现缺陷；接收调度是否解释全部供音等待，仍需同一候选真机观察，不能从串口停顿直接断言 TCP 重传。

## 验证

- 相关回归 134 项通过，Ruff、diff 检查通过。
- 完整 `firmware/esp32/tests`：852 passed in 106.74s。
- 完整 ESP-IDF 6.0.2 构建、构建后 overlay 检查通过。首次 overlay 检查因 IDF Python 无 pytest 失败；指定现有项目虚拟环境后完整重跑通过，无跳过构建后检查。
- 独立 Gemini 评审：运行时无阻塞；门禁原先依赖 pytest 的问题已改为标准库独立入口。5 项相关回归及默认 IDF Python 下完整 overlay 检查通过，保留相同的并发、继承和两个板型验证。该修改不影响固件镜像，无需重编。
- 尚未验证设备实际接收优先级、播放连续性及新候选的供音等待/丢包。

原始日志仅留本地，未纳入版本控制：

- `baseline-serial.log`: SHA-256 `0b3f39340a540dba56d68def9de742d1c31a1b74498d85323361c001f0469c71`
- `baseline-bridge.log`: SHA-256 `92e686212a4689ec14c1ece7976908d60abf9f4c66b15cd171112030150cb9a8`
- `firmware-tests.log`: SHA-256 `792c0bee03e05cd3078e6a512642200c8df196a2b0169e60fbd91b5a3db2af11`

## 构建产物

`firmware/esp32/artifacts/stutter-build34/memoria-esp-vocat-app.bin`，3,390,928 bytes，版本 `2.4.2`、build34，SHA-256 `7c73d4738a8a8d8eda8c63ffe6596735d2b9973c8c1203a8846fd0208c65e48e`。发布脚本的真实镜像检查确认 build 标记、产品镜像限制和 OTA 容量通过；已本地签名且通过内置公钥验签；签名产物在 `outputs/firmware-releases/memoria-esp-vocat/34/`，尚未上传。

## 2026-11-14 定向 OTA 与真机复测

用户明确确认本次更新。目标 MAC `90:e5:b1:d7:83:2c` 匹配。定向发布 build34，服务端镜像哈希与本地一致；全局 `current.json` SHA-256 前后一致：`5e6030edd618747d71584d8b585d70ce5ab37f02492fa7dda5a5b12ac2bb0dea`。为触发启动后检查，仅在空闲时重启旧 build33，没有用 USB 写 flash。00:36:18 通过 OTA 写入 ota_0，00:36:30 报告 `MEMORIA_FIRMWARE_BUILD=34` 并 confirmed。旧 ota_1 保留。应用 OTA 不更新 assets，启动 assets checksum 检查正常；未重新读取独立分区作字节比较。

实际媒体 session `36e7157a-a022-441e-9804-dfc9307283b6`、stream 3194。首次连接 `priority_before=1 priority_actual=4`。三次有效故事回答均有 playback_ended/actual_heard 回执；这些回执不代表连续听感或人耳验收。

- generation 2：发送 857 帧/17140 ms，max_gap 27 ms；软件输出计数 856，供音等待 2 次、max 30 ms、total 34 ms。
- generation 4：发送 5791 帧/115820 ms，max_gap 39 ms；软件输出计数 5790，供音等待 5 次、max 56 ms、total 94 ms。
- generation 5：发送 2481 帧/49620 ms、wall 50448 ms、max_gap 866 ms；软件输出计数 2480，供音等待 2 次、max 795 ms、total 808 ms。等待日志为 00:41:25.215，位于这段回答开头。仍须定位发送间隔来源，不能将相关性直接当成 TTS 或网络根因。

设备累计 `dropped_total=0`，exact_timeouts=0。每段发送与软件输出仍差 1 帧，尚未确认是计量边界或真实缺帧，不能声称帧数严格守恒。第一遍第三次输入撞上 30 秒 listening_closed，未形成有效轮次，已作废；重新唤醒并确认监听恢复后重测。00:43:12 正常告别后进入 wifi_hold、mic close；串口 logger 已停止。Mac 输出音量恢复为 6，mute=false，输入94、alert100；R02 前保存了完整音量状态，本轮开始前仅留输出6记录。没有人工听音验收。

下一步：定位服务端 866 ms 间隔；并行准备 R02 打断/附和矩阵及真实对话内容记录。R02 和内容优化尚未验收，不沿用历史 Card5 通过为新组合自动放行。

## R02 与内容审查准备

现有 `devicetest/build_r02_scenarios.py` 固定矩阵为 A 停止词 8×10、B 附和 6×10、C 内容打断 10、D 危机软停止 5，共 155 步。正式验收前需同一当前候选的 Card5 前置，逐轮核对唤醒、麦克风、监听窗口与实际播放；停止回执与 `silence_verdict` 分开判断，未知或缺行不能作为通过。

既有 runner 硬编码切回主 checkout 的问题已修正为按脚本路径定位 repo root。默认候选仍为历史值，正式运行必须显式传入当前线上候选并保留原始分母及失败样本，不沿用旧候选的门禁结果。

本轮会话已只读查询 PG 归档：3 条 user finalized、3 条 assistant playout 元数据事件，但 payload 均不含正文，`archive_transcript_versions` 对该 session 为 0 行。因此 `conversation.log` 仅为元数据，不是完整对话记录，不据此作内容质量评审。R02 开跑前须先验证真实提问/回答的记录链路，用户输入脚本和文本长度均不能替代机器实际回答。未修改生产留存或 debug 配置。

R02 规程检索子任务因 15 分钟无输出失败，主 Agent 已直接核对矩阵和记录路径。服务端 866ms 间隔独立定位仍进行中，R02 尚未启动。

## 后续本地复现与候选 build35（未部署）

服务器准备轮次时先启动委托、随后冻结 context 的竞态已本地复现：委托观察 `(ready=False, context_version=0)`，准备完成后版本为 1。候选让 ReplyPipeline 在响应计划与上下文冻结后才启动委托，保留前置 prefetch/warm、实际版本拒收以及非 prepare 调用原有行为。对应状态分派放在既有 context activation mixin；模块预算仅收紧至 DuplexRuntime 1461、ReplyPipeline 1379，未提高上限。

IP 故事 fallback 等待检索时，原流程先向正文语音流吐短过渡语；新回归确认修前首个 anext 提前返回，修后保持等待，直到 grounded 正文可用。候选仅移除此 IP 故事 fallback 的同流过渡语；MediaSession 原有独立辅助代际继续负责过渡，天气路径原契约暂不变。62 项相关回归、599 个源文件 mypy、Ruff 通过；全量 Agent 回归仍进行中。

固件首帧另已实际复现，不能继续归为纯计数差：generation.started 同步建立 barrier 后，seq0 入队，随后异步 Speaking 调 ResetDecoder 将它清掉。旧函数 Host 结果 queued=0/output=0/fence_delta=1；0048 候选为 queued=1/output=1/seq=0/fence_delta=0，显式停止/取消/换代、本地及其他板型正控保持。该新 Host 用例最初误读旧生成缓存，出现编译失败，现改成从 pinned Git 对象在临时目录应用所有当前 patch，6 项相关回归通过。

候选 build35 完整构建与独立 Gemini 评审进行中，设备仍运行已获授权的 build34；未进行新一轮生产切换。尚未验证这些新修复的真实发声和服务端长间隔是否消失。现有生产测试 subject 白名单已配置，回答正文 debug 日志未打开；没有擅自修改该生产配置。

### 冻结源码后的检查与部署准备

较早一轮 Agent 全量结果为 4056 passed、2 failed：两项子进程测试在调整模块期间读到了临时错误的 UtteranceRoute import 路径。该路径已修正，不能将这一轮记为通过。源码冻结后 8 项进程退出测试重跑通过，完整测试集重新进行中；保留失败原始日志，不跳过用例。构建后 overlay 检查通过，所有 33 项模块预算检查通过。

build35 产物为 3,390,960 bytes，SHA-256 `ceaafd33284e7c646c5b13d4a475d0a6c7e88ae51cd5c9d9a57fd7da01271cac`，真实产品镜像检查确认 build=35、版本2.4.2；尚未签名或更新设备。Agent 主分支候选提交 `b0771745`；为避免混入其他主线差异，从实际生产 `9efcf9d7` 建立最小发布候选 `7965b77ed9ffe10c2f0e90f3e41a8d967086842a`，仅 cherry-pick Agent 修复和预算表。临时 worktree `/private/tmp/memoria-prod-stutter-20261114`。本地标签 `20261114-stutter-v1` 未推送，正式组件脚本的 dry-run 与必需门禁在该干净候选上进行，未切换生产或修改生产配置。

冻结源码后的完整 Agent 测试结果：4058 passed、1 warning，255.24 秒，exit0。warning 为既存 endpoint grace coroutine 在无运行中事件循环时清理的 RuntimeError，本轮未扩大范围修复。完整输出保存在 `agent-final-full.log`。最小生产候选的正式组件门禁仍在执行；R02 runner 的 root 定位仅作 bash 语法及 diff 检查，尚未运行真实专项。

正式最小发布候选 `7965b77e` 的组件脚本 dry-run 已 exit0：Ruff、33 项预算、mypy(213源文件)、必需测试及 source/base provenance 预检通过。测试日志含既有环境跳过项及异步清理 warning，原始输出为 `component-preflight.log`。`agent_component_preflight=PASS`、`agent_component_dry_run=PASS`。基座 image ID 为 `sha256:2d768b036395918d39a9fab45c603b9b828e538e99a948d00dae073771c022ee`，source archive SHA-256 `d6ce82c34e2ee3b5c7ec5f3bacbf53790f6926520b1294292906197dc8e17980`，目标镜像名 `memoria-agent:20261114-stutter-v1`。本次是 dry-run，没有上传、构建生产镜像或切流。

仅在容器内比较哈希，已确认当前故事 session 匹配既有唯一测试 subject，未输出或修改 subject 白名单。既有 `REPLY_TEXT_DEBUG_LOG` 为未配置，后续内容记录可按现有双重门控仅覆盖该测试 subject；当前未启用，也未取得历史正文。独立评审仍待结论，build35 当时未签名；最新状态见最终候选就绪段。

Gemini 独立评审两次因内部错误失败，均无有效结论；按用户资源故障切换约定改为独立上下文 GPT 只读评审，仍未据此批准上线。R02 四套场景已生成至 `outputs/acceptance/r02-ready-20261114/scenarios`，80/60/10/5 共155步，tag唯一、各场景哈希已留manifest。`content-review-template.json` 只含证据字段与产品/儿童审查维度，没有伪造对话内容或审查结果；硬件测试仍未启动。

## 最终候选就绪（待本次生产更新授权）

两次 Gemini 内部错误均无评审结果，独立上下文 GPT 完成只读评审：本次两组 diff 无新增阻塞项，独立相关测试106项通过。还复现了一项既有极快 resolver 接纳竞态：task完成而 `_done` 回调尚未填好record时，coordinator可拒收真实结果。它不是本补丁引入，本次不扩展修改，作为已知限制保留，不能宣称所有委托时序问题已解决。评审建议的Host来源锁校验已补上，6项相关检查再过，未改固件二进制。

build35 已完成本地签名并以固件内置公钥验签，完整固件 Host 集为853 passed in73.81s，构建及构建后 overlay 通过。真实硬件仍为build34；build35尚未上传或刷写，女声assets及身份未改动。

正式组件脚本在相同最小候选上再次完整运行门禁并仅上传构建镜像成功，`agent_component_upload_build=PASS`、`agent_component_cutover=SKIPPED`。候选 `memoria-agent:20261114-stutter-v1`，image ID `sha256:a6fb1b37e6e876f45b3cb64fb814e40d67785f7b95043db79a91d1935aa5a5b1`，revision `7965b77ed9ffe10c2f0e90f3e41a8d967086842a`。现场核实运行桥仍为 `memoria-agent:20261113-dialogue-hotfix-v1`、image `sha256:2d768b036395918d39a9fab45c603b9b828e538e99a948d00dae073771c022ee`，healthy/restarts0，将保留为回滚。该运行组件同时是候选基座，正式cutover按脚本文档需要 `--allow-manual-base-image --reason` 记录这个已核实事实；不跳过其他门禁。

计划中的本次变更仅为：切换Voice Core Media Bridge至上述候选；仅对dev_atk_a4cb8fd6095c发布build35 app OTA；在R02测试窗口向 `/etc/memoria-agent.env` 临时添加 `REPLY_TEXT_DEBUG_LOG=true`，沿用且不扩大已核实的唯一测试subject白名单，完成或中止后恢复此单项原状态。已只读核实env文件当前无该键。所有三项均尚待本次明确授权，其他服务和全局固件发布指针保持当前值。

执行顺序为先复测卡顿，再按同候选前置要求进行R02及真实内容审查。R02 driver仅看listening状态可能漏掉listening_closed窗口，正式155步前还需补齐此门禁验证，避免重演本轮已作废的窗口碰撞。场景准备和模板不是专项验收通过证据。
