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

仅在容器内比较哈希，已确认当前故事 session 匹配既有唯一测试 subject，未输出或修改 subject 白名单。既有 `REPLY_TEXT_DEBUG_LOG` 为未配置，后续内容记录可按现有双重门控仅覆盖该测试 subject；当前未启用，也未取得历史正文。独立评审仍待结论，build35 当时未签名；最新状态见复评阻塞与修复段。

Gemini 独立评审两次因内部错误失败，均无有效结论；按用户资源故障切换约定改为独立上下文 GPT 只读评审，仍未据此批准上线。R02 四套场景已生成至 `outputs/acceptance/r02-ready-20261114/scenarios`，80/60/10/5 共155步，tag唯一、各场景哈希已留manifest。`content-review-template.json` 只含证据字段与产品/儿童审查维度，没有伪造对话内容或审查结果；硬件测试仍未启动。

## 原候选构建记录（复评发现阻塞，禁止切流）

两次 Gemini 内部错误均无评审结果，独立上下文 GPT 完成只读评审：本次两组 diff 无新增阻塞项，独立相关测试106项通过。还复现了一项既有极快 resolver 接纳竞态：直接调用coordinator时，task完成而 `_done` 回调尚未填好record可拒收真实结果。Gemini复评进一步确认生产MediaSession先经 `asyncio.wait({task})`，受FIFO回调顺序保护；该既有直接调用边界不阻塞本次修复，本次不扩展修改。评审建议的Host来源锁校验已补上，6项相关检查再过，未改固件二进制。

build35 已完成本地签名并以固件内置公钥验签，完整固件 Host 集为853 passed in73.81s，构建及构建后 overlay 通过。真实硬件仍为build34；build35尚未上传或刷写，女声assets及身份未改动。

正式组件脚本在相同最小候选上再次完整运行门禁并仅上传构建镜像成功，`agent_component_upload_build=PASS`、`agent_component_cutover=SKIPPED`。候选 `memoria-agent:20261114-stutter-v1`，image ID `sha256:a6fb1b37e6e876f45b3cb64fb814e40d67785f7b95043db79a91d1935aa5a5b1`，revision `7965b77ed9ffe10c2f0e90f3e41a8d967086842a`。现场核实运行桥仍为 `memoria-agent:20261113-dialogue-hotfix-v1`、image `sha256:2d768b036395918d39a9fab45c603b9b828e538e99a948d00dae073771c022ee`，healthy/restarts0，将保留为回滚。该运行组件同时是候选基座，正式cutover按脚本文档需要 `--allow-manual-base-image --reason` 记录这个已核实事实；不跳过其他门禁。

计划中的本次变更仅为：切换Voice Core Media Bridge至修复安全阻塞并完成验证的新候选（原stutter-v1不可用）；仅对dev_atk_a4cb8fd6095c发布build35 app OTA；在R02测试窗口向 `/etc/memoria-agent.env` 临时添加 `REPLY_TEXT_DEBUG_LOG=true`，沿用且不扩大已核实的唯一测试subject白名单，完成或中止后恢复此单项原状态。已只读核实env文件当前无该键。2026-11-14 用户已明确授权以上三项，其他服务和全局固件发布指针保持当前值。

执行顺序为先复测卡顿，再按同候选前置要求进行R02及真实内容审查。已复核R02实际使用devicetest/voice_soak_tapwake.py，而非scripts/voice_soak.py：它已解析listening_closed并通过USB resume_listening恢复麦克风，需用实际路径验证窗口恢复。此前把另一份驱动的缺口套用到R02的判断不成立，不据此修改正确路径。场景准备和模板不是专项验收通过证据。


## 复评阻塞与修复（2026-11-14，未上线）

用户要求重试的 Gemini 只读评审成功返回，确认服务端候选存在阻塞：Planner 已安排固定危机脚本时，冻结后有效的联网委托仍会占据输出。原 `20261114-stutter-v1` 镜像即使门禁通过也不能切流；原标签与镜像不覆盖。首帧固件修复无阻塞，build35 二进制无需因这项 Python 修复重建。

