# 2026-10-22 Agent 排期规划

只读调研后的排期建议。执行记录见下节；未更新到节内的描述仍代表调研当时的状态。

## 执行记录（2026-10-22 当晚更新）

- 方案 C 已执行：#377（pr376 白名单行）以 a6751861 合入 main，#383 关闭。
- 方案 A 已实现（主机状态文件 `/opt/memoria/state/component-overrides.tsv`，不是文中的 `.cutover/`）：白名单常量整表删除，`override_spec()` 改读状态文件（0600、root 属主、按发布根 rebase 路径），新增 `release-ops.sh record-override <服务> <持久路径> </tmp/原件>`（逐字节一致性、image-only、flock 原子追加，幂等）与 `migrate-component-overrides`（一次性播种 7 行含 pr376）。全部字节级校验原样保留。改动与本 PR 同批提交。
- T-15a 服务端协议已合入（PR #388，squash 3f3a734f）：`StateEvent.play_line = 10`、grpc_bridge/standby 透传、standby 触发 `reason=tts_unavailable`。两处与本文草稿不同，以实现为准：① reason 常量与触发异常都放在 `services/agent/src/providers/tts_failover.py`（不是 `services/common/`），触发条件由通用 `except Exception` 收窄为专用 `TTSUnavailableError`——只有非危机单元的全部引擎候选耗尽时由 failover 包装抛出，LLM/ASR/编码错误与危机轮不再误触发；② media-edge 的 `PlayLine` 透传没赶上 #388，属于后续 media-edge PR（含 Go 生成物再生）。
- 未动：T-15b 固件（认 `tts_unavailable` + `play_line`、3 秒探测、本地提示音、hash 负向）、T-15c e2e、音频男声换版。按下方拆分表继续。

## 需求一 release_ops.sh 白名单

### 现状（已核对）

- `scripts/release_ops.sh:45-52` 的 `PREV_OVERRIDE_WHITELIST` 是硬编码 4 列表（服务、持久路径、sha256、/tmp 原件），现有 6 行覆盖 pr376 之前的历次组件切换。
- `derive_prev()`（`scripts/release_ops.sh:310`）读容器 compose chain，extra-file 命不中表白名单即 `fail "override for $c is not whitelisted"`；命中后再过 `validate_override_file`（恰好 image-only、等运行镜像、sha 一致、副本一致）。
- **今天就会被拒。** 线上跑的是 pr376 的 bridge override（`c6af0168`，10-22 07:41 回滚后）。main 的白名单只有 6 行，没有 pr376 这一条，`gh pr diff 377` 显示 #377 加的就是它。所以现在从 main 跑 `freeze` 会直接 fail，下一次整栈发布在第一步就停。这不是将来的风险，是当前状态。
- #377（OPEN，无 review）只补 pr376 一行；#382 修好后重新切组件会再产生新路径+新 sha，需要再开一次 PR（T-07 评审附件《评审-377-20261022.md》第 3 节确认，其中「#382 的 switch 脚本不存在」是按命名规则推断）。#382 走整栈发布则回到纯 compose 链，不需要新行；组件切换是低成本路径，被拒的是它。
- #383（keepalive-180 override）已被 #382 取代，按计划关闭，与本问题无关。
- #377 评审附件第 1 节还引用了切换脚本自身的说明：`switch-pr376-stepfun-20261021.sh:10-11` 明确写着「白名单还没有本 override 的行，下次整栈 freeze 前需要另开 PR 加」。这个预告就是节奏的出处。

### 根因

每次组件切换都要一次 release_ops.sh 的 PR 把新 override 行补进白名单，才能保住下一次 freeze。T-09/T-10 及以后每个组件发布和回滚链都被这个节奏卡一次。#377 和被 #382 取代的 #383 是同一前提下的两次补行，证据指向表格本身。

### 方案

**方案 C，立即执行。合并 #377。** 它是白名单加一行加三处测试枚举加 runbook 一段，CI 已绿，线上实件 sha 与白名单行一致（T-07 评审附件第 1 节核过）。合掉之后今天的 freeze 就能跑。这一步不需要结构改造，也不该等结构改造。