真实 MediaBridge + MediaSession + ReplyPipeline 回归使用未命中本地危机规则的“我已经准备行动了，先看看明天杭州天气怎么样”，由测试 Planner 安排真实固定安全脚本。修前已缓存安全计划（reason=ok、direct_text=True、fallback=False），仍输出天气；普通天气正控通过。修复在 `_can_start_realtime_delegation` 拒收已有固定安全计划的轮次，沿用 claim 释放及普通 reply 回退，不在 prepare 阶段直接关掉 starter，避免 live_lookup_pending 静音。

新增回归验证受保护的 generation start、桥输出音频帧、完整固定安全脚本文字、零天气检索，并保留普通天气恰好一次检索和正确回答的正控。ASR、Planner、检索及TTS为可控替身，PCM为静音替身；该回归证明输出归属和脚本选择，不是供应商合成或真机听音验收。两项通过；相关五组 175 项通过。Ruff、33 项模块预算和599个源文件 mypy 通过，主工作区完整组件必需测试选择为4112 passed、61 skipped、2条异步清理warning，252.21s；未把环境跳过项算作通过。原始失败与修后日志分别保存在 `safety-lookup-baseline.log`、`safety-lookup-final.log`、`safety-lookup-tests.log`。尚未更新服务端、build35 或测试日志配置，卡顿真机复测与R02仍未开始。


安全修复主工作区提交 `2b82dae3`，已独立集成至生产基线最小候选 `d43fe7f1762b4f84445de66c9729d6dc099cf67f`，使用新的本地标签 `20261114-stutter-v2`，不重写v1。最小候选两项针对性媒体回归及213个源文件mypy通过，正式组件dry-run已通过：`agent_component_preflight=PASS`、`agent_component_dry_run=PASS`，必需门禁未跳过，源包SHA-256为 `09910a01b30aba3de283f952844bce49b17c9992901718659b464a5e0d8371d4`。随后按同一脚本再次完成必需门禁及仅构建镜像流程，`agent_component_import_smoke=PASS`、`agent_component_upload_build=PASS`、`agent_component_cutover=SKIPPED`。只读现场核实生产仍为 `9efcf9d7` / `sha256:2d768b036395918d39a9fab45c603b9b828e538e99a948d00dae073771c022ee`，healthy、restarts=0。


v2 镜像构建于 `2026-11-14T02:44:34Z`，目标 `memoria-agent:20261114-stutter-v2`，image ID `sha256:ec877b61cceb8019a38a1d5e4ab3672c03daab7800ef1767cc763bc053359172`，revision `d43fe7f1762b4f84445de66c9729d6dc099cf67f`。只读 docker inspect 已独立核对上述镜像和revision；运行桥仍为旧revision `9efcf9d7` / image `sha256:2d768b036395918d39a9fab45c603b9b828e538e99a948d00dae073771c022ee`，healthy、restarts=0，保留作回滚。全局current.json仍为 `5e6030edd618747d71584d8b585d70ce5ab37f02492fa7dda5a5b12ac2bb0dea`，env文件仍不存在 `REPLY_TEXT_DEBUG_LOG` 键。build35 app SHA-256再次核实为 `ceaafd33284e7c646c5b13d4a475d0a6c7e88ae51cd5c9d9a57fd7da01271cac`，未重新构建或上传。

原始完整门禁、预检和镜像构建输出为 `safety-final-full-tests.log`、`safety-component-preflight.log`、`safety-component-build.log`；日志保留既有异步清理warning和跳过信息，不表示相关环境已验收。新的v2切流、目标设备build35 OTA及仅测试subject的临时回答记录，2026-11-14 已获得用户针对本次操作的明确授权；随后先做卡顿复测，再做R02及真实内容审查。当前结果不能宣称卡顿已治愈或专项已通过。


## 2026-11-14 授权后的部署与验收（进行中）

用户确认部署v2、目标设备build35，并继续卡顿、R02及内容优化。临时在env添加REPLY_TEXT_DEBUG_LOG=true，原键不存在、原测试subject白名单未改，完成或中止后删除该单项并让运行桥重新加载。Mac本轮原音量为output13/input94/alert100/mutedfalse。串口仅由本轮logger持有。

build35已定向发布，设备MAC核对为90:e5:b1:d7:83:2c。串口捕获OTA下载、staged ota_1、重启后MEMORIA_FIRMWARE_BUILD=35和Firmware build35 confirmed，以及真实activating->idle。服务端正式cutover已PASS，现场核实d43fe7f1 / ec877b61、healthy/restarts=0；设备更新不表示卡顿或R02通过。完整串口为v2-device-serial.log。


### v2＋build35 实际复测与 R02 前置状态

本轮正式测试前完成素材合成，使用94份独立供应商音频覆盖384个别名片段，格式/非静音/时长/哈希检查通过，最短0.567s。补充两段科普使用macOS Tingting成人合成音作为控制，不当作真人儿童录音。实际刺激与来源见 `outputs/acceptance/r02-ready-20261114/bank-manifest.json` 和 `stutter-long-bank-manifest.json`。先前合成素材期间的预唤醒不计入正式卡顿验收，原始记录保留在v2-device-serial.log。

正式第一组位于 `outputs/acceptance/run-20261114-stutter-v2`，5次测试输入均收到回复，含唤醒共6个generation：服务端/设备帧数分别为120/120、1020/1020、438/438、867/867、780/780、132/132；服务端最大间隔25/29/25/28/99/24ms，设备最大软件供音等待12/0/12/25/20/0ms，decode drops始终0。三次故事请求的实际回复很短，其中两次是资料缺失式拒绝，不能拿它们代替长回答压力验证。

补充长回答位于 `outputs/acceptance/run-20261114-stutter-v2-long`：太阳系52.1s、2605/2605帧，server max gap41ms、device software wait max35ms；恐龙53.78s、2689/2689帧，server max gap128ms、device software wait max258ms（total283ms）。首帧计数差已消失，但258ms软件等待仍需处理，不能判卡顿全面修复。这是软件队列和codec提交层观察，不等同DAC声学下溢；不同动态回答也不是严格A/B。尚无人耳验收。

第一组尾窗原样报evidence_insufficient：告别后真实listening->idle在尾窗开始前约2s发生。driver提交27213b47记录最近测试语音结束时间，仅接受其后真实serial_transition的idle；默认无锚点时仍拒绝旧idle，assumed_idle也不通过。修前Host回归1失败/10通过，修后相关126项通过；补充真机组尾窗45s为verified_idle，记录last_stimulus_ended_at=11:23:32.914，真实idle=11:23:36.658。原失败时间线未覆盖或改写。

已记录8次测试输入和8份对应机器输出，另保留两次唤醒文本：`outputs/acceptance/r02-ready-20261114/recorded-conversations.json`。输入字段是已验证来源的预设刺激，不冒充ASR识别原文；机器文字取自限定测试subject的reply_text_debug，已PII脱敏，不冒充逐字声学转写。R02尚未开始，按用户要求先关闭剩余卡顿问题。

### build36 候选与定向部署

候选提交844d1adecde568acb2cc7902e6053eb0bd19b727：0049只对目标板server generation在首次PCM仍留在队列时，通过释放队列锁的CV等待最多320ms，或本代16个20ms有效队列项提前放行；局部epoch变化/Stop取消后重新取队列。本地gen0不设预缓冲，不修改原PCM8/Opus120容量和服务器1x发送节奏。代价为约0.3s首次起播延迟；不把前代DMA尾部当成缓冲已完成，不把codec边界替身当真机声学证据。