**方案 A，随后做。把记录从代码移到主机状态文件。**
1. 组件切换脚本在切换时把行（service、持久副本路径、/tmp 原件、文件 sha256、image id）追加写入 `/opt/memoria/.cutover/component-overrides.tsv`。
2. `override_spec()`/`derive_prev()` 改读该状态文件（白名单常量整表删除或仅余测试 fixture）。
3. 所有既有校验不动：image-only 形状、sha 与运行镜像一致、freeze 时字节不变。状态文件只回答「我们执行过这次切换」，不是信任根，字节级校验仍是门槛。
4. 新切换脚本直接把 compose `-f` 指向组件 releases 的持久副本，/tmp 原件不再进 chain（存量容器 chain 里的 /tmp 路径靠行里两路径等价校验兜住，逻辑不变）。
5. 迁移 PR 把现有 6 行一次性搬进既有主机状态目录（部署主机已有 `.cutover/` 体系），之后该 PR 同步把 #377 那行一并纳入（或 #377 直接关闭），主机 `/root/memoria-release/release-ops.sh` 重装并更新 TODOLIST 中的 SHA 记录。

- #382 走整栈发布则回到纯 compose 链，不需要新行。但组件切换是低成本路径，结构性修复恰恰是为了让这条路径可用。

**方案 B，不推荐。** 保留白名单，写 `release_ops override add` 半自动生成行。改动小，但每次组件切换仍要代码 PR，只是把补行从手写改成半自动，节奏不变。去掉前提（把记录写进代码），而不是补偿它。

### 风险与要点

- 冻结记录 `prev-release` 只存持久路径的行为不变。
- 测试基线：`scripts/tests/test_release_ops_script.py`（#363 时 ~497 行新增）需要加状态文件读写/迁移用例；CI 全绿才合。
- 主机状态文件是新增数据面：权限、备份与现有 `.cutover/prev-release` 同待遇；runbook `docs/runbooks/release-rollback.md` 的白名单节同步改写。

## 需求二 T-15 兜底话术集成

### 现状（已核对）

- 文案已定稿（T-12 通过，offline-lines-v2 #4）；T-13 男声试听句已合成待试听；T-14 兜底 #4 的审批对象 12:41 被男声统一作废，男声版要重合成后才按 8 条清单重审。T-15 的真机验证与 OTA 发布被 T-14 挡住，代码不依赖这个审批。
- OTA 的边界已经定了（v3.2 第八节）。`share/需求/MVP安全需求-v3.2.md:180` 写「8.3 assets 更新：改离线音频时由工程负责人 USB 重刷 assets 分区，OTA 本期不带 assets」。固件 OTA 只写 app 槽（`memoria_firmware_update.cc:189` 是唯一一处 `esp_ota_get_next_partition`，没有写 SPIFFS 的代码）。所以 T-15 里「固件 OTA 改造」指的不是让 OTA 带音频，而是 app 侧的播放改造，音频换版走 USB 重刷。排期要按这个口径，别按「OTA 推音频」估工作量。
- 不是从零做。已有底子：
  - 主机端 `services/common/llm_failover.py:13-16` 已定义 `LLM_UNAVAILABLE_LINE_ID/TEXT`，`LLM_UNAVAILABLE_AIGC` 在 :38-42。
  - 固件已有消费链：`memoria_offline_menu.h:35` 的 `kLlmUnavailable`、`memoria_protocol.cc:2183` 收到 `close_reason == "llm_unavailable"` 时以 `play_line=false` 进菜单（服务端已经播过）、`memoria_esp_vocat.cc:963-966` 消费信号。播放走 `Application::PlaySound(ogg)`（`memoria_esp_vocat.cc:825`），整文件交给解码器，合规第 3 条要的 OpusTags 保真由这条路经天然满足，前提是新音频按现有 Opus 打包流程生成。
- 两个真实缺口，都在协议层。
  1. `session.close` 的 `reason` 只认 `"llm_unavailable"`（`memoria_protocol.cc:2181-2186`）。v3.2 8.2 要的 `reason=tts_unavailable`、`play_line=true` 在服务端和固件里都不存在，`git grep tts_unavailable` 只命中需求文档。这是 T-15 里最实的一块代码。
  2. 无 `SESSION_CLOSE(reason, play_line)` 结构，`play_line` 只有固件内部的静态布尔，服务端没法让它为真。要把它提升成协议字段。
- 合规硬约束 4 条（`share/合规留证/20261022-offline-fallback/compliance-review.md`）：
  1. 提示音由固件本地播，开始/结束各一次，不写进 wav，测试断言文件原哈希。
  2. 固件播放前校验 sha256，不符不播并告警（负向用例）。固件已有 `crypto_hash_sha256` 可用（`memoria_bootstrap.cc:230-231`），但 assets 侧没有任何 hash 清单，`offline_lines/index.json` 只有 `id/text/file/placeholder` 四个键。要加字段才做得到。
  3. AIGC OpusTags 整文件保真，见上，经 `PlaySound` 路径已满足。
  4. e2e 不写死 3.5 秒（男声实测 3.04 秒）。