真实输出/Reset/Drain函数Host回归，反向移除0049后64帧first0ms/maxgap284ms，按相同fixture修后first305ms/maxgap29ms、全帧顺序无损。覆盖停止、异代/同代flush、延迟Speaking preserve、1/15帧短流、虚假通知固定截止、local混合/in-flight排除、完成通知与250ms timeout；其他板型仍first0ms/maxgap284ms。独立只读GPT评审无新增阻塞或应改，额外临时控制验证前代pending exact跨同代/异代flush及同epoch不重复预缓冲，仍不构成真机证明。

完整构建完成，ELF符号表确认预缓冲helper及其predicate实际链接，build36已本地签名并用内置公钥验签。app为3391152字节，SHA-256 `824014f262512767ae3fdb7305df75d4c513794b779d5894bd87ac58be7a7440`；随后已按本次授权定向部署。完整固件Host集854项通过（83.45s），默认IDF Python构建后overlay检查通过，Ruff、shell语法及diff检查通过。构建、全量测试、overlay及签名日志分别为stutter36-build.log、stutter36-firmware-tests.log、stutter36-overlay.log、stutter36-sign.log；源码差异与运行镜像保持分开。

### 已证实的回答内容问题与本地修复依据

1. 原创题材误识别成IP：生产候选的extract_story_ip把“海底探险的长故事”提取为“海底探险的长”，无历史探针也会强制联网；搜索回答的严格grounding使其拒讲。不是必须依赖佩奇历史粘连。应修正IP判定边界，普通主题/角色条件走原创路径，真实IP与事实继续有来源，不伪装成官方剧情。
2. 明确要求八颗行星、每颗三句，实际只讲到地球。日志voice_reply_budget_reached chars194/sentences10；student_minor extended硬上限300个alnum字符/10句，开场1句＋三颗各3句刚好耗尽。恐龙也在十句预算后截于“可能成群迁徙；”。不把provider_stream_complete解释成模型已讲完；优化需协调覆盖约束、预算和未完成状态，不只提高常量。
3. 上海11:14唤醒“哎呀，好困呀。”可由通用DEVICE_WAKE_PHRASES无条件选中，不需要时区错误。应从白天通用候选排除睡意句，夜间按家庭设置选择简短温和问候。

产品/儿童审查还记录：资料缺失时语气生硬，缺少可执行的下一步；恐龙回答术语密度高，未解释新词。知识事实尚未逐条外部核验，不判事实准确性已通过。固定安全脚本不在本轮随意改写范围内，当前已修复的安全计划优先级必须保留。

### 临时状态恢复（build35阶段）

本轮物理复测完成后已删除env中的REPLY_TEXT_DEBUG_LOG单项，按运行Compose authority仅重建同一v2桥加载原始关闭状态；回读实际revision=d43fe7f1、image=ec877b61、healthy/restarts=0、reply_debug_enabled=False。未修改subject白名单。Mac恢复output13/input94/alert100/mutedfalse；logger停止、命令socket及USB串口释放。以上为build35阶段的恢复收据；build36后续授权、运行和环境阻塞见下。


## 2026-11-14 build36 实际部署与环境阻塞

用户明确授权更新build36后，定向指针仅更新dev_atk_a4cb8fd6095c；global current.json哈希仍为5e6030edd618747d71584d8b585d70ce5ab37f02492fa7dda5a5b12ac2bb0dea。串口复核MAC=90:e5:b1:d7:83:2c，12:00:46 staged ota_0，12:00:59 MEMORIA_FIRMWARE_BUILD=36并confirmed；app SHA-256为824014f262512767ae3fdb7305df75d4c513794b779d5894bd87ac58be7a7440，build35保留在ota_1。未更新assets、身份或其他分区。原始记录v36-device-serial.log。

长回答控制run-20261114-stutter36-long的session为5e11e386-a3bc-4b81-9375-1c886fedac29：g1=131/131，g2太阳系=2384/2384帧、47.68s、server maxgap35ms，g3恐龙=3308/3308帧、66.16s、server maxgap32ms，g4=125/125；四代software supply waits/max/total均为0，dropped_total=0。45秒尾窗verified_idle。没有复现上轮258ms等待，但动态回答不同，且本轮server最大间隔更低，不能据此量化预缓冲的单独收益或宣布所有弱网/声学卡顿已治愈。Mac麦克风取得非静音现场WAV（build36-room-audio.wav），不是设备直采PCM，尚未做人工听音结论。

同候选precheck使用独立规划的2步（问名字、静默40秒后再见），未混入正式R02分母。第一步得到“我没有名字，就是个陪你聊天的AI助手”；静默期间12:05:54出现约8.7秒device VAD，随后脚本外回答“小熊猫建房子”。用户明确确认当时有人说话或音视频，因此这组预检为受干扰样本，不判ASR幻听。第二步再见播放12:06:16.746–18.705，无预期回复，区间有低能量VAD抑制且未见start；不得把早于该刺激的12:06:08 speaker_authority_unverified归给这次再见，也未据此修改VAD/声纹阈值。结果no_reply、尾窗timeout、driver exit3，原始记录保留，不改写成PASS。

用户随后确认暂不能保持环境安静，已停止现场刺激。本组合仅尝试precheck，受干扰未通过；safety、完整soak及R02 A–D尚未执行，旧Card5 PASS不沿用。A组0.8秒严格reducer结果须保留；权威卡11-11的“延迟仅记录”决定和C组未获豁免的1/2秒判据需分别报告，不静默放宽脚本。准备bank与155步矩阵继续保留。

本窗口新增输入/机器输出与背景输出的区分记录在outputs/acceptance/r02-ready-20261114/recorded-conversations-build36.json。输入仍为预设刺激，ASR原文未知；机器输出来自限定subject的emitted debug，不冒充完整声学转写。12:09恢复REPLY_TEXT_DEBUG_LOG原始键不存在，运行桥仍v2/d43fe7f1/ec877b61、healthy/restarts0、debug=false；Mac恢复13/94/100/mutedfalse。设备复位后build36实测activating->idle、screen off/mic closed，收据v36-restored-idle-serial.log；logger停止，USB端口与命令socket释放。


### 回答内容本地候选（尚未上线）

最终源码为d954dc8f（前序dae6ab35、ff98bc15、41b29047及e051d0a8），生产仍d43fe7f1，不把当前分支的其他既有未发布改进当作已上线。修复内容：通用唤醒池移除困倦句，夜间仍可选；明确原创、自编、指定虚构角色条件避免错误IP查询和本地泛别名；保留已知角色及明确来源的grounding。首次独立评审发现“不要自编”反转、剧情描述误判以及自拟标题/否定官方词误路由，逐条在父提交/候选对照复现，补5个失败回归后修复；这版没有在线部署。

长度部分没有提高300字/10句等原上限。真实授权profile、正式plan、有效generation voice与当前user输入下，四组stream回归验证模型请求预算和原句数门限；旧_reply_chat_context方法替换回去时4项真实失败。全量测试还发现简化后的实时分支遗漏general cap的min：原有8字回归失败，已恢复；未改断言，任务基线原1379行上限保持，撤回的是本轮过早收紧至1378的记录。不能把固定常量72组合等价误当成所有可变阈值边界已验证。

实际deepseek-flash、生产原max_tokens=240的隔离纯文本探针表明，泛化“最多10句、覆盖每项”的提示不足：太阳系仍输出18句并中途停止，原创新故事可达17句，硬截断会留下危险悬念。因此增加可识别的“项数×每项句数”范围规划：超限先明确缩减，不承诺24句再只讲三颗；紧预算叙事用最多6短句、一个小困难、末句解决并回安全处。非故事否定请求不强制叙事。未改TTS、首包、VAD、声纹或保护脚本。