- 音频落点：`firmware/esp32/overlay/files/main/boards/memoria/esp-vocat/assets/offline_lines/`。现有 29 条全是女声（`VOICE_ID=zh_female_tianmeitaozi_uranus_bigtts`，`build_offline_stories.py:60-64`）；换成男声要按 RULES 第 14 行整套重合，不只换 #4 一条。这是一次独立的音频换版，不是 T-15 代码能带走的事。

### T-15a 的协议形状（已定稿）

下发链路是 agent 的 `StateEvent` 经 media-edge 转成设备 JSON，所以要改三层。

1. `packages/proto/memoria/media/v1/media.proto` 的 `StateEvent` 加 `bool play_line = 10;`。字段号取下一个空闲值，不动已有字段。生成物 `services/agent/src/voice_core/generated/memoria/media/v1/media_pb2.py` 用仓库 venv 的 grpc_tools 重新生成，不用 homebrew 的 buf（它是 protobuf 7，会把 `Protobuf Python Version` 从 6.33.5 抬到 7.36.2，跟 CI 的 6.33.x 冲突）。
2. agent 侧：`grpc_bridge.emit_conversation_state` 加 `play_line: bool = False` 参数，往下传进 `StateEvent`。`media_session_standby._request_device_standby` 的 `reason` 已经是自由字符串，加一个 reason 常量即可。新的关闭原因定为 `tts_unavailable`，按实现放在 `services/agent/src/providers/tts_failover.py`（与 failover 状态机同处），因为它是 TTS 挂掉的信号，LLM 侧的 `llm_failover.py` 与组合义。
3. media-edge 的 `deviceServerSessionClose` 加 `PlayLine bool \`json:"play_line,omitempty"\``，从 `state.GetPlayLine()` 取值。固件 `memoria_protocol.cc:2181` 的 close 分支加一条 `tts_unavailable` 且读 `play_line`，把它传给 `PostLlmUnavailable(play_line)`，播放走已有的 `OnLlmUnavailable` 路径，不需要新状态机。

触发点。今天 TTS 双失败落在 `tts_failover._engine_candidates` 的最后一个候选失败上，那里 `_put_failure` 之后整条输出流抛 `provider_failed`（`media_session_output_stream.py:1163-1168`）。这是正确的挂点。`_abort_unheard_stream` 保持不动，新的关闭放在它之后：只在「这个 turn 一个字都没发出去」时才请求 standby，因为孩子已经听到半句时再播「我现在有点卡住了」会重读。判断依据是 `emitted_audio` 与 `context.output.audio_sent_for(fence)` 都为假。

设备侧 3 秒探测（v3.2 8.2 第三条）不在 T-15a 里，它是固件行为，归 T-15b。

### 拆分与排期（4 agent 上限下）

| 子项 | 内容 | 依赖 | 建议 agent |
|---|---|---|---|
| T-15a 服务端 `tts_unavailable` 协议 | 见上节三层改动加单测 | 无，可立即开始 | bugfix-iterator |
| T-15b 固件协议与播放 | 固件认新 reason 和 `play_line=true`；设备侧 3 秒探测；提示音本地播；hash 校验负向。音频文件位置留空 | T-15a 的协议形状定稿 | deep-judgment（跨协议与固件） |
| T-15c e2e + 镜像部署验证 | 时长不写死；占位版本验证镜像链路 | T-15a/b、T-14 重审 | 并入 15b 收尾 |
| 音频换版（不属于 T-15 代码） | 29 条整套重合男声、试听、合规、USB 重刷 assets | T-14、T-30 至 T-32 | 你与工程负责人 |

音频合规未过前，代码 PR 不得携带新音频文件（AGENTS.md 儿童可听内容门禁 + T-14 未审）。

## 推荐排期

1. **合并 #377**（今天，方案 C）。线上现在是 pr376 链，main 的 `freeze` 今天就会拒。这是最短路径，deep-judgment 或直接由你点合并。
2. **白名单结构修复 PR**（方案 A）。解决下一次组件切换的同一问题。deep-judgment，改动小但 release 链敏感，可与第 1 步并行开工但不抢在它前面合。
3. 并行开 T-15a（bugfix-iterator）。
4. 第 2 步合入后，T-15b 上线。
5. #383 按计划关闭，它被 #382 取代，与本问题无关。
6. T-15 真机与发布等 T-14。