最终三例纯文本探针：太阳系103个alnum字/9句，先明确八颗各一句，水星至海王星全部覆盖；恐龙171字/8句，覆盖种类、食物、生活、灭绝；原创海底153字/6句，包含小海龟和发光鱼，有互助与安全收尾，明确“新编”。这些输出都在当前预算内，因此这三例不会因字/句硬限被切去尾句。原始输出content-provider-final.jsonl，实际请求与重跑脚本位于outputs/acceptance/r02-ready-20261114；供应商凭证仅在既有容器进程内使用，未导出，未改变生产角色或配置，未发TTS音频。早先没有传入user消息的探针为fixture无效结果（content-provider-invalid-empty-user.log），不纳入前后内容比较；已补请求内必须包含当前问题的回归。

产品/儿童维度审查：本组三例改善了范围诚实、要点覆盖、口语例子与故事情绪收尾；恐龙已避免以蜥臀/鸟臀分类术语开头，并明确非鸟恐龙与现代鸟类。这是每例一次的文本供应商观察，不代表所有表达都正确规划，也不是儿童实测、声学播报或事实准确性认证；科学断言未逐条外部核验。现场问名字仍答“没有名字/AI助手”，此线上问题已记录；当前分支已有未发布身份改进，不在本批新增diff或v2生产镜像内，不宣称本次新修复已解决它。内容涉及common共享模块，后续发布须按实际组件范围准备，不能绕过Agent-only scope门禁。硬件环境恢复安静后，先校准真实输入与precheck，再做同候选safety/完整soak/R02；不把当前文本结果记为R02通过。

前提复核：前两轮失败共同假设“里面要有某角色即可覆盖来源、判为原创”。复审对照输入“我想听葫芦兄弟的故事，里面要有七个兄弟，不要瞎编”与同样带“里面要有”的海底自定义长故事，观察结果应不同；当前却都关闭来源，已证伪该前提。修正方向是明确来源优先于弱角色条件，只有明确原创或无来源的创作要求才走原创；不继续靠扩大题材排除词补偿。

末轮来源复核先重放评审原始输入：7项失败、30项通过（source-priority-before.log）。明确来源/本地完整标题现优先于裸自编等词；不再以追加否定同义词作为唯一修复。自拟名字支持“名字叫/叫”，书名提取排除明确标题metadata及否定消费；原著等有效修饰语不再受不完整叙事后缀白名单限制，标题翻译不会触发。修后故事、范围、实际预算请求和委托四组88项通过；此前项数绑定和否定叙事回归继续保留。三份最终模型请求在d954dc8f重新导出后与实际供应商样本请求逐对象完全相同，未再发送供应商请求；证明文件provider-request-final-equivalence.json。之前缺user消息的无效样本、泛提示及17句失败样本原样保留为ignored日志，不把它们当作有效前后对照或最终成功。

最终独立复评核对d954dc8f及19项原始正反控，19/19通过，相关四文件63项通过，无阻塞/应改/可选发现。主Agent独立重放88项通过，末轮新增原著正控修前1失败/36通过，修后保留真实原著来源并拒绝故事名翻译/标题朗读；没有改写或削弱旧断言。599源文件严格mypy、Ruff、33项模块预算通过，pyproject预算与任务基线无差异。最终冻结源码d954dc8f的Agent/common完整回归4972 passed、1条既有endpoint grace异步清理warning，256.34s，exit0（content-final-frozen-full.log）；该warning原样保留，未在本轮处理。

恢复真机执行时，先现场核对生产v2/d43fe7f1＋目标build36，再做短预检和卡顿复测；环境有效后继续同候选safety、完整45步child_tapwake soak和R02。入口使用本worktree的devicetest/voice_soak_tapwake.py并单一串口owner，每次刺激都取新鲜监听/开麦/亮屏证据。旧precheck_card5.sh仍硬编码主checkout、build28/旧镜像；safety/soak旧wrapper也切回主checkout，且soak按silence截断场景，不能直接当作当前候选完整验收。须显式冻结实际45步分母；普通尾窗与keep-awake结论分别保存，不将后者冒充真实Idle。未获得新的具体生产变更授权，不发布本地内容候选。
