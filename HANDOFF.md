# Memoria 当前交接

## 当前生产快照

- **最近生产收据**：2026-10-02 19:05–19:06（CST）media-edge 组件发布 `20261002-late-progress-v1`（tag → `f3fe8742`，#174 分支头；合并提交 `7499b0e2`，合并后 main 的树 `bfa4c7bd…` 与 tag 同树），只换 media-edge 一个容器（取消代的迟到 `playback.progress/started` 不再关设备 WSS，TODOLIST N-11），其余 memoria 容器的镜像与启动时间不变；无 schema、无 env、无 compose 变更。回滚目标 `20260930-late-receipt-v1`（`media-edge-rollback.override.yml`）。详见下一节。
- **最近一次整栈收据**：2026-10-02 18:06–18:14（CST）整栈发布 `20261002-device-memory-v1`（tag → `4101c7e4`，#172 分支头；合并提交 `324d1c9d`，合并后 main 的树 `b4e80839…` 与 tag 同树），control-api、bridge、speaker-model 三角色换 tag；含 #171（N-8：首帧前被无文字的 VAD 抢掉的回复改为扣住，TODOLIST N-8）与 #172（设备回复带上绑定人已确认的记忆，TODOLIST N-9；release-ops PREV 前移）。无 schema、无 env 值变更，仍是 `LLM_PROVIDER=deepseek`；media-edge 不在整栈内，随后单独换成组件发布 `20261002-late-progress-v1`（见上一条）；整栈回滚目标是 `20261002-stop-pin-v1`（2026-10-02 13:56–14:01 整栈发布，`b3e5811e`，三角色都在它的纯链上；它的 env 就是 `freeze` 快照里的当前配置）。`verify-load`、`freeze`（合并前）与 `env`、`schema`、`cutover`、`finish`（合并后）每步 PASS，详见下一节。生产 `/etc/memoria-agent.env` 常开 `MEDIA_PCM_TAP_DIR=/tmp/media-pcm-tap`（容器 tmpfs，每会话 4 MB；新文件打开前按最旧优先清理，目录上限 40 MiB），原始上行音频留在容器内存直到重启。

| component | actual image/tag | OCI digest | revision | health | restarts | startup time | receipt | rollback target |
|---|---|---|---|---|---:|---|---|---|
| Control API | `memoria-control-api:20261002-device-memory-v1` | `sha256:5f8865e7…`（服务器 image id） | `4101c7e44e07dc82eb3edfd841162270e95e0db6` | healthy | 0 | `2026-10-02T10:12:47Z` | `/opt/memoria/releases/20261002-device-memory-v1/.cutover/` | `memoria-control-api:rollback-20261002-device-memory-v1-pre`（= `20261002-stop-pin-v1`） |
| Bridge | `memoria-agent:20261002-device-memory-v1` | `sha256:843c1e83…` | 同上 | healthy | 0 | `2026-10-02T10:13:00Z` | 同上 | `memoria-agent:rollback-20261002-device-memory-v1-pre`（= `20261002-stop-pin-v1`） |
| Speaker Model | `memoria-speaker-model:20261002-device-memory-v1` | `sha256:3faa9e07…` | 同上 | healthy | 0 | `2026-10-02T10:12:40Z` | 同上 | `memoria-speaker-model:rollback-20261002-device-memory-v1-pre`（= `20261002-stop-pin-v1`） |
| Media Edge | `memoria-media-edge:20261002-late-progress-v1` | `sha256:8c381535…`（服务器 image id） | `f3fe8742177e4002db652203b1fadffa549d1098` | healthy | 0 | `2026-10-02T11:05:24Z` | `/opt/memoria/component-releases/20261002-late-progress-v1-media-edge/` | `memoria-media-edge:20260930-late-receipt-v1`（`media-edge-rollback.override.yml`） |

- **服务器磁盘（2026-10-02 15:01 清理后 41 GB / 37%，18:14 发布 `20261002-device-memory-v1` 后 45 / 118 GB / 40%）**：清理后 `/var/lib/containerd` 15 GB，`/opt/memoria/incoming` 只留 `20261002-stop-pin-v1`、`20261002-stop-diag-v1` 与最近三个 media-edge（发布后多一个 `20261002-device-memory-v1`，约 1.5 GB，下一次清理时随「当前 + 紧邻回滚」规则处理）；memoria 镜像只剩当前与紧邻回滚、runtime-base、media-edge、sensevoice（共 18 个 tag）；19:06 发布 media-edge `20261002-late-progress-v1` 后共 25 个 tag（含这次新增的 1 个 media-edge tag，现共 4 个），根分区仍是 45 / 118 GB（40%）。每次整栈约占 4 GB，约每 6 次要清一次；收据见下一节。
- **候选可见性状态**：已随整栈发布上线（契约提交在 main 上为 `0059368`，早期记录中的 `f7c4c2a` 是合并前哈希）。普通 search/context 只返回 confirmed 且无 active 冲突，`include_candidates=true` 仅供审核与评测。真实 PG 上的 candidate 行为与线上带鉴权读口尚无单独收据。
- **评测基线边界**：四份 2026-09-23 评测收据统一为 `receipt_scope=parent_baseline`、`source_commit=3ccba9c69a77d3bc97f3a48e5f702ab0a4da7948`；它们产生于候选可见性提交之前，只证明上线前基线，不证明当前线上版本的召回质量。
- **发布身份**：`20261002-device-memory-v1` / `4101c7e4`；`/opt/memoria/current` → `releases/20261002-device-memory-v1`。上一栈 `20261002-stop-pin-v1` / `b3e5811e` 为回滚目标。media-edge 组件：`20261002-late-progress-v1` / `f3fe8742`（编排仍在 `releases/20260930-local-stop-v2` + 组件 override；该树的 compose 必须用 `sudo env MEMORIA_RELEASE_TAG=… MEMORIA_RELEASE_COMMIT=… docker compose …` 跑，`ubuntu` 读不了 `/etc/memoria-agent.env`）。
- **设备固件**：开发板（`dev_atk_a4cb8fd6095c`，MAC `90:e5:b1:d7:83:2c`）2026-10-02 19:43 经 USB 刷入 build 20（USB 串口 `wake` 调试命令，TODOLIST N-12，未发布 OTA）；唤醒方式在手机端是 `button`（只点屏幕）。测试时唤醒机器人走 USB：常驻串口记录进程加 `scripts/voice_soak_serial_command.py wake`，不再播唤醒词。
- **未关闭缺陷**：P0-03 仍开放（缺陷 A 核心续问边界与工具查询最终回答已在 09-24、09-25 真机走通；TLS/WSS 自动重连保留观察项）；缺陷 B 的输入电平摆动/近讲削波仍需固件 AGC/AEC；F2 禁止源 barge 尚未取得设备旁的真实复现证据。
- **下一步必须动作**：N-12（固件 build 20 的 USB 串口 `wake` 命令）已于 2026-10-02 19:43 刷入并在真机上验证（见下一节），当前没有排在前面的开发项；用户放开电脑语音测试之后，才做 N-8 的真机 A/B、跨会话记忆与 N-11 语音停止系列的真机验证（N-11 已于 2026-10-02 随 media-edge 组件发布 `20261002-late-progress-v1` 上线）。更早的事项：设备对话归档与设备信任分档已随 `20261001-device-archive-v1` 上线，开关 `MEMORIA_BOUND_DEVICE_TRUST_ENABLED` 已于 2026-10-01 00:30 打开（收据见下一节），验收清单见运维手册「设备信任开关」。用户 2026-10-01 新增六项需求（唤醒方式设置、空闲熄屏、发布与刷机、回顾为空、电脑长稳对话、说话风格评审）见 [TODOLIST「2026-10-01 新需求」](TODOLIST.md)。当日遗留已汇总到 [TODOLIST「2026-09-28 收尾待办」](TODOLIST.md)：伙伴页选一次绵绵、嘈杂环境验证 #83；需决定是否发布固件 OTA；待查回复规划 `no_verified_runtime_profile` 兜底；12-17 前换证书。之后按验收清单验 P0-04 产品决定、P1-11 三种绑定与隔天记忆、P1-03 孩子人格、P2-04 与 P0-03 剩余矩阵；`direct_real_device_verified=false`、`full_duplex_verified=false`、`student_safety_loop_verified=false` 保持不变。
- **固定参考**：[发布、恢复与回滚运维手册](docs/runbooks/release-rollback.md)、[空间治理运维基线](docs/runbooks/operations-space-governance.md)、[删除域与 seal 契约](docs/compliance/delete-domains.md)、[2026-09-20 及更早历史归档](docs/HANDOFF-archive-before-0920.md)、[2026-09-16 至 2026-09-23 历史归档](docs/HANDOFF-archive-0916-0923.md)。

## 2026-10-02 固件 build 20：USB 串口 `wake` 调试命令（PR #176，TODOLIST N-12；不属于整栈，USB 刷入，未发 OTA）

- **背景**：用户 2026-10-02 决定去掉关键字唤醒（误唤醒多）、改点屏幕唤醒，电脑模拟测试今后不能再播唤醒词。build 19 的 USB 串口只出日志（主控制台是 UART0，USB-Serial-JTAG 是只出不进的次级控制台），测试时没有任何办法从电脑唤醒机器人。
- **范围**：固件 `memoria_usb_command.h`（精确的 `wake` 解析、行装配器、与点屏同一套门控的唤醒策略，主机测试覆盖）、板级 `memoria_esp_vocat.cc`（`usb_command_task` 以只读方式打开次级控制台设备节点 `/dev/secondary`，每 25 ms 非阻塞轮询接收 FIFO，优先级 1、固定核 0；`HandleUsbWake()` 在配对/启动之后按 `UsbWakeDecisionFor` 决定，只有空闲且 wake_mode 含屏幕时才调用 `app.WakeWordInvoke("usb_wake")`）、build 20、`check-overlay.sh` 三条新断言。主机端：`voice_soak_serial_logger.py` 加 unix socket 命令口（只接受 `wake` 与 `ping`），新增客户端 `voice_soak_serial_command.py`，`voice_soak.py` 的唤醒步骤改走 USB（发不出或被拒就失败，不退回唤醒词，删掉 `--wake-clips`/`--wake-voice`）；`firmware/esp32/scripts/flash_backup.py`（见下）。无服务端改动，无 protocol 变更。
- **刷机（19:36–19:44，全程只写 app 与 otadata）**：写前整槽（`0x20000/0x3f0000`，md5 `dda6e8d0…`）、引导+分区表（`13e19405…`）、nvs+otadata+phy（`feced13c…`）、身份区（`d0ebd792…`）备份到 `firmware/esp32/artifacts/backups/pre-usb-wake-20261002/`，每个文件都与芯片端 MD5 一致；`esptool write-flash 0x20000 <app> 0xd000 <8 KiB 的 0xff>`，app 3,331,728 字节（sha256 `f8b10e47cf5029f0…`），两段都 `Hash of data verified.`；写后设备端 MD5：引导+分区表、nvs（`07bdb426…`）、phy_init（`6ae59e64…`）、身份区（`d0ebd792…`）、assets（`e6fa91d8…`）与写前逐项相同，`ota_0` 由 `dda6e8d0…` 变为 `c55a5afc…`，otadata 在首次启动后由引导程序重写成与写前相同的记录（`11c30a9a…`）。回滚：`esptool … write-flash 0x20000 ota0-before.bin 0xd000 <8 KiB 0xff>`，备份文件在上面的目录。
- **本板的 esptool 读取缺陷**：USB-Serial-JTAG 上 `esptool read-flash` 在个别 4 KB 块（`0x106000` 起，4 MB 槽里共 6 处）确定性失败（「No more data to read from the serial port」，失败后芯片留在 stub），同样的字节分成 2 KB 读就好；起初两次整槽备份都死在 `0x106000`，才换成 `flash_backup.py`（分块读、失败后重连改读小帧、用芯片端 MD5 核对，整槽 84 秒、6 次重连）。写入不受影响。机器人在刷机前约 19:34–19:36 从 USB 上掉线又回来，原因没查。
- **真机验证**（19:44–19:48，常驻串口记录进程 `LOGGER: started …` + 命令口）：开机 `MEMORIA_FIRMWARE_BUILD=20; slot=ota_0`，2.7 s 后 `usb command console ready commands=wake`，`wake mode from NVS: button`；`wake` 之后 44 ms 记 `usb wake accepted wake_mode=button`，`idle → connecting`，亮屏，媒体会话 v2 协商，约 2.7 s 后 `Wake word detected: usb_wake`、`connecting → listening`；对话中（`speaking`，state=7）再发 `wake` 记 `usb wake ignored reason=not_idle state=7 wake_mode=button` 且对话照常，19:45:32 自己回到 `idle`；边缘侧该会话 `epoch=2391` 以 `conversation_end_explicit` 正常关闭，bridge 没有报错。压测驱动自己的监视器同样走通（`wake()` 0.16 s 内 `connecting`，第二次 `wake()` 的拒绝原因读到 `not_idle`），没有播放任何音频。
- **没有验证的**：`wake_mode=keyword` 时拒绝（手机端设置现在是 `button`，策略只在主机测试里覆盖，我没有改设置）、配对/启动阶段拒绝（同上，源码级测试）、用 USB 唤醒跑完整的电脑长稳对话与 N-8、N-11、跨会话记忆的真机验证（等用户放开电脑语音测试）。

## 2026-10-02 media-edge 组件发布 20261002-late-progress-v1（PR #174；取消代的迟到回执不再关设备 WSS）

- **背景**：N-11。语音停止时 bridge 发 CANCEL，13–117 ms 之后 edge 收到设备在处理 flush 之前发出的、对刚取消那一代的 `playback.progress`（或 `playback.started`）；账本接受，Voice Core 以 stale generation 拒绝，`handlePlaybackReceipt` 发 `session.error playback_receipt_rejected` 并关连接，设备约 7 s 才回到聆听，这段时间的下一句话丢失（`20261002-stop-pin-v1` 之后 17 次语音停止里 2 次，普通停止也会触发；`20260930-late-receipt-v1` 只覆盖了账本拒绝的回执与已替换代的 `ended/error`）。
- **范围**：只动 `services/media_edge/`：`device_ws_uplink.go`（账本接受之后、动播放窗口之前先看这一代是否已被替换——已替换则记 `media edge dropped late receipt for a replaced generation`（`stage=precheck`）后丢弃，不转发、不发 `session.error`、不关连接、不碰播放窗口；取消恰落在检查与转发之间时同样丢弃并收掉刚打开的窗口（`stage=forward`）；其它失败仍可重试地关连接）、`bridge_runtime.go`（`GenerationReplaced` 同时看边缘 Session 与 Voice Core 自己的会话）、新测试 `device_ws_late_receipt_test.go` 与测试替身的两个钩子、TODOLIST。无 Python、无 schema、无 env、无 compose 变更，其余 memoria 容器不动。
- **过程**：PR #174（分支头 `f3fe8742`，annotated tag `20261002-late-progress-v1`，推送在合并之后）。本机先用 CI 同版本 golangci-lint（官方镜像 `golangci/golangci-lint:v2.12.2`，装 `libopus-dev`，默认 linter）0 issues，干净提交树上 `gofmt`、`go vet`、`go test`（310 项）通过；CI 全绿（11 项通过、4 项按路径跳过）后 19:04 合并（merge commit `7499b0e2`，合并后 main 的树 `bfa4c7bd…` 与 tag 同树）。干净 detached worktree 构建 linux/amd64 镜像 18:52（revision/version/role 标签逐个核对，二进制含新日志串）；`docker save`（44 MB，sha256 `44a8f372…`）上传到 `/opt/memoria/incoming/20261002-late-progress-v1-media-edge/`，服务器侧 `sha256sum -c` 通过后 `docker load`（服务器 image id `sha256:8c381535…`；本机 containerd 存储里同一镜像的 id 是 `84f4f3b2…`，两边 id 不同是正常的，以 revision/version 标签与二进制 sha256 为准）；组件 override 与回滚 override 写到 `/opt/memoria/component-releases/20261002-late-progress-v1-media-edge/`。切换前设备已离线（edge 最后一条设备 WSS 日志是 14:48 CST），确认无会话后 19:05 切换：`cd /opt/memoria/releases/20260930-local-stop-v2 && sudo env MEMORIA_RELEASE_TAG=20260930-local-stop-v2 MEMORIA_RELEASE_COMMIT=… docker compose -f docker-compose.production.yml -f …/media-edge-component.override.yml --profile media-runtime up -d --no-deps --no-build media-edge`（生产机上 `ubuntu` 读不了 `/etc/memoria-agent.env`，compose 必须走 `sudo env`；渲染配置含 env 展开值，只取镜像行后立即删除）。
- **线上核对（只读）**：只有 media-edge 一个容器变（切前后全部容器的「名字 镜像 启动时间 重启次数」快照 `pre-all.txt` / `post-all.txt` 逐行对比，其余容器不变，两份快照存在 `component-releases/20261002-late-progress-v1-media-edge/`）；新容器 `memoria-media-edge:20261002-late-progress-v1`，revision `f3fe8742…`，healthy，restarts 0，启动 `2026-10-02T11:05:24Z`；编排链 `releases/20260930-local-stop-v2/docker-compose.production.yml` + `component-releases/20261002-late-progress-v1-media-edge/media-edge-component.override.yml`；运行中的二进制（`docker cp` 取出，容器没有 shell）sha256 `88d6f53b…` 与本机构建出的相同，含新日志串；启动日志没有 WARN/ERROR（Voice Core bridge、设备 WSS 入口、关闭上报都已起来）；外部 readiness 200，未带凭证的设备入口 401；根分区 45 / 118 GB（40%，新增约 95 MB 镜像与 44 MB 上传包）。设备不在线（切换前 10 分钟 edge 没有任何设备日志），设备重连与真机对话待下次唤醒。
- **回滚**：`cd /opt/memoria/releases/20260930-local-stop-v2 && sudo env MEMORIA_RELEASE_TAG=20260930-local-stop-v2 MEMORIA_RELEASE_COMMIT=… docker compose -f docker-compose.production.yml -f /opt/memoria/component-releases/20261002-late-progress-v1-media-edge/media-edge-rollback.override.yml --profile media-runtime up -d --no-deps --no-build media-edge`（回到 `20260930-late-receipt-v1`，镜像仍在服务器上）。
- **没有验证的**：真机。设备自 14:48 起不在线，且关键字唤醒已停用、USB `wake` 命令（N-12）还没做，所以没有做发布后的语音停止系列。验收看 edge 日志：停止后偶发的 `media edge dropped late receipt for a replaced generation … stage=precheck|forward`（预期约占停止次数的一成），同时不再出现 `WSS handler rejected … type=playback.progress` 与设备串口停止后紧跟的 `playback_receipt_rejected` / `errno=104`；需要至少 20 次语音停止才有说服力。发布前基线（旧 edge 容器 09-30 03:59Z 到切换，日志随旧容器一起没了，只记在 TODOLIST N-11）：回执类 `WSS handler rejected` 共 3 条（`playback.started` 10-01 14:15Z，`playback.progress` 10-02 06:14Z 与 06:18Z）。同类未处理：按钮停止带着已被替换代的围栏（`stop_rejected` 并关连接，09-30 04:30 一次），先看发布后日志里的频率。

## 2026-10-02 整栈发布 20261002-device-memory-v1（PR #171 + #172；预备好的回复不再被无文字的 VAD 抢掉，设备回复带上绑定人已确认的记忆）

- **背景**：N-8（第四轮起发现、第八/九轮用粉红噪声确定性复现）：有 barge-in 时设备 VAD 一起，地板立刻交给用户，第一帧之前的回复被判 `superseded`；噪声边沿之后没有任何文字，已经听对、已经准备好的回答就整句丢了。跨会话记忆（N-9）：设备会话恒走本地安全计划，计划里没有记忆，机器人记不住上一次会话说过的事。
- **范围**：相对 `20261002-stop-pin-v1`：①`media_session_output_stream.py`——首帧前地板只被「没有文字证据的用户轮」占着时扣住首帧（复用 evidence-less 扣住上限 3 s、每次 vad.start 再延 1.5 s、最多 6 s，另有 7 s 兜底），有文字照旧 `superseded`；②`reply_pipeline.py`——设备绑定的属主且签名档案带 `memory_recall_private` 时，与响应计划并行按最终文字向 `/v1/interaction/context-prefetch` 取记忆（调用 2 s 上限、计划到后最多再等 0.6 s），`memory_claim` 冻进本轮快照、经装配器作为 `DATA.grounded_items` 交给模型，人格特征不带，失败即无记忆；③`companion_turn_policy.py`——设备绑定属主有该授权时的范围说明（只在自然涉及时一句话带出、不罗列、没有就说记不得）；④`agent.py` 星期短语判断挪到 `realtime_information`（预算 473 → 463）；⑤`release_ops.sh` 的 PREV。无 schema、无 env 值变更、无 control-api 路由变更（`/v1/interaction/context-prefetch` 是已有端点）。
- **过程**：PR #171（N-8）与 #172（记忆 + 发布提交，叠在 #171 之上）18:00 开；#172 分支头 `4101c7e4`（annotated tag `20261002-device-memory-v1`，推送在合并之后）；detached worktree 构建三镜像 18:03:05–18:05:14（腾讯 PyPI 镜像，层缓存，共 2 分 09 秒；revision/version/role 标签逐个核对）；打包 18:05（verifier `aff0c2ec…` 与上次相同，manifest `025b96d9…`，source `73e553db…`，images `b51b169b…`）；种子上传（基座 `20261002-stop-pin-v1`）18:06:05–18:06:28（共 1.53 GB，实发 100.42 MB）双端校验 PASS；服务器 `release-ops.sh` 换成新版（sha256 `4525387a…` 两端一致，旧版备份为 `release-ops.sh.pre-20261002-device-memory-v1`）；`verify-load` 18:06:31–18:07:13、`freeze` 18:07:17（库 dump `memoria-pre-20261002-device-memory-v1.dump`，3.6 MB）PASS，二者在合并前完成；CI 全绿（#171 10 项通过、5 项按路径跳过；#172 11 项通过、4 项按路径跳过）后 18:10:47 合并 #171（合并提交 `4e26aff1`）、18:10:59 合并 #172（合并提交 `324d1c9d`），均为 merge commit；核对合并后 main 的树 `b4e80839…` 与 tag 的树相同，18:11:31 推 tag；`env` 18:11:38–18:12:16（无 env 键差异，FunASR、Qwen 实时搜索、DeepSeek、豆包冒烟全 PASS）、`schema` 18:12:27 PASS；确认 `media_active_sessions=0.0` 后 `cutover` 18:12:38–18:13:07（speaker-model 18:12:39、control-api 18:12:46、bridge 18:12:59，全程约 30 秒）、`finish` 18:13:10–18:14:23 PASS（readiness 刷新 `20261002-device-memory-v1`（deepseek），对外 200；media-edge 在 10:13:04Z 重拨上新 bridge）。
- **线上核对（只读）**：三角色 `20261002-device-memory-v1` / `4101c7e4`，重启 0，全部 healthy；media-edge 仍是 `20260930-late-receipt-v1`；readiness `ready 20261002-device-memory-v1`，外部 200；运行中的 bridge 镜像里有 `_floor_held_by_textless_turn`、`_fetch_device_memory`、`_DEVICE_MEMORY_GRACE_S`；新容器切流后 6 分钟的日志没有 error/exception/warning；根分区 45 / 118 GB（40%）。
- **回滚**：`release-ops.sh rollback` 恢复到 `20261002-stop-pin-v1`；切前镜像 `memoria-*:rollback-20261002-device-memory-v1-pre`（三个都在服务器上，id 与 stop-pin-v1 的服务器镜像一致：agent `3755cd6b…`、control-api `f3c49b20…`、speaker-model `f0c16495…`）。无 schema 变更。
- **没有验证的（等用户放开电脑语音测试）**：①N-8 真机 A/B——同一套粉红噪声配方（`phantom_stop.py … --noise-starts`），丢失应从 3/15 降到 0，日志应出现 `media prepared reply held for a user turn with no words` → `released waited_s=…`；②跨会话记忆——生产 `memory_claims` 现在是 0 行，先说「帮我记住我最喜欢蓝色」，等归档编译后重新唤醒再问；日志应出现 `device memory attached … items=N`。唤醒方式见 TODOLIST N-12（固件串口调试命令，发布后的下一步），N-11（edge 迟到回执）随后已由 media-edge 组件发布 `20261002-late-progress-v1` 修复（见上一节）。

## 2026-10-02 服务器磁盘清理（用户授权「先清理磁盘」）

- **起因**：只读核对发现根分区 80 / 118 GB（71%），10-01 清理后（33%）不到两天涨了 43 GB，期间 11 次整栈发布（约 4 GB/次）：`/var/lib/containerd` 38 GB（66 个镜像、109 个 tag，只有 10 个在用）、`/opt/memoria/incoming` 19 GB（12 个整栈上传包各约 1.5–1.8 GB）。上一次是拖到 98% 才清的。
- **做法**（按运维手册「发布制品的手工清理」）：只读盘点 → 候选清单（75 个镜像 tag、11 个 `incoming/*` 目录，sha256 `84b22d19…` / `90e55479…`）逐项核对 → 带校验的分阶段脚本（`/root/memoria-release/cleanup-20261002.sh`，sha256 `9fdbea52…`，每阶段先过门禁：无发布进程、容器 healthy、内外 readiness、保留集镜像都在、候选不含保留集或运行中镜像）→ 阶段一 `docker rmi` 逐项（12 秒，不用 `docker system prune`、不动卷）→ 复核 → 阶段二精确目录删除 → 复核。
- **保留**：`memoria-{agent,control-api,speaker-model}` 的 `20261002-stop-pin-v1`、`20261002-stop-diag-v1`、`rollback-20261002-stop-pin-v1-pre`；`memoria-media-edge` 三个 tag；`memoria-agent-runtime-base` 全部 tag；`memoria-sensevoice-asr`；所有非 memoria 镜像；`incoming/` 的 `20261002-stop-pin-v1`、`20261002-stop-diag-v1` 与三个 media-edge 目录。**没动**：`releases/*` 旧源码树（1.2 GB）、`component-releases`（0.5 GB）、journald（1.2 GB）、数据卷、`/var/www/memoria-releases`（1.4 GB）、WMS/saas。
- **结果**：根分区 80 → 41 GB（71% → 37%）；containerd 38 → 15 GB；`incoming` 19 → 3.0 GB。清理后 10 个容器全部在线（带健康检查的都是 healthy），内部 `/health/ready` 与外部 `https://aigcnice.com:8443/memoria-api/health/ready` 均 200，回滚镜像与被运行容器引用的 compose 文件（`releases/20261002-stop-diag-v1`、`20260930-local-stop-v2`、`20260827-architecture-split-v1`、`20260823-210222-voice-fix`、`current/infra`、media-edge 组件 override）逐项核对存在。
- **收据**：服务器 `/root/memoria-release/cleanup-20261002-images.log`、`cleanup-20261002-incoming.log`，清理前快照 `cleanup-20261002-pre/`（`docker ps`、镜像清单含完整 id、卷、`incoming` 列表、`df`）与两份候选清单。
- **仍待**：`scripts/docker_image_retention.sh` 仍给不出候选（TODOLIST「定期运维」），下一轮还是手工清单。

## 2026-10-02 整栈发布 20261002-stop-pin-v1（PR #169；播放期间的停止词接管「没有文字的轮次」占住的终点）

- **背景**：N-10。soak10 的诊断日志显示，机器人说话时一次没有文字的 VAD 误触发会占住 `turn_endpoint_sample`，空轮次永远提交不了，要等 ASR 尾部超时（约 3–6 s）才丢弃，窗口内所有 final（包括停止词）都被 `endpoint_already_pinned` 拒绝。
- **范围**：相对 `20261002-stop-diag-v1` 只动 `services/agent/src/voice_core/media_session_playback_stop.py`（`_pinned_turn_holds_no_words`：停止词在占点之后才开始、被占轮次自己的范围内时间线没有文字、不是时钟/告别/实时查询的内容类占点且无强制文本时，替换这个占点，并用 `restart_endpoint_bounds` 作废旧占点的尾部超时；日志 `media early playback-stop endpoint … replaced_empty_endpoint=<旧终点>`）、两个测试文件和 `release_ops.sh` 的 PREV。无 schema、无 env 值变更、无 control-api 行为变化。
- **过程**：tag `20261002-stop-pin-v1` → `b3e5811e`（annotated，推送在合并之后）；干净 detached worktree 构建三镜像 13:53:53–13:56:09（腾讯 PyPI 镜像，层缓存，2 分 17 秒），打包 13:56:15–40（verifier sha `aff0c2ec…` 与上次相同，manifest `74ba6aa8…`）；种子上传 13:56:55–13:57:17（共 1.53 GB，实发 101.87 MB），新 `release-ops.sh`（sha256 `7ee7a231…`）装到服务器，旧版备份为 `release-ops.sh.pre-20261002-stop-pin-v1`；`verify-load` 13:57:30–13:58:13、`freeze` 13:58:17（库 dump `memoria-pre-20261002-stop-pin-v1.dump`，3.58 MB）PASS；CI 全绿（10 通过、5 按路径跳过）后 13:58:33 合并 #169（merge commit，合并后的树与 tag 的树相同），推 tag；`env` 13:58:45–13:59:24（无 env 键差异，各提供商冒烟全过）、`schema` 13:59:29 PASS；确认 `media_active_sessions=0.0` 后 `cutover` 13:59:41–14:00:10、`finish` 14:00:13–14:01:27 PASS。
- **线上核对（只读）**：三角色 `20261002-stop-pin-v1` / `b3e5811e`，重启 0，全部 healthy；media-edge 仍是 `20260930-late-receipt-v1`；readiness `ready 20261002-stop-pin-v1`（deepseek），外部 200；运行中的 bridge 镜像里有 `_pinned_turn_holds_no_words`。cutover 时一个设备的空闲连接被断开（`Cancelling all calls`），edge 到 Voice Core 的通道约 4 s 后重连（预期）。
- **回滚**：`release-ops.sh rollback` 恢复到 `20261002-stop-diag-v1`；切前镜像 `memoria-*:rollback-20261002-stop-pin-v1-pre`（三个都在服务器上，id 与 stop-diag-v1 的服务器镜像一致）。无 schema 变更。
- **真机复核（第九轮 14:06–14:21，现网已是新版本）**：
  - 正常停止没有退化：8 次「故事 +『别说了』」里有回复的 6 次全部 0.27–0.72 s 停下。
  - **空占点被接管**：噪声时刻扫描 15 次里 11 次停下，其中 5 次接管了空占点（`replaced_empty_endpoint=620800 / 499520 / 673600 / 2304960 / 2478400`），占点都比停止词 final 早 2.11–2.29 s（`endpoint_ms`）、范围内没有文字，停止词 final 到 `spoken stop interrupted reply` 28–69 ms；0 次停止词被占点挡掉（修复前同配方 5 次里 1 次完整被挡，故事讲满 29.7 s）。
  - 没有测到的：N-8（回复被假 VAD 抢掉）仍在，且这一天更容易复现——固定 +2.7 s 的噪声 12 次里 11 次回复被抢掉；自然发生也有（回归系列 8 次里 1 次，另 1 次是设备 VAD 没响应停止后的下一句，30 s 静默关会话）。「停」单字被识别成停止词的比例仍低。
  - **p03 的设备连接重置已归因（2026-10-02，TODOLIST N-11）**：不是本次修复引入的，普通停止也会触发（发布后 17 次停止里 2 次）。bridge 发布 CANCEL 之后 13–117 ms，edge 收到设备在收到 flush 之前发出的、对刚取消那一代的 `playback.progress`；账本接受，但会话已前进到替换代，`SendPlaybackProgress` 报 stale generation，`handlePlaybackReceipt` 发 `session.error playback_receipt_rejected` 并关连接，设备 `recovering` 约 7.3 s。`20260930-late-receipt-v1` 只放行了「账本拒绝且该代已被替换」与已替换代的 `ended/error`。修法（该代已被替换时丢弃回执）已随 media-edge 组件发布 `20261002-late-progress-v1` 上线（见该发布一节）。另有一条 bridge `media reply failed … StopAsyncIteration`（被新一轮取代的回复上 TTS 流结束的竞态），与停止路径无关。
  - 详见 `docs/acceptance/run-20261001-longsoak/findings.md` 第九轮。

## 2026-10-02 整栈发布 20261002-stop-diag-v1（PR #168；语音停止的诊断日志，行为不变）

- **背景**：第六轮（`20261001-deepseek-flash-v1`）讲故事时「别说了」两次没停（TODOLIST N-10）：原始上行音频和识别结果都对，但服务端没有任何日志说明是哪个守卫挡掉了停止词。
- **范围**：相对 `20261001-deepseek-flash-v1` 只动 `services/agent/src/voice_core/media_session_playback_stop.py`（`_maybe_pin_playback_stop` 守卫顺序不变，每个守卫挡下设备 final 时记 `media playback-stop not taken reason=…`，带是否停止词、是否回声、终点被谁占、起点下限等字段，不记原文，诊断出错时 fail-open）、`media_session_commit.py`（`_log_asr_rejection` 带同样字段，接收阶段四处静默丢 final 的返回补了日志）、新测试 `test_playback_stop_diagnosis.py` 和 `release_ops.sh` 的 PREV。无 schema、无 env 值变更、无 control-api 行为变化。
- **过程**：tag `20261002-stop-diag-v1` → `26b0d53a`，合并提交 `8f39e324` 与 tag 同树；腾讯镜像构建；种子上传 10:29:05（共 1.53 GB，实发 99 MB），新 `release-ops.sh`（sha256 `24bcc8cd…`）上传，旧版备份为 `release-ops.sh.pre-20261002-stop-diag-v1`；`verify-load` 10:29:31–10:30:17、`freeze` 10:30:18（库 dump `memoria-pre-20261002-stop-diag-v1.dump`，3.5 MB）PASS；#168 合并（同树），推 tag；`env` 10:30:32–10:31:10（FunASR、DeepSeek、QwenRealtimeSearch、豆包冒烟全过；这一步只把 control-api env 里的 `MEMORIA_RELEASE_TAG/COMMIT` 改成本次发布）、`schema` 10:31:12 PASS；确认 `media_active_sessions=0` 后 `cutover` 10:31:13–10:31:42、`finish` 10:32:57 PASS。
- **线上核对（只读，2026-10-02 当天）**：三角色 `20261002-stop-diag-v1` / `26b0d53a`，重启 0，全部 healthy；media-edge 仍是 `20260930-late-receipt-v1`；readiness `ready 20261002-stop-diag-v1`、`llm.provider=deepseek`，外部 200。cutover 时一个设备会话被断开（`Cancelling all calls`），edge 到 Voice Core 的通道约 4 s 后重连成功（预期）。
- **回滚**：`release-ops.sh rollback` 恢复到 `20261001-deepseek-flash-v1`；切前镜像 `memoria-*:rollback-20261002-stop-diag-v1-pre`（已核对三个都在服务器上，id 与 deepseek-flash-v1 的三角色一致）。无 schema 变更。
- **真机复核（第七轮 10:47–10:55，短场景 16 步，音量 65）**：14 句说出口、13 句有回复；唤醒 14 次只成功 2 次（音量 65 贴着唤醒阈值），两步因唤醒失败被跳过。讲故事时只有第二个故事（t016）的「别说了」在 0.53 s 停下；其余「停下」都是回复自然讲完——t004 的「别说了」比故事结束还晚到（`no_reply_in_flight`），t008 的「停。」没有被识别成停止词。新日志给出了机制：回复播放期间一次没有文字的 VAD 误触发占住 `turn_endpoint_sample`（`media turn has no text … stage=endpoint`），之后约 3–6 s 内每条 final 都被 `endpoint_already_pinned` 挡回，直到 `media turn discarded after ASR tail timeout`。本轮 3 个窗口里被挡的 10 条 final 没有一条是停止词，所以第六轮那两次失败仍是推断，不是亲眼所见。详见 `docs/acceptance/run-20261001-longsoak/findings.md` 第七轮。
- **发布后清理**：删除 4 个废弃 worktree、10 个本地分支、2 个远端分支和约 3 GB 的发布包；本地 main 快进到 `8f39e324`；分支 `wip/tts-stream-hardening`（未评审、有 bug、没有远端副本）有意保留。
- **后续**：N-10 的修复（停止词接管空占点）已由整栈 `20261002-stop-pin-v1` 发布，见下一节。

## 2026-10-01 整栈发布 20261001-deepseek-flash-v1（PR #167；所有文本模型改用 DeepSeek 官方 DeepSeek-V4.1-Flash）

- **背景**：用户要求把 LLM 全部换成 DeepSeek 官方的 DeepSeek-V4.1-Flash（API 模型名 `deepseek-flash`）。原来的 `LLM_PROVIDER=deepseek` 只换回复模型，分类器、危机判断、记忆/人格抽取仍写死 DashScope；而 DeepSeek 不认 `enable_thinking`、默认思考，短的分类和 JSON 调用会返回空内容。
- **范围**：相对 `20261001-trusted-adult-v1`：`services/common/llm_thinking.py` 按提供商发关闭思考的开关；意图/收尾/危机分类器与 control-api 的文本调用都跟随 `LLM_PROVIDER`（官方 DeepSeek 下统一用 `DEEPSEEK_FAST_MODEL`，没有 DeepSeek key 时退回规则，不会把 DeepSeek 模型名发给 DashScope）；家长回顾照搬孩子原话时追加一次点名改写（合成测试 8/18 → 17/18 天有回顾）；人格抽取只丢掉缺反例的决策/价值条目；三个分类器超时 0.8 → 1.2 s；生产 env 模板和 `prepare_production_upgrade_env.py` 默认 deepseek。实时搜索仍用 Qwen（DeepSeek 不能联网）。
- **过程**：tag `20261001-deepseek-flash-v1` → `70136e94`；腾讯镜像构建、种子上传；`verify-load` 21:59、`freeze` 22:01 PASS（快照含切换前的 qwen env 和数据库 dump）；#167 合并（同树），推 tag；备份两份 env（`.pre-20261001-deepseek-flash-v1`），写入 DeepSeek key（经 stdin，不打印）、`LLM_PROVIDER=deepseek`、`DEEPSEEK_FAST_MODEL=deepseek-flash`、control-api `DEEPSEEK_SUMMARY_MODEL=deepseek-flash`、三个分类器超时 1.2；`env` 第一次豆包对齐冒烟偶发失败、重跑 22:17:50 PASS（DeepSeek 流式冒烟通过）；`schema` 22:18:54；确认没有进行中的会话后 `cutover` 22:20:39–22:21:07；`finish` 22:23:06 全部 PASS。
- **线上核对（只读）**：三角色 `20261001-deepseek-flash-v1` / `70136e94`，重启 0，全部 healthy；readiness `ready`、`llm.provider=deepseek`，外部 200；bridge 与 control-api 切换后 0 条报错。cutover 时一个设备会话断开约 4 s 后重连（预期）。
- **回滚**：`release-ops.sh rollback` 恢复镜像到 `20261001-trusted-adult-v1`，并用 `freeze` 快照还原 qwen env；切前镜像 `memoria-*:rollback-20261001-deepseek-flash-v1-pre`。无 schema 变更。
- **真机复核（第六轮 22:30–22:39）**：16 句 13 句有回复，DeepSeek 首字在提交后 0.5–0.6 s；讲故事时说「别说了」两次都没停（TODOLIST N-10）。服务器上的延迟测试被权限拦下没做，1.2 s 超时依据本机实测，本轮 0 次超时。详见 `docs/acceptance/run-20261001-longsoak/findings.md`。

## 2026-10-01 整栈发布 20261001-trusted-adult-v1（PR #166；孩子被欺负时「告诉爸爸妈妈或老师」必说）

- **背景**：第五轮里被欺负时的「告诉爸爸妈妈或老师」偶尔漏掉（实验室 2/8）。
- **范围**：相对 `20261001-stop-cancel-v1` 只动 `prompt_composition.py` 的孩子块一句（加测试）与 release-ops 的 PREV。
- **过程**：tag `20261001-trusted-adult-v1` → `ed1119a0`；#166 17:15 合并（同树），推 tag；`env` 17:17:04、`schema` 17:17:06、`cutover` 17:17:59、`finish` 全部 PASS；readiness `ready 20261001-trusted-adult-v1`、外部 200。
- **回滚**：`release-ops.sh rollback` 恢复到 `20261001-stop-cancel-v1`。无 schema 变更。

## 2026-10-01 整栈发布 20261001-stop-cancel-v1（PR #164；语音停止改发 CANCEL，加提示词微调）

- **背景**：上一版（`stop-terminal-v1`）在语音停止后给 flush 安装的替换代发 `generation.completed`，而 edge 已把该围栏取消：`AdvanceGeneration` 对已取消围栏的任何非取消事件报 `cancelled generation cannot be reactivated` 并让整条 Voice Core 流失败，设备每次停止后 `recovering` 约 7 s（第四轮串口 + edge 日志）。
- **范围**：相对 `20261001-stop-terminal-v1` 只动 `media_session_output_dispatch.py`（`_end_replacement_generation` 改发 `GENERATION_ACTION_CANCEL`；edge 对同一围栏的取消是幂等的 `ApplyCancelledGeneration`，转成设备 `generation.cancelled`，固件 `HandleGenerationTerminal(cancelled)` flush 并上报 tts stop → 聆听）、`prompt_composition.py`（孩子块：被要求保密时说「好，我听着呢」不答应保密；长辈块：简单问题直接答、不把旧话题硬接）、`companion_turn_policy.py`（设备会话回退指令加「不要把不相关的旧话题硬接进回答」）、`release_ops.sh`。无 schema、无 env、无 control-api 行为变化。
- **过程**：PR #164 16:2x 开；tag `20261001-stop-cancel-v1` → `12a301b2`；腾讯镜像构建；`verify-load`/`freeze` 16:34 PASS；CI 约 18 分钟；16:46 合并（同树 `b4cc875e…`），推 tag；上线前确认没有进行中的对话；`env` 16:47:26、`schema` 16:47:43、`cutover` 16:47:45–16:48:14、`finish` 16:49:28 全部 PASS。
- **线上核对（只读）**：三角色 `20261001-stop-cancel-v1` / `12a301b2`，重启 0，全部 healthy，纯链；`/opt/memoria/current` 指向新目录；readiness `ready 20261001-stop-cancel-v1`、外部 200；镜像内确认 `_end_replacement_generation` 用 CANCEL 且不含 COMPLETE、孩子块 / 长辈块带新规则。
- **回滚**：`release-ops.sh rollback` 恢复到 `20261001-stop-terminal-v1`；切前镜像 `memoria-*:rollback-20261001-stop-cancel-v1-pre`。无 schema 变更。
- **真机复核（第五轮 16:50–16:57）**：语音停止延迟 0.56 s，设备立刻回到聆听，edge 日志里 `cannot be reactivated` 为 0、会话没有被拆；孩子的自我介绍是朋友口吻，被要求保密时说「好，我听着呢」；被欺负时的「告诉爸爸妈妈或老师」第三句偶尔漏掉（实验室 2/8），另有分支加强措辞（`fix/trusted-adult-line`）；假 VAD 抢掉已提交回复的问题仍在（TODOLIST N-8）。详见 `docs/acceptance/run-20261001-longsoak/findings.md`。

## 2026-10-01 整栈发布 20261001-stop-terminal-v1（PR #162；故事不再被截断，语音停止补终结事件——这一补法有缺陷，下一版 stop-cancel-v1 改正）

- **背景**：第三轮真机测试（`device-prompt-v1`）里回答变短后暴露两个老问题：①每次语音停止（「别说了」「停」）之后设备卡在「说话」状态，直到下一次回复结束或 30 s 静默关闭，紧接着那句话被吞掉（5/5）；②「给我讲一个故事吧」只讲 4 句（孩子 4 句上限，且只有「讲个故事」会被当成长内容请求）。
- **范围**：相对 `20261001-device-prompt-v1` 只动 `services/agent/src/voice_core/media_session_output_dispatch.py`、`media_session_playback_stop.py`、`services/common/response_depth.py`（故事请求识别）和 `release_ops.sh`。语音停止后补发 `generation.completed`（`_end_replacement_generation`）；故事请求（动词 + 故事/童话）按长内容处理。
- **过程**：PR #162 15:5x 开；tag `20261001-stop-terminal-v1` → `ebb6926d`，合并提交 `72766740` 与 tag 同树 `36cdf247…`；腾讯 PyPI 镜像构建；`verify-load`/`freeze` 15:53 PASS；CI 约 25 分钟（`python-shards (1)` 这次跑了 13 分钟）；16:12 合并，`env` 16:13:17、`schema` 16:13:25、`cutover` 16:13:27–16:13:54、`finish` 16:15:10 全部 PASS；上线前确认没有进行中的对话。
- **真机复核（第四轮 16:15–16:25）**：故事完整讲完（约 16 s）✓；**但每次语音停止都把会话拆了**：edge `Voice Core stream failed … err="cancelled generation cannot be reactivated"`（`session_generation.go: AdvanceGeneration` 拒绝对已取消围栏的任何非取消事件），设备 `Server closed media session: code=voice_core_unavailable`，`speaking -> recovering` 约 7 s 后回到聆听。进程内测试夹具不模拟 edge 的这条规则，单测没抓到。**这一版的语音停止行为比上一版更重（拆会话），下一版立即改**。
- **回滚**：`release-ops.sh rollback` 恢复到 `20261001-device-prompt-v1`；切前镜像 `memoria-*:rollback-20261001-stop-terminal-v1-pre`。

## 2026-10-01 整栈发布 20261001-device-prompt-v1（PR #161；设备对话里模型终于拿到系统提示词）

- **背景**：N-5 第二轮（`20261001-turn-budget-v1`，10:07–10:43）里机器人 59 句听对了、48 句有回答，但内容是成人讲座加 Markdown，会把【控制响应计划】念给孩子，也记不住上一句。根因（TODOLIST N-9，已用生产模型逐字复现）：设备会话恒走本地安全回退计划；回退 + 属主时 `current_user_only_chat_context` 把系统提示词连同会话历史全部删掉，模型只收到「这一句 + 计划块」。所以 #157 的孩子/长辈说话规则只让 TTS 语气生效，从未到达真机模型。
- **整栈范围**：相对 `20261001-turn-budget-v1`（`dc138e61`）只动 `services/agent` 与两个共享模块：`context_assembler.py`（属主保留系统提示词）、`reply_pipeline.py`（设备绑定属主保留本次会话轮次；回复深度/口播上限按对象）、`agent.py`（回退指令按说话人选；放行检查接受各对象的自我介绍；行数仍 473）、`services/common/companion_turn_policy.py`（`companion_scope_instructions`）、`companion_response_safety.py`（`companion_identity_replies`）、`response_depth.py`（`audience`）；`release_ops.sh` PREV → `20261001-turn-budget-v1`。无 schema、无 env、无 control-api 行为变化。
- **过程**：PR #161 14:58 开；分支头 `73f7bc1c` 打 annotated tag `20261001-device-prompt-v1`；detached worktree 构建三镜像——清华 PyPI 镜像对 `aiohappyeyeballs-2.7.1` 的 wheel 返回 403（索引 200、文件 403，重试仍 403），改用腾讯镜像 `https://mirrors.cloud.tencent.com/pypi/simple` 一次通过（哈希锁定，镜像选择不改变镜像内容）；制品：verifier `aff0c2ec…`、manifest `a608fc36…`、source `5b9a8c2b…`、images `c4bb31a1…`；seeded 上传 112 MB；新 `release-ops.sh`（`1352b0c6…`）上传，旧版备份为 `release-ops.sh.pre-20261001-device-prompt-v1`；`verify-load` 与 `freeze` 在合并前 PASS；服务器上的镜像内确认新代码在位。
- **合并与上线**：CI 全绿后（汇总任务 `python` 的依赖安装就要 13 分钟，整条 CI 约 25 分钟）15:23 `gh pr merge 161 --merge`，合并提交 `e557c044` 的树与 tag 的树相同（`d8374b43…`），随后推 tag；上线前确认没有进行中的对话（`media_active_sessions 0`，15:18–15:21 有人在和机器人说话，等它回到 idle 才切）。`env` 15:24:34、`schema` 15:24:51、`cutover` 15:24:56–15:25:24（speaker-model → control-api → bridge）、`finish` 15:26:40，每步 PASS；切换时设备空闲的 WSS 被边缘关闭（`Cancelling all calls`），设备随后自行重连，没有对话被打断。media-edge 未动。
- **线上核对（只读）**：三角色 `20261001-device-prompt-v1` / `73f7bc1c`，重启 0，全部 healthy，纯链；`/opt/memoria/current` 指向新目录；readiness `ready 20261001-device-prompt-v1`、外部 200、`heartbeat failed` 0；运行中的 bridge 容器内确认 `current_user_only_chat_context(keep_instructions=)` 与 `stream_reply` 的 `device_bound_owner` 在位；`/tmp` 4 KB/64 MiB。
- **回滚**：`release-ops.sh rollback`（同一 TAG/COMMIT 环境变量）恢复 env 快照与 `current`，三个服务从 `20261001-turn-budget-v1` 重建；切前镜像另存为 `memoria-*:rollback-20261001-device-prompt-v1-pre`。无 schema 变更，回滚不涉及数据。
- **真机复核（第三轮，15:30–15:47，音量 65）**：42 句里 38 句有回答；回复 p50 19 字、最长 50 字、最多 3 句，Markdown 0 条、念出内部计划 0 条；情绪类先接感受，被欺负 / 陌生人落到「马上告诉爸爸妈妈或老师」；说完到开口 p50 4.4 s 没有改善；**新发现**：语音停止后设备卡在「说话」状态、「讲一个故事」被截断（均在 PR #162），详见 `docs/acceptance/run-20261001-longsoak/findings.md`。
- **未验证 / 请注意**：①孩子的自我介绍没有在真机上听到（第一步没唤醒成功），用实验室回放核对；长辈侧只有实验室回放；②响应规划器仍不可用（围栏纪元与策略版本两处不兼容，见 TODOLIST N-9），所以跨会话记忆仍没有进入回复；③播放期语音打断与「说完到开口」延迟（p50 4.3 s）未改。

## 2026-10-01 整栈发布 20261001-turn-budget-v1（PR #160；回复后的第一句不再被丢）

- **背景**：N-5 的第一次安静房间对话测试里，机器人把 14 句话都听对了，却只回答了 2 句（只有天气问句，走查询路径），其余 12 句没有回答、会话被服务端关闭（`turn_prepare_timeout`）。查实两个比今天更老的服务端缺陷：①响应计划客户端对 `session_epoch ≥ 1` 的围栏恒报 `fence_mismatch`（规划契约只回显 turn/generation/tool_epoch），`prepare_turn` 一遇它就 `StopResponse` 丢掉整句话——只有上下文版本在取计划期间恰好变化、协调器先把结果当过期丢弃时才走回退计划并作答；②单轮绝对预算 2.5 s 小于「重开窗口 2.0 s + 跟进宽限 1.2 s + 准备 1–1.5 s」，会话在回答准备好之前被要求待命。两个云端分类器还串行多占约 0.6 s。细节与证据见 TODOLIST N-8 和 `docs/acceptance/run-20261001-longsoak/findings.md`。
- **整栈范围**：相对 `20261001-output-seq-v1`（`2fd8df3b`）只动 `services/agent`：`reply_pipeline.py`（`fence_mismatch` 改用本地安全计划作答，真正的过期仍由运行时围栏检查丢弃；`resolve_live_lookup_needed` 与 `resolve_conversation_close_needed` 用 `asyncio.gather` 并行）、`voice_core/media_session.py`（`turn_endpoint_absolute_timeout_s` 2.5 → 6.0）。control-api、speaker-model 无代码变化（镜像换 tag）；无 schema、env、compose 变化。`release_ops.sh`：PREV → `20261001-output-seq-v1`。4 条回归测试（两条去掉修复即失败）、全部 `services/agent/tests/unit`、ruff、mypy --strict。
- **过程**：PR #160 09:55 开；分支头打 annotated tag `20261001-turn-budget-v1`；detached worktree 构建三镜像（约 2.5 分钟）；制品：verifier `aff0c2ec…`、manifest `e2b334c5…`、source `f9361884…`、images `dab97ac6…`；seeded 上传（基座 `20261001-output-seq-v1`）双端校验 PASS；服务器 `release-ops.sh` 换新版（旧版备份 `release-ops.sh.pre-20261001-turn-budget-v1`，sha256 `27b9a228…` 两端一致）。CI 期间 09:59 `verify-load`、`freeze`（pg_dump 3.3 MB），CI 全过后 `gh pr merge --merge`——此前 #158（固件）已合并，所以合并树与 tag 树只差那 4 个固件文件，服务代码相同——推 tag，随后 10:04 `env`（provider smoke 全 PASS，无 env 键变化）、`schema`、`cutover`（speaker-model 10:05:02、control-api 10:05:09、bridge 10:05:22，全程约 30 秒）、10:06 `finish`（readiness 与对外 200）。收据在 `/opt/memoria/releases/20261001-turn-budget-v1/.cutover/` 与 `/root/memoria-release/*-20261001-turn-budget-v1.log`。
- **线上核对（只读）**：三角色 `20261001-turn-budget-v1` / `dc138e61`，重启 0，全部 healthy，纯链；bridge 容器内确认 `turn_endpoint_absolute_timeout_s = 6.0`、`prepare_turn` 含并行分类器与「response plan ignored」。重跑 36 分钟对话测试的头两句：回答延迟（说完到开口）3.1 s 与 4.9 s，均得到完整回答。
- **回滚**：`release-ops.sh rollback`（同一 TAG/COMMIT 环境变量）恢复 env 快照与 `current`，三个服务从 `20261001-output-seq-v1` 重建；切前镜像另存为 `memoria-*:rollback-20261001-turn-budget-v1-pre`。无 schema 变更，回滚演练未做。
- **未验证**：整套 44 步对话测试的结果（正在跑，见 findings.md）；响应计划本身是否应当真正生效（补 `session_epoch` 比对会让 persona/grounding 指令首次进入生产，需单独评估）。

## 2026-10-01 整栈发布 20261001-output-seq-v1（PR #159；设备卡在「说话」状态的修复）

- **背景**：09:00 跑唤醒词试验时撞出：噪声让 ASR 听成一句「滴滴不到。」被判成实时查询；查询的开场白（FAST_ACK，13 帧，序号 0–12）先于提交完成播出，提交随后把同一围栏（turn 2 / gen 2）的输出计数清零，查询没有结果后的本地回退回答从序号 0 重新开始，被下行拒绝为 `sequence_gap`（期望 14），派发报告 `emitted_audio=False`，所以既无回答也无终结围栏，设备在「说话」状态停了 2 分 43 秒直到板子被复位。细节见 TODOLIST N-8。
- **整栈范围**：相对 `20261001-speaking-style-v2`（`b3b5f075`）只动 `services/agent/src/voice_core`：`media_session_state.py`（`OutputState.begin_turn_output()`：围栏已有帧就保留输出计数；`audio_sent_for()`）、`media_session_commit.py`（提交改走该方法）、`media_session_output_stream.py`（`_abort_unheard_stream` 把同围栏更早的输出已让设备出声算作已出声，从而关闭生成）。control-api、speaker-model 无代码变化（镜像换 tag）；无 schema、env、compose 变化。`release_ops.sh`：PREV → `20261001-speaking-style-v2`。7 条回归测试，安全网那条去掉一行修复即失败。
- **过程**：PR #159 09:23 开；分支头打 annotated tag `20261001-output-seq-v1`；detached worktree 构建三镜像（约 3 分钟）；制品在构建机 scratchpad：verifier `aff0c2ec…`、manifest `8a4525f6…`、source `1544cb44…`、images `84b0de4a…`；seeded 上传（基座 `20261001-speaking-style-v2`）双端校验 PASS；服务器 `release-ops.sh` 换新版（旧版备份 `release-ops.sh.pre-20261001-output-seq-v1`，sha256 `13e84e6c…` 两端一致）。CI 期间 09:30 `verify-load`、`freeze`（pg_dump 3.2 MB），CI 全过后 `gh pr merge --merge`，核对合并树与 tag 树相同、推 tag，随后 09:35 `env`（provider smoke 全 PASS，无 env 键变化）、`schema`、`cutover`（speaker-model 09:35:12、control-api 09:35:18、bridge 09:35:32，全程约 30 秒）、09:36 `finish`（readiness 与对外 200）。收据在 `/opt/memoria/releases/20261001-output-seq-v1/.cutover/` 与 `/root/memoria-release/*-20261001-output-seq-v1.log`。
- **线上核对（只读）**：三角色 `20261001-output-seq-v1` / `2fd8df3b`，重启 0，全部 healthy，纯链；bridge 容器内确认 `OutputState.begin_turn_output` 与 `audio_sent_for` 在位；`/tmp` 4 KB/64 MiB。
- **回滚**：`release-ops.sh rollback`（同一 TAG/COMMIT 环境变量）恢复 env 快照与 `current`，三个服务从 `20261001-speaking-style-v2` 重建；切前镜像另存为 `memoria-*:rollback-20261001-output-seq-v1-pre`。无 schema 变更，回滚演练未做。
- **固件 build 19**（同日，不属于整栈）：修熄屏计时（`now | 1` 回绕，见 N-8）。09:32 USB 只写 `ota_0`（0x20000）与空 `otadata`（0xd000），哈希校验通过，保留身份与 NVS；开机串口 `MEMORIA_FIRMWARE_BUILD=19; slot=ota_0`，`State: activating -> idle` 后正好 10.06 秒出现 `screen off (idle)`（之前是 80 毫秒）。PR #158 待合并；未签名发布 OTA。
- **未验证**：真实 30 分钟对话（正在跑，结果见 `docs/acceptance/run-20261001-longsoak/findings.md`）；服务端修复在真实「开场白后查询无结果」场景下的表现（单测覆盖，现网没有可复现的触发条件）。

## 2026-10-01 整栈发布 20261001-speaking-style-v2（PR #157；说话风格第二轮）

- **背景**：第一轮（`20261001-audience-recap-v1`）之后，用真实流水线（生产提示词 + 生产聊天模型 + 真实语速与语气规划 + 生产豆包音色「阿序」）给孩子、长辈各合成 8 句回复与音频，发现：长辈说「孙女不愿意跟我说话」被回成「您一定很想念孙女」、「想老伴」仍回「他是个什么样的人」、儿子不打电话被回「是不是工作忙」、要听故事只讲两句；孩子问「天为什么是蓝的」得到错误的「过滤器」类比、故事只有三四句、伤心时被问「为什么」、十几岁学生被「抱抱你」；老年语速 0.94 实测 3.7–3.9 字/秒，孩子 3.5–4.1，几乎听不出；三种人群共用阿序的「落点利落」音色提示；「难过」以外的情绪词不触发温和语气。细节与实验数据见 TODOLIST N-6。
- **整栈范围**：相对 `20261001-audience-recap-v1`（`c6ec91ed`）只动 `services/agent`：`prompt_composition.py`（长辈块：只接他说出口的那种心情并带示范句、不说成想念、不猜家人为何不联系、不用「他/她」、五六句的怀旧小故事；孩子块：四条准确的「为什么」一句话事实与「不确定就一起去查」、至少六句的故事、问「发生什么了吗」不问「为什么」、十几岁不说「抱抱你」「乖」）、`orchestration/prosody.py`（长辈语速系数 0.94 → 0.90；按对象在豆包音色指令后追加一句提示，难过时用柔和版，用户要求某种风格时不加；孤单/睡不着/压力/想念/想老伴/不理我/不跟我玩等词也选温和语气）。control-api、speaker-model 无代码变化（镜像换 tag）；无 schema、env、compose 变化。`release_ops.sh`：PREV → `20261001-audience-recap-v1`，控制面组件链支持去掉（三角色都在纯链上），守卫测试同步。
- **过程**：PR #157 05:22 开；分支头打 annotated tag `20261001-speaking-style-v2`；detached worktree 构建三个 linux/amd64 镜像（2.5 分钟，清华源）；制品放在构建机 scratchpad：verifier `aff0c2ec…`、manifest `0a89d188…`、source `7ca03b68…`、images `ce0f19f9…`；seeded 上传（基座 `20261001-audience-recap-v1`）双端校验 PASS；服务器 `release-ops.sh` 换成新版（旧版备份为 `release-ops.sh.pre-20261001-speaking-style-v2`，sha256 `b37ac722…` 两端一致）。CI 期间 05:26 `verify-load`、05:27 `freeze`（pg_dump 3.2 MB），CI 全过（10 项成功、5 项按路径跳过）后 `gh pr merge --merge`，核对合并树与 tag 树相同、推 tag，随后 05:31 `env`（provider smoke FunASR、Qwen 实时搜索、Qwen、Doubao 全 PASS，无 env 键变化）、`schema`、`cutover`（speaker-model 05:31:13、control-api 05:31:20、bridge 05:31:34，全程约 30 秒）、05:33 `finish`（readiness 与对外 200）。切流时一条安静时段内的设备会话被 bridge 重启断开，设备自动重连后被 edge 以 `minor_quiet_hours` 关闭。收据在 `/opt/memoria/releases/20261001-speaking-style-v2/.cutover/` 与 `/root/memoria-release/*-20261001-speaking-style-v2.log`。
- **线上核对（只读）**：三角色 `20261001-speaking-style-v2` / `b3b5f075`，重启 0，全部 healthy，compose 链是新版本的纯链；bridge 容器内确认新提示词在位（长辈块含「只接他真正说出口的那一种心情」、孩子块含天蓝事实）、`SENIOR_RATE_FACTOR = 0.9`、对长辈说「我有点想我老伴了」得到 `supportive`、速率 0.882 且音色指令以「对长辈说话：…」结尾；`/tmp` 4 KB/64 MiB，`heartbeat failed` 为 0。
- **回滚**：`release-ops.sh rollback`（同一 TAG/COMMIT 环境变量）恢复 env 快照与 `current`，三个服务从 `20261001-audience-recap-v1` 重建；切前镜像另存为 `memoria-*:rollback-20261001-speaking-style-v2-pre`。无 schema 变更，回滚演练未做。
- **未验证**：新声音的听感（59 段样本在 `outputs/listening-20261001/`，被 gitignore，需要你来听）；孩子绑定设备上的真实 30 分钟对话（07:00 夜间休息结束后执行，见 TODOLIST N-5）。

## 2026-10-01 整栈发布 20261001-audience-recap-v1（PR #156；含控制面组件 20261001-wake-mode-v1）

- **背景**：用户 2026-10-01 新增需求（唤醒方式设置、空闲熄屏、发布刷机、回顾为空、电脑长稳对话、说话风格评审，见 TODOLIST「2026-10-01 新需求」）。
- **先发的控制面组件 `20261001-wake-mode-v1`**（04:2x CST）：显示档案轮询（设备签名 `GET /v1/devices/{id}/display-profile`）增加加性字段 `wake_mode`（不进 `display_version`；读设置失败时省略该字段，设备保持现状）。从线上提交 `412f31e9` 拉热修分支 `hotfix/control-wake-mode`（只带 control_api 三个文件），annotated tag `20261001-wake-mode-v1`（→ `92664ec5`）已推送；`deploy_control_component.sh` dry-run 后 `--cutover --base-image memoria-control-api:20261001-device-archive-v1`，约 5 分钟，`/etc/memoria-control-api.env` 未变（信任开关仍开）；回滚镜像 `rollback-20261001-wake-mode-v1-pre-control`。
- **agent 组件快车道被拒**：`20261001-audience-style-v1`（agent 三项改动）本地门禁全过、服务器上瘦镜像也构建成功，切流前被 `deploy_agent_component.sh` 的独立性检查拒绝（基座镜像恰是线上 bridge 正在跑的镜像，`runtime base image is missing, has invalid provenance, or is not independent`）；服务器无任何变化，我删除了孤儿镜像与上传目录。因此 agent 改动改走整栈。
- **整栈范围**：相对 `20261001-device-archive-v1`（`412f31e9`）：`services/control_api`（显示档案 `wake_mode`、`guardian_recap.py` 与 `routes/guardian.py` 的 `/days`、`/days/{day}/recap`，`weekly_summary` 的访问判定抽成 `_guardian_evidence_account`，行为不变）；`services/agent`（`SERVICE_MODE_BLOCKS` 两段加「说话方式」、老年模式 TTS 语速 ×0.94、PCM tap 按最旧优先清理）；固件 build 17/18 源码、小程序、文档不进镜像。schema、env、compose 无变化。`release_ops.sh`：PREV → `20261001-device-archive-v1`，`LIVE_CONTROL_RELEASE` → `20261001-wake-mode-v1`。
- **过程**：PR 分支头打 annotated tag `20261001-audience-recap-v1`；在干净的 detached worktree 按 compose 定义构建三个 linux/amd64 镜像（依赖未变，清华源，控制面 20 s、bridge 45 s、speaker-model 63 s，revision/version/role 标签逐个核对）；制品放在构建机 scratchpad：verifier `aff0c2ec…`、manifest `5d573aa3…`、source `f75e3e6e…`、images `44c1c05a…`；seeded 上传（基座 `20261001-device-archive-v1`，实传 100 MB）双端校验 PASS；服务器 `release-ops.sh` 换成新版（旧版备份为 `release-ops.sh.pre-20261001-audience-recap-v1`，sha256 `a01df8ff…` 两端一致）。04:46 `verify-load`、`freeze`（pg_dump 3.1 MB；控制面组件链被认出）在 PR 合并前完成；CI 13 项全过后自动合并（merge commit），核对合并树与 tag 树相同（`8a1edd04…`）、推 tag，随后 04:50 `env`（provider smoke FunASR、Qwen 实时搜索、Qwen、Doubao 全 PASS，无 env 键变化）、`schema`、`cutover`（speaker-model 04:50:32、control-api 04:50:40、bridge 04:50:53）、04:52 `finish`（readiness 与对外 200，media-edge 约 2 秒内重拨上新 bridge）。收据在 `/opt/memoria/releases/20261001-audience-recap-v1/.cutover/` 与 `/root/memoria-release/*-20261001-audience-recap-v1.log`。
- **线上核对（只读）**：三角色 `20261001-audience-recap-v1` / `c6ec91ed`，重启 0，全部 healthy，compose 链是新版本的纯链；bridge `/tmp` 4 KB/64 MiB（此前 64 MiB 满、心跳本地状态写入失败 14 小时 1104 次），`heartbeat failed` 为 0；信任开关仍为 true；未带凭证访问 `/v1/guardian/minors/x/days` 与 `.../recap` 返回 401。
- **回滚**：`release-ops.sh rollback`（同一 TAG/COMMIT 环境变量）恢复 env 快照与 `current`，control-api 按组件链回到 `20261001-wake-mode-v1`，speaker-model 与 bridge 从 `20261001-device-archive-v1` 重建；切前镜像另存为 `memoria-*:rollback-20261001-audience-recap-v1-pre`。无 schema 变更，回滚演练未做。
- **小程序体验版**：`0.2.20261001.1`（设备页唤醒方式两个开关）与 `0.2.20261001.2`（回顾页监护人视图），均用 DevTools CLI 上传，**需你在公众平台设为体验版**。
- **固件 build 18**：USB 只写 `ota_0`（0x20000）与空 `otadata`（0xd000），写后哈希校验通过，保留身份与 NVS；开机串口 `MEMORIA_FIRMWARE_BUILD=18; slot=ota_0`、`MemoriaWakeMode` 就绪、首个显示档案轮询 200、空闲 10 秒后 `MemoriaMascot: screen off (idle)`。**未验证**：点屏唤醒、黑屏肉眼确认、三种唤醒方式切换，需要你在机器旁（我无法代点屏幕、也看不到屏幕）。未签名发布 OTA。
- **未验证（服务端）**：新提示词与老年语速的真机听感；30 分钟对话（07:00 夜间休息结束后执行，见 TODOLIST N-5）。

## 2026-10-01 打开设备信任开关与服务器磁盘清理（用户授权）

- **打开开关**：用户 2026-10-01 授权。`/etc/memoria-control-api.env` 加 `MEMORIA_BOUND_DEVICE_TRUST_ENABLED=true`（改前备份 `/etc/memoria-control-api.env.pre-bound-trust-20261001`），重建 control-api 后 healthy；进程内验证：绑定的设备 `untrusted + device_bound` → `trusted`（理由 `device_bound_no_attestation`），`verified` 才有的能力（如声音克隆）仍被拒。关闭 = 删该行并重建 control-api。
- **只读取证（03:40）**：开关打开后的 00:33–01:32 有 8 条 `owner` 的 `speech.utterance_finalized` 与 2 条 `assistant.playout_stopped` 入库，`history_eligible`/`owner_projection_eligible` 为真、`retained`，`account_id` 是家长账号 `wx_71bd…`，`subject_id` 是孩子 `83370358…`（账号是该孩子和另外三个历史孩子的 `guardian_of`）。07、08 月的 17 条 `uncertain`、无主体，从不进回顾。不显示对话内容。
- **服务器磁盘清理**（用户授权；清单见 `docs/runbooks/operations-space-governance.md` 新增一节）：分阶段执行，镜像清单先只读生成并复核 sha256、再执行 77 个 tag 的逐项 `docker rmi`；随后 `incoming` 13 个旧目录、旧 `releases` 源码树（保留 `.cutover`）、`component-releases/*/build`、`/home/ubuntu` 9 月 16 日构建残留。根分区已用 90 → 37 GB（80% → 33%）。清理后 10 个容器全部 healthy，内外部 readiness 200，回滚用的镜像与 compose 链文件逐项核对存在；未动数据卷、journald、WMS/saas、`/var/backups/memoria`。收据：服务器 `/root/memoria-release/cleanup-20261001.log` 与 `cleanup-20261001-pre/`（清理前的 `docker ps`/images/volumes 快照）。
- **并发提醒**：同一时段另一会话在主检出里改了固件（build 17，`memoria_stop_keyword.h`、`0031` 补丁等未提交），并把开发板刷成了 build 17；本轮工作在独立 worktree 里继续，并把那份改动作为单独提交带上。

## 2026-10-01 整栈发布 20261001-device-archive-v1（#148、#151–#153，含 #150）

- **范围**：tag → `412f31e9`（#154 分支头，与合并提交 `5f0b9f2a` 同树）。相对线上栈 `20260930-local-stop-v2`（`88a3c80`）：#148（去掉 `livekit.agents` 依赖，bridge 镜像 892 MB 到 683 MB）；#150（向量路径词面项，此前已作为 control-api 组件在线上）；#151、#152、#153（设备对话归档闸门、孩子记忆口径、设备信任分档）。固件与 media-edge 的改动不在栈内，media-edge 仍是组件发布 `20260930-late-receipt-v1`。schema 只有两个函数：`action_device_lock_trust`（多返回 `device_bound`）与 `device_fleet_assert_action_authority`（收据只认 `verified`）。无 compose 变更（只是去掉已无用的 livekit 遥测环境变量）、无 env 变更：新增的 `MEMORIA_BOUND_DEVICE_TRUST_ENABLED` 只在模板里，默认 false，线上 env 没有设。
- **发布前只读核对**：control-api 当时跑在 `20260930-vector-keyword-v1` 组件链上（PREV compose、pre-cutover override、component override），而 `release_ops.sh` 只认纯 PREV 链，`freeze` 会拒绝；发布 PR #154 按 `4eb1df69` 的做法补回了 control-api 组件链支持（freeze 只认该链，rollback 先按该链重建 control-api），PREV 改为 `20260930-local-stop-v2`。服务器根分区 76%（剩 28 GB）；切换前 10 分钟内没有设备会话。
- **发布过程**：本机在干净 detached worktree（tag `20261001-device-archive-v1`）按 compose 定义构建三个 linux/amd64 镜像（依赖有变化；清华源；有缓存，约 2.5 分钟；revision/version 标签核对无误；agent 镜像构建内的发布门 `verify_agent_release_artifact` 通过）；制品放在构建机 scratchpad（verifier 必须在源码树外）。摘要：verifier `aff0c2ec…`，manifest `4463e9f4…`，source `fa59f673…`，images `b3c8d73f…`。seeded 上传（基座 `20260930-local-stop-v2`，实传 102 MB）双端校验 PASS。服务器 `release-ops.sh` 换成新版（旧版备份为 `release-ops.sh.pre-20261001-device-archive-v1`，哈希与仓库一致）后依次执行：`verify-load`（00:07:26，冒烟自带临时 PG）、`freeze`（00:07:38，pg_dump 2.9 MB，control-api 链核对通过）在 PR 合并前完成；PR 合并、推 tag 之后：`env`（00:11:59，provider smoke FunASR、Qwen 实时搜索、Qwen、Doubao 全 PASS，无 env 键变化）、`schema`（00:12:05）、`cutover`（00:12:29–00:12:57，speaker-model、control-api、bridge 依次）、`finish`（00:14:13）。每步单独确认 PASS，收据在 `/opt/memoria/releases/20261001-device-archive-v1/.cutover/` 与 `/root/memoria-release/*-20261001-device-archive-v1.log`。
- **线上核对（只读）**：三角色都在 `20261001-device-archive-v1` / `412f31e9`，重启 0，全部 healthy，compose 链是新版本的纯链（control-api 的组件链已并回）；readiness ready、内外部 200，media-edge 约 1 秒内重拨上新 bridge；发布后日志没有错误行。`action_device_lock_trust` 对线上设备返回 `untrusted / device_attestation_unavailable / device_bound=true`（等级不变、多一个事实字段，旧代码此前就在无碍地运行）；control-api 内 `bound_device_trust_enabled=False`，`device_trust_allows` 的分档如设计（`verified` 放行声音克隆，`trusted` 只放行记忆类）；bridge 新进程导入 `scripts.run_media_bridge` 后没有任何 `livekit*` 模块。
- **回滚**：`release-ops.sh rollback`（同一 TAG/COMMIT 环境变量）恢复 env 快照与 `current` 软链，control-api 按组件链回到 `20260930-vector-keyword-v1`，speaker-model 与 bridge 从 `20260930-local-stop-v2` 重建；切前镜像另存为 `memoria-*:rollback-20261001-device-archive-v1-pre`。schema 变更只加不改，回滚不需要还原 SQL。回滚演练未做。
- **未验证**：发布当时没有真机对话（00:1x 落在夜间时段，设备只会播晚安），开关发布时关闭、行为与发布前一致（开关其后已打开，见下一节）。bridge 去 livekit 后的语音节奏（慢速 TTS）与停止词延迟需在使用时段内真机验收。
- **发布后仍待**：①~~使用时段内做一次设备对话验收~~——开关已打开，改为 TODOLIST N-5；②~~打开开关~~已做（下一节）；③下一次整栈发布删掉 `release_ops.sh` 里的 control-api 组件链支持，PREV 改为本版本；④~~服务器磁盘清理~~已做（下一节）。

## 2026-09-30 control-api 组件发布 20260930-vector-keyword-v1（#150，向量路径词面项）

- **范围**：向量混合检索的词面分改用「首尾字都是内容字」的 n-gram，并按命中查询词占比的平方计分（`memory_domain.edge_content_query_terms`、`PostgresMemoryCatalog._search`）；指标与取舍见 TODOLIST P1-06「大语料干扰评测」。
- **过程**：main 已越过线上提交，所以从线上提交 `88a3c805` 拉热修分支 `hotfix/control-vector-keyword-bonus`，只带 `services/archive` 的 5 个文件，annotated tag `20260930-vector-keyword-v1`（→ `0f1ebcfd`）已推送。`deploy_control_component.sh --dry-run` 后 `--cutover --base-image memoria-control-api:20260930-local-stop-v2`，约 5 分钟；`MEMORIA_RELEASE_TAG` 刻意保持栈 tag。
- **现状（2026-09-30 晚只读核对）**：容器 healthy、重启 0，回滚镜像 `rollback-20260930-vector-keyword-v1-pre-control`。**尚未**用线上带鉴权读口或设备追问验收。

## 2026-09-30 设备信任分档（用户决定选项 2；#153，已随 `20261001-device-archive-v1` 上线，开关 2026-10-01 00:30 已打开）

- **范围**：`action_device_lock_trust`（`services/session_runtime/postgres_schema.sql`）返回值新增 `device_bound`，`device_trust` 与 `reason_code` 不变；新模块 `services/session_runtime/device_trust.py` 校验答复并在 `MEMORIA_BOUND_DEVICE_TRUST_ENABLED`（默认 false）打开时把 `untrusted` 加 `device_bound` 映射为 `trusted`，`service.py` 因此缩小 12 行、行预算 3060→3048；引擎 `services/policy/engine.py` 删掉 `TRUSTED_DEVICE_TRUSTS`，改为 `device_trust_allows(capability, trust)`：`verified` 放行全部，`trusted` 只放行 `BOUND_DEVICE_CAPABILITIES`（`memory_capture`、`memory_recall_private`、`guardian_summary_view`）；`device_fleet_assert_action_authority` 的收据校验从 `IN ('trusted','verified')` 收紧为只认 `verified`；内存版 `profile_service.py` 两处写死的 `trusted` 改 `verified`；配置项进 `config_fields/media.py`、env 模板与 `main.py` 接线（`main.py` 内联一个单次使用的路径小函数，预算 1245→1242）。
- **为什么不新增枚举值**：`DeviceTrust` 是 ADR-0033 冻结的契约，同时生成到小程序 JS，加值还要对线上 `policy_receipts_v2` 的 CHECK 约束做 ALTER；契约里的 `trusted`（「已建立受信设备链路」）语义合适，生产 848 条 receipt 全是 `untrusted`，重新定义没有历史数据影响。引擎原先把 `trusted` 与 `verified` 一视同仁，所以 SQL 绝不直接返回 `trusted`（旧引擎会因此放开全部敏感能力），而是加事实字段、由新代码在开关打开后才映射。
- **测试**：策略矩阵新增分档用例（成人与孩子的记忆放行、成人与孩子的其余敏感能力拒绝、家长小结、资源类动作、对全部敏感能力的穷举）；纯函数 `snapshot_from_authority` 21 条；真实 PG：函数在已绑定、证书激活或吊销或过期、已 attest 等情形下的 `device_bound`（原有断言保持）；设备编排：设备动作的收据校验对 `trusted` 拒绝、对 `verified` 接受（`test_postgres_sim_authority.py`，去掉 SQL 收紧即失败）；端到端 `services/control_api/tests/test_bound_device_trust_postgres.py`：成人控制会话、孩子设备会话加家长 app 会话（生产的形状）、动作时授权（记忆放行、声音克隆仍 `device_untrusted`），开关关与开各一遍。变异检查：映射永不升级、映射无视开关、引擎对 `trusted` 放行全部，各自被对应用例抓到。测试默认设备信任由 `trusted` 改为 `verified`（代表完整硬件证明的设备）。
- **发布与回滚**：整栈发布（component 通道拒收 `session_runtime` 的 SQL）；发布时开关关闭、行为不变；`schema` 步骤只加字段，可先于 cutover，回滚代码不需要回退 schema；打开与关闭开关都是改 control-api env 后重建容器，步骤与验收清单见 `docs/runbooks/release-rollback.md`「设备信任开关」。
- **未验证**：没有设备验证（线上只读核对见下一节 `2026-10-01` 整栈发布）。打开开关后新链路（归档、编译、向量写入、召回、孩子人格、家长小结入口）都是自 08-08 起第一次在现网跑。

## 2026-09-30 设备对话为什么没有入库（只读核对；修复为 #151、#152、#153，已随 `20261001-device-archive-v1` 上线，开关 2026-10-01 00:30 打开后设备对话重新入库）

- **现象**：生产 `archive_evidence_events` 里 `speech.utterance_finalized`（233 条 `funasr.authoritative_final` + 4 条文本输入）与 `assistant.playout_stopped`（213 条）的最后一条都是 2026-08-08；此后只有声纹分类事件和 09-28 一条监护同意事件。近 72 小时 bridge 与 control-api 日志里 `session-events` 为零。
- **三层原因**：① Agent 归档门要求已签名 profile 列出 `memory_capture`，而 Control 从不签发它（`PROFILE_ISSUE_DEFERRED_CAPABILITIES`），线上验证器又是 `DefaultDenyReceiptVerifier`；#151 让成人凭 profile 自己的 `memory_recall_private` 归档。② 对孩子，Policy 的 `memory_capture` 旧口径带 `PERSIST_AGGREGATE_ONLY`，与 09-25「家长勾选长期记忆后孩子被记住」的决定不一致；本分支把它改成与成人同样的 `RETENTION_TTL` + `NO_MODEL_TRAINING`，并让闸门也认孩子的 `memory_recall_private`。③ **设备信任**：`action_device_lock_trust` 只有「证书有效且 attestation 当前有效」才返回 `verified`，生产 `device_fleet_attestations` 为 0 行，返回 `untrusted / device_attestation_unavailable`，Policy 因此对所有主体拒绝 `memory_recall_private` 与 `memory_capture`。库里最近签发的设备与 app 会话 profile，能力都只有 `["chat"]` 或 `["chat","english_practice"]`。这一层不能靠 Agent 侧改动绕过；用户 2026-09-30 决定按「仅绑定链路」分档解决，见上一节。
- **已核对的授权事实**：孩子 `83370358…` 的 `guardian_person_consents.memory_retention` 有效（2026-09-28 02:39Z 授予，未撤销）；binding v3 的 `memory_capture`（purpose `memory_capture`）、`memory_recall_private`（purpose `memory_recall`）、`guardian_summary_view` 三条 consent 均 active；`device_fleet_devices` 只有一行 `dev_atk_a4cb8fd6095c`（`bound`，reason `binding_backfill`）。
- **为什么不给归档逐轮申请 `memory_capture` 收据**：Control 的动作围栏每次授权必须恰好前进一步（`turn`/`interrupt`/`tool`，`_action_fence_kind`），并发请求只有一个赢家；Agent 的 turn/generation 计数与它不同步，给每个归档事件申请会大面积 409。服务端落库时本来就独立重新授权：`/v1/archive/session-events` 按主体类别与监护同意决定是否保留原文，`memory_project_capture_evidence` 重验当前 Session profile，未成年人的长期记忆再经 catalog 的窄投影过滤（`filter_extraction_for_subject`）。Agent 上报的 `policy_receipt_id` 不是权威。
- **未验证**：以上修复只有本地 PG 与单元测试；没有设备或生产验证。归档证据行没有过期任务，`RETENTION_TTL` 目前只有 MemoryScope 记录会执行。

## 2026-09-30 设备本地停止词（#141、#142，已发布 20260930-local-stop-v2 + media-edge 20260930-local-stop-v1）

- **范围**：固件 build 13（播放期间本地识别「停一下 / 别说了 / 停停 / 停」，patch `0031`；MultiNet 检测门限 0.10，唤醒词仍按 0.20 接受，0.10–0.20 的弱唤醒以 INFO 打印 `Wake word below threshold`）。Agent：设备上报的硬停止关键词不再发 `playback.flush`（设备已在本地清空，固件会把这条 flush 判为协议违规并断线重连约 8 秒，09-30 build 12 「停」0.324 实测）。media-edge：关键词硬停止像 `button.stop` 一样关闭播放窗口，过期的硬停止记为 stale、不断连接。
- **固件**：build 13 只经 USB 刷到开发板（仅写 `ota_0`），未走 OTA 发布。09-29 build 12 刷完后卡在 bootloader 之后、需手按 BOOT 恢复；同一镜像 09-30 重刷正常启动，视为一次性板子/USB 状态，未再复现。
- **发布过程**：agent 组件快车道 `deploy_agent_component.sh` dry-run PASS，但 `--cutover` 在切换前拒绝：线上 bridge 是整栈镜像，不带脚本要求的 `com.memoria.release.kind` 标签（脚本已随后修复：无 kind 且 version 等于栈 tag 的镜像视为整栈，回滚 tag 只复用当前 bridge 镜像本身；尚未在生产实跑），线上未变。改走整栈：本机以 `20260929-session-limits-v1` 为基座增量构建三镜像，seeded 上传双端校验 PASS；摘要：verifier `aff0c2ec…`，manifest `7fc5490f…`，source `3c834576…`，images `6afab151…`。`release_ops.sh`（sha256 `62b4bab0…`）PREV → `20260929-session-limits-v1` / `26e937f`，旧版备份 `release-ops.sh.pre-20260930-local-stop-v2`。media-edge 镜像本机构建（revision `1a02175`），scp 后两端 sha256 `447c3ff9…` 一致再导入；按组件覆盖文件在 `20260929-session-limits-v1` 发布树下切换，新旧渲染配置的 media-edge 段只差构建上下文、构建参数与镜像；其余容器未变，未带凭证的设备入口 401。
- **验证**：agent 发布门禁（ruff、mypy --strict、单测）、固件测试、media_edge `go test` 全过；新回归测试在去掉修复时失败。**尚未**在真机上验证 build 13 + 新 bridge 的停止词不再断线。


- **后续同日（真机测试后）**：
  - 唤醒词接受线 0.20 → 0.10（build 14，#144），旁边电脑放视频时误唤醒了好几次，用户决定 0.10 → 0.12（build 16，#147；此前 0.113、0.117 的两次唤醒会被拒）：台架上「茉莉」十二次里十次只有 0.11–0.20，build 13 全部拒绝，build 10 的代码在同位置也只认出 2/7，所以不是停止词并入词表造成的。得分不到 0.10 的唤醒仍不会被上报，需要更近的距离或更大的声音。build 14 仅 USB 刷入。
  - media-edge `20260930-edge-reject-log-v1`（#145，仅日志）：`WSS handler rejected` 带上被拒帧的 type、control_sequence、fence 的 turn/generation，不含内容。
  - media-edge `20260930-late-receipt-v1`（#146）：设备被任务看门狗卡住约 4 秒，云端语音停止已替换第 2 代，设备恢复后先报该代 `playback.ended` 再补一条 `playback.progress`（control_sequence 102），账本视 `ended` 为终态而拒绝，进而关闭 WSS、约 6 秒重连。现在账本拒绝的回执若属于已被 Voice Core 替换的代际，记日志后丢弃、连接保持；活代际仍严格拒绝。回归测试在旧代码上复现同一条 `handler rejected` 日志。
  - 设备卡顿的证据：三段长回复（build 12 一次、build 14 两次）都在播放约 10–13 秒时触发任务看门狗，回溯落在 `audio_afe → CustomWakeWord::FeedSamples → model_detect`（MultiNet6 编码层），Opus 编码任务堵在同一核上（`Encode queue is full`）；三段里本地停止词共 0 次命中，都是云端语音停止停下的。
  - 决定（用户 09-30）：先关掉播放期间的设备端停止词识别，由云端按语义停播（`interruption_guard.py` 的精确名单含「退下吧」「好的我知道了」「再见」等，其余由语义分类小模型判断）。固件 build 15 用 `kLocalStopKeywordEnabled=false` 回到 build 10 的播放期负载，代码和词表保留；是否用更省算力的方式加回，等有云端停播延迟的数据再定。
  - **尚未验证**：build 15 是否消除看门狗告警与断线；云端语音停止的实际延迟（开口到声音停下）未测。

## 2026-09-29 CI python 任务分片（待合并）

- **问题**：合并流程每次要等约 20 分钟，PR 与 main 各一轮。`python` 任务里 `Pytest` 一步 16.5 分钟，其余步骤合计不到 1 分钟；同一套测试在本机串行只要 8 分钟。
- **不可行的做法**：`pytest -n 4`。非数据库测试没问题，但 Postgres 测试共用一个集群里的角色（改密码、`tuple concurrently updated`、`password authentication failed`，73 个错误）；`main.py` 导入时建 app，多个 worker 同时收集会锁住 SQLite；一个参数化测试遍历 `set`，跨进程顺序不同。
- **做法**：`scripts/ci_test_shards.py` 按 `scripts/ci_test_durations.json`（本机串行 `--durations` 求和，374 个文件）把测试文件贪心分成 4 片，新文件按中位数权重分配、不会被漏掉；每片一个 runner、一个独立 Postgres 服务，`--cov-fail-under=0` 各产出覆盖率数据。原来的检查步骤搬到并行的 `python-gates`；必需检查 `python` 变成汇总任务，任何分片、gates 或镜像构建失败都失败（被跳过的任务会被分支保护当成通过，所以用 `always()` 显式判断），再合并覆盖率并做 85%/90%/90% 门槛。
- **本机验证**：4 片分别 2:27、4:00、2:32、2:15，共 5580 个测试全过，合并后覆盖率 89%，三个门槛都通过。CI 上第一轮 Pytest 步骤 239 / 340 / 242 / 169 秒（理想 248），用 CI 实测的逐测试耗时（各片 `durations-shard-N` artifact，保留 14 天）重生成耗时表后，按 CI 时间计的各片负载是 226 秒上下（原分配为 214 / 223 / 280 / 187）。逐次运行的波动约 ±100 秒（同一分配下最慢的分片第二轮换了一片），继续调表收益有限；刷新方法：下载 artifact，`cat` 成一个日志，`python scripts/ci_test_shards.py --from-log <log> --write`。
- **main 上不再自动跑 CI**：触发条件从 `push`（main/master）+ `pull_request` 改为 `pull_request` + `workflow_dispatch`（Actions → ci → Run workflow，手动触发时所有任务都跑，不看路径过滤）。代价：分支保护没有要求分支保持最新（`strict=false`），两个 PR 各自通过后合并的组合结果不再被自动测试；怀疑漂移，或发布前想核对 main，就手动跑一次。

## 2026-09-29 设备页「使用时段」可修改（#136，已发布 20260929-session-limits-v1）

- **缺陷**：小程序设备页的「使用时段」卡片只读（夜间休息、单次最长），监护人绑定后无处修改；后端也没有绑定后改写限额的通道（同意授权按绑定版本固定，offer id 是确定性的）。
- **修复**：新增 `PUT /v1/devices/{device_id}/session-limits`（owner/device admin，仅 `parent_for_child`，仅当前监护人自己授予的会话同意；`max_session_minutes` 1–240、`quiet_hours` 起止 HH:MM，传 `null` 取消该项）。`BoundSubjectConsentService.update_session_limits` 用带 `offer_key` 的新 offer 重新授权 chat/tutor/english_practice，原 head 被原子取代（改回旧值也重新授权，不重放旧授权）；`PostgresMultiSubjectRuntimeControl.refresh_profile` 显式轮换控制 profile（`ensure_profile` 只比较人格与主体事实，不会因同意变化轮换），再以 `next_session` 语义投影给设备，进行中的回复不被打断。SQLite 部署没有同意授权与 Session Runtime，接口返回 409 `session_limits_unavailable`。设备页卡片加「修改」，抽屉复用绑定页的时长档位与时间选择，保存后重读已签名 profile 显示设备实际执行的值。
- **发布**：本机以 `20260929-turn-taking-v1` 为基座增量构建三镜像（依赖输入未变），seeded 上传双端校验 PASS；摘要：verifier `aff0c2ec…`，manifest `4b8ce09d…`，source `d9880da7…`，images `54530088…`。`release_ops.sh`（sha256 `346c44ee…`）PREV → `20260929-turn-taking-v1` / `195e104`，旧版备份 `release-ops.sh.pre-20260929-session-limits-v1`。`verify-load`、`freeze` 在 #136 CI 前完成，合并后推 tag，`env`（provider smoke PASS）、`schema`、`cutover`（23:12:38）、`finish`（23:14:01）PASS；未带凭证访问新接口返回 401。cutover 时设备 WSS 断开一次并在几秒内重连（bridge 重启的预期表现）。小程序体验版 `0.2.20260929.1`（1.5 MB）用 DevTools CLI 上传（首次用相对路径报 code 19，要传绝对路径），需在公众平台设为体验版。回滚（未实跑）：`TAG=20260929-session-limits-v1 COMMIT=26e937f177b550a25ce88d46200cfbdaae2a4c68 release-ops.sh rollback`，三角色按 `20260929-turn-taking-v1` 重建，无 schema 变更。
- **验证**：本机 Postgres 上 `test_a_changed_session_limit_reaches_the_reissued_profile`（改后重发的 profile 带新限额，epoch 递增，普通读取在刷新前仍是旧值）、`test_bound_subject.py` 两条、小程序 `npm test` 324/324。未在真机与线上验证。
- **边界**：卡片仍只在 profile 带限额时出现（绑定时两项都未设的孩子看不到入口）；抽屉总是同时写入时长与夜间时段，不提供「取消限额」。

## 2026-09-29 整栈发布 20260929-turn-taking-v1（#133、#134）+ media-edge 同 tag

- **范围**：Edge `playback.flush` 带上 `session_epoch`（停播不再断线）；Core 播放窗口轮次三项 + 播放期「再见」只提交自身区间（详见上一节「Core 侧已修」）；release-ops PREV → `20260929-stop-reconnect-v1`。
- **发布过程**：本机构建四镜像（speaker-model 首次 pip 解析失败，同 Dockerfile 重试通过，与 09-29 livekit-retire 那次相同）；seeded 上传双端校验 PASS，media-edge 镜像 sha256 核对后导入。`verify-load`、`freeze`、`env`（provider smoke PASS）、`schema` 后等 #134 CI 全绿，`cutover`（21:53:14）、`finish`（21:54:36）PASS。media-edge 渲染配置与当前栈只差构建参数与镜像，切换后 healthy，其余容器未变，未带凭证入口 401。
- **电脑模拟复测未能进行**：21:55–22:02 的 10 个场景全部在唤醒应答后被 `minor_quiet_hours` 收尾（设备绑定的是孩子，家长设的使用时段已到休息时间，唤醒只播一句晚安并待命，不开 ASR），不是回归。需在使用时段内复测。

## 2026-09-29 整栈发布 20260929-stop-reconnect-v1（#130）+ media-edge 同 tag

- **发布过程**：本机构建四个 linux/amd64 镜像（Edge 二进制含新日志串核对），seeded 上传双端校验 PASS，media-edge 镜像单独 scp、sha256 核对后导入。`verify-load`、`freeze`、`env`（provider smoke PASS）、`schema` 在 #132 CI 全绿前完成，全绿后 `cutover`（18:36:44）、`finish`（18:38:11）PASS。media-edge 按组件覆盖文件切换：新旧渲染配置对比基准改为当前栈发布树（`20260928-review-batches-v1` 发布树引用的网关 env 已随 LiveKit 清理删除、无法渲染），差异只有构建参数与镜像；其余容器未变，未带凭证的设备入口 401。
- **真机（用户，18:39–18:42）**：「停」生效，但停后设备仍断线重连（这次是设备侧 close 1005），重连后前几句上行电平落在底噪（RMS 90–540，四家 ASR 全空），第三遍才识别；播报快结束时开口的问题不被提交，重说后两句合并回答，第三遍又答一次，表现为「答的是上一个问题」。
- **重连根因（已修，待发布）**：Edge 把 `CANCEL_GENERATION` 转成 `playback.flush` 时丢了 `RealtimeEffect.session_epoch`（fence 里是 0），固件 `ParseGenerationFence` 要求正数，整条消息判违规（`Rejected invalid device media message`）并关闭 WSS。Core 发出的每次停播（语音停止、KWS、抢占）都会触发。串口复现 2/2（「别说了」模拟）。修复：`device_ws_downlink.go` 带上 `effect.GetSessionEpoch()`，回归测试解析 flush 的 fence（main 上失败）。
- **电脑模拟测试（19:00–19:17，Mac 扬声器 Tingting 代替用户，串口 + bridge 日志）**：模拟的「停」4/4 未生效：播放期上行有明显回声，FunASR 把「停」听成别的单字（离线复核为「行」，豆包为「停」），走了语义收尾，钉住的端点既不提交也不结束会话，之后「讲一个短一点的故事」识别出来也不提交，故事播完为止。「别说了」2/3 停下（第 3 次被识别成别的 4 字、同样走了语义收尾）；停下的两次都复现了上面的重连，且重连后「讲一个短一点的故事」已被 FunASR 识别（10 字）仍被当空输入丢弃（`turn discarded after ASR tail timeout partial_present=False`），即此前「重连后第一句为何被当空输入丢弃」。Core 侧已修（#133、#134，已随 `20260929-turn-taking-v1` 上线）：播放结束前 <1 s 开口、越过边界 ≥1 s 且不是回复原文的 final 直接开续问轮；设备播放期语义收尾、与回复原文相同的告别、整点问句的端点先挂起，不再挡住随后的「停」；FunASR 每个任务 sentence_id 从 1 重来，跨任务同 id 只在音频重叠时才算修订，重连后被静默抹掉的 final 不再丢；播放期真正的「再见」只提交它自己的区间，不再和前面挂起的候选拼成闲聊。
- **ASR A/B 补录**：见 `docs/acceptance/run-20260929-asr-ab/findings.md`。轻声组四家全空，远场、噪声组四家一致，换厂商无助于空结果；电平问题在固件 AGC（缺陷 B）。

## 2026-09-29 整栈发布 20260929-stop-playback-v1（播放中语音停止 + 抽取提示词 v3）

- **真机验收**：
  - 第一次：「停」的 final 在故事已播完（`playback_ended`）之后才到，停止路径未触发，和下一句合并成一轮并被回应。
  - 第二次（长故事中途说「停」）：停止生效（`media early playback-stop endpoint` → `media spoken stop interrupted reply ... flush=True` → `preempted`），会话未关；但随后两次「讲一个短一点的故事」都没反应，会话失聪直到超时关闭。
- **失聪根因（Edge，影响所有 Core 主动停播）**：CANCEL_GENERATION → Edge 发 `playback.flush` → 固件为被冲掉的那一代补发 `playback.ended` → Edge 因该代已被替换而拒收并发 `session.error playback_receipt_rejected`、关闭设备 WSS → 固件重连，上行采样从 0 开始（`stream_epoch 2033→2034`、`vad start ... sample=0`）。KWS 停止、输出抢占、身份轮换、输出超时同样会触发；点屏停止不受影响（`button.stop` 是当代终态）。Core 另有两处重连后未复位的位置（`last_playback_end_sample`、`last_asr_evidence_end_sample`）让新 epoch 用了旧 epoch 的边界。
- **修复（待发布，需同时发 media-edge 与 bridge）**：Edge 对已被替换那一代的 `playback.ended/error` 只记录并丢弃、不断线；Core 换 stream epoch 时复位这两个位置。Go 与 Python 各一条回归测试（main 上失败）。
- **未查清**（09-29 18:39 后已复现，见 stop-reconnect-v1 一节）：重连后第一句被当空输入丢弃。

## 2026-09-29 整栈发布 20260929-stop-word-v1（停止词不再当告别；SenseVoice 12 s）

- **真机验收（15:48–16:05 CST）**：讲故事时说「停」三次都没反应，轻点屏幕（`client_stop_assistant`）立即停下回到待命。
- **根因（两层，#127 只修了第 1 层的误判）**：
  1. 设备签名设置 `allowed_barge_in=["button","keyword"]` 不含 voice，固件（`memoria_protocol.cc` 播放期压掉 vad.start）与 Edge（`device_ws_uplink.go` 丢弃）都不送播放期 VAD，「停」的 final 停在没有端点的待定轮次里；#127 之前唯一给它端点的是把「停」误判为告别的收尾定端点，所以那时会走成结束会话。
  2. 即使有端点，媒体提交路径对纯控制指令（`interrupt_command_only`）只把状态改回 listening，从不取消回复、不发 CANCEL_GENERATION；H5 靠 VAD 定端点的「停一下」同样不会停播。
- **修复（待发布）**：`media_session_playback_stop.py`：设备回复进行中、final 为纯停止词且不是回复自身回声时，只提交这句的区间并立即提交；执行步骤仿照 KWS 停止（记已播文本 → 打断并推进 generation → 取消回复 → CANCEL_GENERATION `voice_stop_command`），会话保持。只在 final 触发（partial「等一下」可能长成「等一下我想问…」）。词表接受叠字「停停」「停，停」，不含「停车」。回声判定看最近播报文本末 64 字是否含该停止词。
- **上线后可在 bridge 日志核对**：`media early playback-stop endpoint` → `media final did not start reply reason=interrupt_command_only` → `media spoken stop interrupted reply ... flush=True` → `event=preempted`；回声忽略为 `media playback stop ignored as reply echo`。

## 2026-09-29 整栈发布 20260929-voice-core-refactor-v1（第 5 批语音主链重构）

- **范围**：#123 SenseVoice 只救未覆盖尾段 + 公开测试接缝 + `test_media_session_golden.py` 录制基准；#124 `GenerationRecords`（8 张按 fence 的表）与单一发言权状态 owner `VoiceFloorState`（新增 `voice_floor_divergence_total`）；#125 `PendingTurn`/`OutputState` 子对象、删除 LiveKit 时代可信中断死状态；#126 抽出 `ReplyPipeline`（删 `DuplexVoiceAgent`，`turn_committed` 日志改由 `reply_pipeline` 输出）、删除不可达的 listener-cue 链路、`release-ops.sh` 改为三角色 PREV。四个 PR 期间录制基准一字未变。唯一有意行为变化：身份轮换同时清掉上一主体的 TTS 参考文本。
- **发布过程**：本机完整构建三镜像，`verify-load`（冒烟自带临时 PG）、`freeze`、`env`（provider smoke PASS）、`schema`、`cutover`、`finish` 全部 PASS；readiness ready、心跳 ready、外部 200；media-edge 约 5 s 内重拨新 bridge。
- **真机验收（2026-09-29 14:58–15:17 CST，用户对话，服务器侧日志）**：三个会话，5 轮提交、12 次开口、11 次完整播放并回执 actual_heard；天气查询走 delegation + 实时搜索、过渡语后给结果；「再见」两次正常收尾；沉默后 `owner_silence_timeout` 按时结束；心跳全部 200。
- **验收发现的既有缺陷（发布前基线同样存在；#127 只修了第一层，见下一节）**：讲故事时说「停」，第一次被暂缓、第二次直接结束了会话。原因：语义收尾分类器把单字「停」判为告别，路由第 3 步走 `END_SESSION`（需说话人关卡）而不是第 4 步的停止指令（绕过关卡、只打断播放）。修复：纯停止词/完成确认在词表层优先，不再送语义分类器（`conversation_close_router.lexical_playback_control_only`），有回归测试。
- **SenseVoice**：尾段修复生效（出现 760 ms、4440 ms 的短补救）；整段 30 s 都没有 final 时仍会超时 1 次，默认上限已改为 12 s（`SENSEVOICE_MAX_AUDIO_S`，依据 `docs/runbooks/sensevoice-asr.md` 的实测），待下次发布。
- **仍待**：按键停止本次未测；`livekit.agents` 依赖（TTS 流、ChatContext、openai 插件）的替换为独立系列。

## 2026-09-29 整栈发布 20260929-livekit-retire-v1（旧媒体链退役 + PG-only）

- **范围**：#120（guardian SQLite 孪生删除）、#121（archive 一族 SQLite 实现删除，含 PG 声音登记 RLS 修复与 PG 记忆检索无向量路径排序修复）、#122（下线 LiveKit 旧媒体链：worker、两个 Python 网关、control-api 的 LiveKit 会话/token/回退/放量白名单，readiness 心跳改由 bridge 上报）。用户 2026-09-29 决定下线（没有手机/浏览器实时语音计划）。
- **发布前只读复核**：LiveKit、worker、两个网关 72 小时零业务流量；唯一设备 350 个媒体会话全是 `direct_voice_core`，且在原放量白名单内；control-api `DEVICE_MEDIA_RUNTIME=direct_voice_core`。
- **发布过程**：
  - `uv.lock` 有变化（去掉 vosk），本机完整构建 linux/amd64 三个镜像（清华源；speaker-model 首次因 PyPI 索引响应截断失败，重试通过）。
  - 首轮 `verify-load` 在 `smoke_server_deployment.sh` 失败：预检 control-api 不带 DSN 启动，而监护与档案已是 PostgreSQL-only。脚本改为自带临时 PostgreSQL（NOBYPASSRLS 属主，README 的本机做法），先在服务器用已加载镜像验证 PASS，再入 PR、换提交重建；清掉首轮加载的镜像与发布目录后重跑。
  - `verify-load`、`freeze`（pg_dump 2.7MB）、`env`（provider smoke FunASR/Qwen/Doubao PASS）、`schema`、`cutover`、`finish` 全部 PASS；readiness ready，agent 心跳由 bridge 上报为 ready，外部 200。
  - 切换 bridge 时正在进行的会话 `23210ffb` 被中断，media-edge 5 秒内重拨新 bridge 成功。
- **真机验收（2026-09-29 12:18–12:22 CST，用户对话，服务器侧日志）**：会话 `e854d160` 5 轮提交，7 次开口、6 次完整播放并回执 actual_heard，2 次抢占（1 次被新轮取代、1 次取消），「再见」识别与收尾正常；bridge 心跳全部 200；无新增报错。与发布前基线（旧版本会话 `23210ffb`：4 轮、6 次播完）一致。
- **遗留（发布前后都有，非本次引入）**：
  - ASR 空结果丢段（本次 3 段、基线 4 段）。
  - SenseVoice 补救识别把整个待定缓冲（最长 30 s）送去解码，2 核上约 3 s，超过 2.5 s 超时（`httpx.ReadTimeout`）；应只送 ASR 为空的那一段，已记 TODOLIST，随第 5 批真机验收处理。
  - 播放中说普通内容不打断、也不回应（无已验证 AEC 时的有意策略，`interruption/policy.py` 第 8 步）；「再见」立即生效，播放中的语音停止词见 `20260929-stop-word-v1` 一节。
- **主机清理**：验收后经用户确认完成，见上方「发布身份」。
- **仍待**：`ReplyPipeline` 抽取（去掉 bridge 对 `livekit.agents` 的依赖）与第 5 批语音主链重构，需真机窗口。

## 2026-09-28 整栈发布 20260928-review-batches-v1（双视角评审整改）

- **范围**：评审计划第 1–6 批合并的 #89–#115（进度见 TODOLIST P2-08），已全部随本次整栈发布上线，media-edge 也包含在内。
- **本次修复的生产缺陷**：
  - 账号删除会停在 evolution 一步：`memoria_evolution` 对 `evolution_lifecycle_events` 没有 DELETE 权限（只读核实：权限缺失，control-api 使用该 DSN）。#100 修复，经 `schema` 步骤重放 `007-evolution-schema.sql` 生效。
  - 含监护记录的账号导出返回 500：PG 监护行里的 UUID 和时间无法 JSON 序列化，生产有 1 条 person consent。#103 修复。
  - 自助导出不含任何证据：PG 的 JSONB 列没有解码，证据全部被计为 `unparsed_payload`。#102 修复。
- **潜伏缺陷（生产暂无触发数据）**：
  - 属主是非主体时，被替代的绑定版本不可见，版本列表返回 500（#99）。
  - 绑定主体的记忆编译和留存读取没有带 actor，会被 RLS 全部隐藏（#101；生产证据的 `subject_id` 目前全为空）。
  - 语料维护角色不能标记删除（#104；生产语料 0 条）。
  - self-model JSONB 列表被按字符解码（#96）。
  - 长辈绑定先校验年龄后记关系（#97）。
- **发布时的注意事项**：
  - 本轮新增控制库版本台账 `control_schema_migrations`（#98）。`schema` 步骤会先重放基线，再按编号执行 `database/migrations/`。
  - control-api 的 identity 不再有 SQLite 回退（#112），生产本来就要求 `MEMORIA_IDENTITY_DATABASE_URL`。
  - `ControlSettings` 按域拆分（#111），环境变量名不变。
- **发布过程**：
  - 依赖有变化（新增 psycopg，删掉 sqlalchemy），所以在本机完整构建 linux/amd64 镜像。
  - 阿里云 PyPI 镜像还没有 `psycopg-pool 3.3.3`，pypi.org 当晚又极慢，最后改用清华源；依赖按哈希锁定，换源不影响内容。
  - 以 `20260928-reopen-window-v1` 为基座增量上传，实传 265 MB。各项摘要：verifier `c8b2aac0…`，manifest `d4a5ea2e…`，source `8a434741…`，images `c8e477dd…`。
  - `release-ops.sh` 依次执行 `verify-load`→`freeze`→`env`→`schema`→`cutover`（23:26:54–23:27:47）→`finish`。
  - `env` 只新增了 `MEMORIA_DB_CONTROL_PASSWORD`，provider smoke 一次通过。
  - `schema` 第一次运行时，控制库 schema 已经完整应用（29 张表、`1:baseline`、`memoria_control` 可登录），但因为 psql 只输出了 NOTICE，`grep -v` 过滤后没有输出，在 pipefail 下判为失败，脚本中途退出。#115 修复这个问题，重跑后 `schema=PASS`。
  - `finish` 时，内外 readiness 均为 200，所有容器重启 0 次。
- **media-edge**：同一 tag 按 #87 的先例单独切换，时间 23:29:54。新旧渲染配置只有构建参数和镜像不同；其余容器没有变化，设备入口未带凭证时返回 401，Voice Core 通道已重连。
- **控制库切换 dry-run（PASS，23:32）**：
  - 前两次 dry-run 分别拦下两处历史数据差异：`messages` 中有 294 行 2026-07 的测试消息缺少 `client_message_id`/`request_fingerprint`；`device_media_sessions` 多一列废弃的 `runtime_profile_id`，当前代码不读写这一列。
  - 经用户同意（"老的数据可以删除了，都是测试数据"），删掉这 294 行，并删掉这一列。删除前的备份：`/data/memoria.pre-legacy-message-delete-20260928T153147Z.sqlite3`、`/data/memoria.pre-legacy-column-drop-20260928T153246Z.sqlite3`，完整性检查 ok。
  - 第三次 dry-run 通过：28 张表，18 张非空，共 2266 行，逐表行数与校验和一致，事务已回滚，PG 中没有留下数据。收据在 `releases/20260928-review-batches-v1/.control-store-20260928T153253Z/dry-run-receipt.json`。
  - **apply（用户授权，23:35:26–23:35:43）PASS**：
    - control-api 停机约 17 s。
    - 切换前备份了 SQLite 三件套和 `pg_dump`，与收据 `.control-store-20260928T153526Z/apply-receipt.json` 放在同一目录。
    - 迁移了 28 张表共 2266 行，与 dry-run 一致；PG 中 `profiles` 110 行、`voice_sessions` 649 行、`device_media_sessions` 347 行，`messages_id_seq` 续接到 441。
    - 写入 `MEMORIA_CONTROL_DATABASE_URL` 后重建 control-api，healthy、重启 0 次，内外 readiness 均为 200。
    - readiness 刷新由 `memoria_control` 写入 PG，`marked_at` 为 15:36:50，写入路径正常。
    - 控制库现在是 PostgreSQL。`/data/memoria.sqlite3` 保持原样，可用于回滚，回滚方法见运行手册；切换之后写入的数据不会回到 SQLite。
- **未验证**：发布后还没有真机对话。设备重连和语音链路要等下次唤醒验证。
- **测试底座**：CI 新增 `control-api-postgres` 作业。Control API 与 governance 测试在生产形态的 PG 上运行：用真实 init 脚本建库，按生产角色连接，FORCE RLS 生效。上面这些缺陷都是它找出来的。

## 2026-09-28 小程序 tab 页导航修复（PR #86，体验版 `0.2.20260928.2`）

- **缺陷**：tabBar 页只能用 `wx.switchTab` 打开，另外三处用了其他方式，都会静默失败。第一处，登录页的 tab 路由表漏了伙伴页，从伙伴页触发登录后改用 `redirectTo` 回跳而失败。第二处，「我的」→「人格与声音」用 `navigateTo` 打开伙伴 tab。第三处，设备页敏感入口「私人回顾」（`/pages/memory/index`）经 `openEntry` 统一 `navigateTo`。
- **修复**：新增 `utils/tab-routes.js` 作为 tab 路由的唯一来源，登录回跳与设备页入口遇到 tab 页改用 `switchTab`（去掉 query），「人格与声音」改为 `switchTab`。
- **防回归**：`tests/tab-navigation.test.js` 校验路由表与 `app.json` 的 `tabBar.list` 一致，静态扫描全部 `navigateTo`/`redirectTo` 字面目标（不得是 tab 页，且须在 `pages` 中声明）和指向 tab 页的 `<navigator>`（须 `open-type="switchTab"`），并覆盖登录回跳每个 tab 页；profile、device 页各加一条行为用例。四个新用例在修复前的源码上均失败。`npm test` 316/316，全量 `node --check` 通过。PR #86 于 2026-09-28 17:26（CST）合并（`ac05da0`，CI 全绿）。
- **体验版**：从 main `ac05da0` 用开发者工具 CLI 上传 `0.2.20260928.2`（包 1.58 MB），替代 `0.2.20260928.1`（其内容全部包含在内）。用户已在公众平台设为体验版。
- **真机验收（2026-09-28，用户确认）**：伙伴页触发登录后回到伙伴 tab、「我的」→「人格与声音」进入伙伴 tab、设备页「私人回顾」入口进入回顾 tab，三处均正常。

## 2026-09-28 media-edge 发布 20260928-writer-teardown-v1（写失败即关连接）

- **修复（PR #87，读代码发现，未见线上事故）**：设备 WSS 写循环在 ping 或消息写失败时只退出写协程，不关连接。gorilla 的写错误是粘滞的且不关底层连接，读协程继续把上行送进 Voice Core、控制通道继续收控制，设备却什么都收不到；读超时只由 pong 刷新，而 ping 停了 pong 也停，最长拖到 90 s。现在写循环退出即走现有 `close()`：上报 reason 仍为 `network`（Control 的 close report 是严格白名单，新值会被 422 拒收，且语义本就是可重连的传输中断），Edge 本地原因 `downlink_write_failed` 写进关闭日志，并计入新指标 `device_downlink_write_failed_total`；已由主动关闭设定的原因不会被覆盖。同 PR 两处相邻修复：`VoiceCoreSession.Close/CloseSend` 在 `sendMu` 下执行（grpc-go 禁止与 `SendMsg` 并发），`Close` 先 cancel 以解除阻塞中的发送；`connsBySession` 改为连接被接受后才写入，新 socket 握手失败不再删掉仍在线老连接的 Core 事件路由。三条回归测试不带修复时失败；`go test -race` 通过，新测试 `-count=50` 稳定。
- **发布**：干净 detached worktree（`5e358ec`）按 compose 定义构建 linux/amd64 镜像（revision/version 标签核对无误，二进制含新代码），`docker save` 后 scp 到 `/opt/memoria/incoming/20260928-writer-teardown-v1-media-edge/`，服务器侧 sha256 `dfafe5da…` 校验通过后导入（上传目录已于当日经用户同意删除，镜像仍在本机）。同日经用户同意清理 `/opt/memoria/incoming/` 下 8 个更早的整栈上传目录（`20260925-full-stack-v1` 至 `20260928-session-trust-v1`，约 23 GB），只保留 `20260928-reopen-window-v1`（下次 seeded 上传基座）与 `20260928-followup-endpoint-v1`（回滚目标）；根分区 69% → 49%。切换链为当前栈发布树 `releases/20260928-reopen-window-v1` 的 compose + `component-releases/20260928-writer-teardown-v1/media-edge-component.override.yml`；新旧渲染配置除 build context/参数与镜像外一致（对比用的渲染文件含 env 展开值，比对后立即删除）。17:26:11 切换，healthy、restarts=0，其余 memoria 容器镜像与启动时间未变；外部 readiness 200，未带凭证的设备入口 401。
- **未验证**：切换前 30 分钟内无设备会话，设备重连与新版本下的真机对话需下次唤醒验证；内部 8081 为 TLS，本次未读取运行中进程的新指标值。
- **回滚**（未实跑）：`cd /opt/memoria/releases/20260928-reopen-window-v1 && docker compose -f docker-compose.production.yml -f /opt/memoria/component-releases/20260928-writer-teardown-v1/media-edge-rollback.override.yml --profile media-runtime up -d --no-deps --no-build media-edge`（回到 `20260926-minor-safety-v1`，镜像仍在本机）。

## 2026-09-28 整栈发布 20260928-reopen-window-v1（背景声不再拖住已识别的问句）

- **现象**：#80 上线后真机问天气（14:23，会话 `ad18112b`）已能完整回答，用户确认。但 06:23:41（UTC）问句识别并定下端点后，直到 06:23:58 才提交，延迟 17 s。期间设备 VAD 被背景声触发 5 次，每次 ASR 救援都为空（`empty+vendor_silent`）。每个 `vad start` 都在 `_admit_vad_start` 里撤销端点、重开这一轮，后面噪声段的 VAD 结束位置 ASR 永远覆盖不到，提交因此一再推迟，最终提交的仍是原来的 12 字。
- **修复（PR #83）**：已有文字的设备轮次被端点之后的 `vad start` 重开时，从第一次重开起计一个 2.0 s 的证据窗口（`_REOPEN_EVIDENCE_WINDOW_S`，之后的重开不延长）。到期时若新声音没有产生任何文字（没有更晚的 final，也没有新的非空 partial），就按有文字覆盖的原端点提交；有文字则照旧合并。窗口绑定同一逻辑轮次（轮次起点须一致），仅设备会话，回复进行中不生效。实时查询、报时、结束语三类钉住端点本就忽略后续 VAD，不受影响。代码在不设预算的 `media_session_input.py` 与状态字段里，`media_session_turns.py` 未动。代价：说话中途停顿后，若继续说的内容 2 s 内没有被识别出文字，前半句会先提交。三条测试（噪声复现在不带修复时失败、继续说话仍合并、不会按旧端点提交新轮次）；agent 单测 2265 passed。
- **发布**：tag `20260928-reopen-window-v1` → `173445d`（#83 分支头，与合并提交 `ef05a38` 同树；为省一轮 CI，release-ops PREV 改动放在同一 PR）。seeded 上传（基座 `20260928-followup-endpoint-v1`，实传 175 MB）；摘要：verifier `c8b2aac0…`，manifest `142292be…`，source `432483be…`，images `032e4256…`。`verify-load`→`freeze`→`env`→`schema`（无更新）→`cutover`（14:58:06–14:59:00）→`finish` 全部一次 PASS，restarts=0，外部 8443 就绪 200，media-edge 第 8 次重连成功，运行中 bridge 含 `_REOPEN_EVIDENCE_WINDOW_S`。15:18 真机复测（会话 `9aef3935`）：天气走实时查询约 0.8 s 开口、追问约 4.6 s，但环境安静，窗口未触发；嘈杂环境验证见 TODOLIST「2026-09-28 收尾待办」。

## 2026-09-28 整栈发布 20260928-followup-endpoint-v1（追问不再中途待命）+ 证书续期 + 固件 build 10

- **现象与根因**：12:59 真机唤醒后问天气，机器人没回答就回到待命。会话 `19bb6bd1` 已能建立（`media-sessions` 200，说明会话信任修复生效）。bridge 在唤醒应答播完后，因一个**空文本** final 走「播放后追问」路径定下端点，启动了 2.5 s 绝对截止。用户继续说话，端点随之推进，但只刷新了短宽限；绝对截止在完整问句（9 字）到达的同一瞬间到期，bridge 请求待命（`turn_prepare_timeout`）。修复（PR #80）：空文本 final 不定端点；推进端点时宽限与绝对截止一起重新计时（`MediaVoiceSessionState.restart_endpoint_bounds`，`media_session_turns.py` 预算下调到 1858）。两条回归测试不带修复时失败。
- **发布**：tag `20260928-followup-endpoint-v1` → `a2ea41e`（#81 分支头，与合并提交 `a9373b1` 同树）；仅 agent 源码与 release-ops 变更，无 schema、compose、env、依赖变更。seeded 上传（基座 `20260928-session-trust-v1`，实传 180 MB）PASS；摘要：verifier `c8b2aac0…`，manifest `5621a247…`，source `82d0af88…`，images `b92a944f…`。`release_ops.sh` 仓库版（sha256 `ec0f5a03…`），旧版备份 `release-ops.sh.pre-20260928-followup-endpoint-v1`。`verify-load`→`freeze`→`env`（provider smoke 一次通过）→`schema`（无文件更新）→`cutover`（14:00:36–14:01:29）→`finish` 全部一次 PASS，restarts=0，外部 8443 就绪 200；运行中 bridge 已含 `restart_endpoint_bounds`。14:23 真机问天气（会话 `ad18112b`）完整回答，无 `turn_prepare_timeout`，用户确认。
- **证书**：腾讯云证书开了自动续费，但「未托管、未关联资源」，新签的证书不会自动到服务器，服务器仍是 10-18 到期的 `ZLjud9yE`。用户下载续期证书（Nginx 格式，`atJtgIDd`，TrustAsia DV，SAN aigcnice.com/www，**有效至 2026-12-17 02:59:59 GMT**），核对证书与私钥配对后原位替换 `/etc/nginx/ssl/aigcnice.com_bundle.crt` 与 `.key`（旧对备份在 `/etc/nginx/ssl/backup-pre-20260928-renewal/`），`nginx -t` 通过后 reload。443、8443 均返回新序列号 `50460EFB…`，配网链接页与 8443 就绪 200，开发板 display-profile 在 reload 后正常。本机解压的私钥副本已删除。下次续期同样需要手动下载安装。
- **固件 build 10**：`MemoriaMascotDisplay::SetStatus` 吞掉上游待机时每 10 s 写入的 `HH:MM`，屏幕不再显示时间（用户要求）。USB 写入开发板 `ota_0`（app sha256 `7bdfd9b3…`），启动为 build 10、idle、伙伴绵绵；**用户已亲眼确认不显示时间**。写前未整槽回读（被覆盖的 build 9 可由 tag `20260928-session-trust-v1` 重建；build 8 回读在 `pre-build9-20260928/`）。未发布 OTA。
- **Docker 镜像清理（用户批准）**：仓库 `docker_image_retention.sh` 在 dry-run 时 0 候选，因为全部 `rollback-*`、`agent-runtime-base` 以及 `.cutover/pre-state.txt` 引用的 tag 都受保护。按运维手册「当前 + 一个紧邻回滚」手动清单删除 211 个 tag（155 个镜像，`docker rmi` 不带 `-f`，0 失败），根分区 90% → 55%。保留：运行中镜像、`20260928-child-binding-v2` 与各 `rollback-20260928-session-trust-v1-pre`、media-edge 当前与 `20260926-edge-flush-v1`、最新 2 个 runtime-base、sensevoice、全部第三方镜像。早于 09-25 的版本已无法从本机镜像回滚。

## 2026-09-28 整栈发布 20260928-session-trust-v1（会话设备信任跟随 onboarding）

- **范围**：tag `20260928-session-trust-v1` → `5ae5929`（#78 分支头，与合并提交 `dcc4fd95` 同树）。服务端代码：#76 只改小程序与固件（服务端无变更）；#77 改 `services/session_runtime/postgres_schema.sql`（`action_device_lock_trust` 以 `device_onboarding_devices` 为绑定/生命周期权威、仍尊重 fleet 封禁状态；桥接角色读授权）、`services/device_fleet/bootstrap_postgres_schema.sql`（同一授权，forward-only）、`postgres_store.py` 就绪检查；#78 release-ops PREV。无 compose、env、依赖变更。
- **构建与上传**：5 个角色本机 linux/amd64（compose 定义，干净 worktree），revision/version 标签核对无误；seeded 上传（基座 `20260928-child-binding-v2`，实传 173 MB）双端校验 PASS。摘要：verifier `c8b2aac0…`，manifest `cf3d24e3…`，source `ec2ebd93…`，images `239b9ea9…`。`release_ops.sh` 仓库版安装（sha256 `37af418c…`），旧版备份 `release-ops.sh.pre-20260928-session-trust-v1`。
- **五角色**：`verify-load` PASS → `freeze` PASS → `env` PASS（env 键无变化，provider smoke 一次通过，FunASR 6/6）→ `schema` PASS（更新 session_runtime 与 onboarding 两个 schema 文件）→ `cutover` PASS（12:43:48–12:44:42）→ `finish` PASS（readiness `ready` 且为新 tag，定时刷新 `Result=success`，外部 8443 就绪 200，全部 restarts=0）。media-edge 第 6 次重连连上新 bridge。
- **上线后实测**：以 `memoria_action_executor` 调用新 `action_device_lock_trust`，开发板（`e8a27e45` v3）返回 `untrusted / device_attestation_unavailable`（允许启动）；桥接角色可读 `device_onboarding_devices`，executor 无直接权限。开发板 display-profile 每 20 s 200（12:44:06 一次 502 为 control-api 重建瞬间），control-api/agent/bridge 切后无 error。之后的真实请求（至 15:20）：设备页 `runtime-profile` 3 次均 200（此前一直 403），机器人 `media-sessions` 3 次均 200，对话正常。
- **清理（用户批准）**：删除 `/opt/memoria/incoming/` 下 8 月 4 批上传制品（约 9.5 GB；无容器挂载），根分区 98% → 90%（剩 12 GB）。`releases/` 目录（含 PostgreSQL 挂载 schema 的 `20260827-architecture-split-v1`）与 9 月制品未动。Docker 镜像 185 个约 59 GB，旧 tag 未清理。
- **回滚**（未实跑）：`TAG=20260928-session-trust-v1 COMMIT=5ae5929ccfa57d0c671b4ee565a3c2a70d53c71f release-ops.sh rollback`，6 个角色按 `20260928-child-binding-v2` 整栈 compose 重建；schema 不回滚（函数改动向后兼容，旧代码同样调用该函数）。

## 2026-09-28 配网后卡「连接中」、伙伴显示不一致与设备页整改（PR #76，已随 `20260928-session-trust-v1` 上线）

- **卡「连接中」根因（固件）**：未绑定开机时 `ActivationTask` 里 `protocol_->Start()` 返回 false 后直接退出，不发 `MAIN_EVENT_ACTIVATION_DONE`；手机完成绑定后 `RunActivationRetry` 激活成功，只调 `on_connected_`（仅 DismissAlert），设备永远停在 `activating`：屏幕「连接中」、音频引擎不启动、只在 idle 跑的 display-profile 轮询不跑（线上 10:39:04 激活确认后再无设备请求）。重启后走已绑定路径即正常。修复：重试激活成功后停 BLE、`esp_restart()` 进已绑定启动路径（与「被服务器释放→重启」同一模式）。**固件 build 9**（app sha256 `28073ebe…`）已 USB 写入开发板 `ota_0`（写前回读 `firmware/esp32/artifacts/backups/pre-build9-20260928/ota0-before.bin`，sha256 `b48a915e…`），已验证已绑定启动 → idle → 显示绵绵；**「配网→重试激活→重启」这条修复路径未真机触发**（需重新配网）。未签名发布 OTA。
- **伙伴不一致根因（小程序）**：绑定时的 `persona_selection` 只写进设备绑定默认人格（线上 `e8a27e45` v3 = `mianmian:v1`，设备屏正确），账号资料 `companion_id` 仍是 09-25 选的桃喜，而首页/设备页/伙伴页读账号资料。修复：绑定成功后把所选伙伴写成账号伙伴（`PUT /v1/memory/profile`，失败不影响绑定）。**现有账号数据未改**：用户需在伙伴页选一次绵绵。
- **设备页**：「角色与声音」用 `navigateTo` 打开 tabBar 页（静默失败）→ `switchTab`；自定义 tabBar 在页面之上，挡住所有底部抽屉的确认按钮 → tabBar 增加 `hidden`，抽屉打开时隐藏并锁页面滚动，选项区 `scroll-view`；唤醒词抽屉内置自定义唤醒词，删掉底部重复卡片，目录工程说明改为按字数的通俗说明；网络只显示「已连接/未连接」（不再写「家庭网络」）；年龄申报改为带间距的三格选项；备注改为点击弹窗编辑；去掉与顶部卡重复的双格统计与模式胶囊；按「设备 / 使用者 / 设备管理」分组，解除绑定置底。按使用人分配人格保留（自建人格唯一入口）。「在线」仍由激活就绪推导，不是实时心跳。
- **伙伴页**：横向拼接卡片改为堆叠卡组（五位伙伴循环，前卡可拖动切换、点后面的卡直达，卡上试听包内音频），导航标题改为「伙伴」。实时媒体门禁放行伙伴页试听（与绑定页同一约束：只播 `assets/voices/`）。
- **验证**：`npm test` 310/310；固件 `test_memoria_protocol_source.py` 通过；微信开发者工具自动化（mock 登录与接口）截图确认伙伴卡组、设备页各分组、唤醒词与人格抽屉按钮可见、点「角色与声音」进入伙伴 tab。未在真机上看。体验版 `0.2.20260928.1`（包 1.58 MB）已用微信开发者工具 CLI 上传（miniprogram-ci 本次因出口 IPv6 不在上传 IP 白名单被拒 `-10008`），**需在公众平台设为体验版**。
- **新发现的线上阻塞（数据已修；代码由 PR #77 修复，已随 `20260928-session-trust-v1` 上线）**：会话运行时的设备信任表 `device_fleet_devices`/`device_fleet_certificates` 仍指向 08-28 回填的旧绑定 `d1c67b2e` v2，重新配网只写 onboarding 存储与 Identity，没有任何代码更新这两张表 → `action_device_lock_trust` 返回 `device_binding_mismatch`（revoked）。设备页 `runtime-profile` 一直 403；按代码，机器人 `POST …/media-sessions` 走同一 `PostgresSessionRuntimeService.start`，也会被拒（12:59 起机器人会话已 200，证实修复有效）。
- **数据修复（2026-09-28 11:45 CST，用户批准）**：生产库单事务把 `device_fleet_devices`（`state_version` 2→3、`binding_version_floor` 3）与唯一 active 证书行改指向 `e8a27e45` v3，行数守卫各 =1。之后 `action_device_lock_trust` 返回 `untrusted / device_attestation_unavailable`（允许启动会话，与重新绑定前同级）。机器人对话与设备页 runtime-profile 尚未在修复后实测。根治（信任函数改读 onboarding 权威）由 PR #77 完成并已上线，重新配网不再需要手工修数据。

## 2026-09-28 整栈发布 20260928-child-binding-v2（孩子绑定真正放行）

- **根因**：v1 的 #67 判定只认 `identity_relationship_source_confirmed`（要求 `status='pending'`），而绑定路由对账号所有者的 `guardian_of` 调 `attest_binding_relationship`，写的是 `active`（证据 `guardian_attestation_v1:device_binding`，只有 `confirmed_by_source_at`）。#67 的单测造的是 pending 声明，所以没测出。PR #72：所有者兼监护人时，active 或 pending 的 `guardian_of` 都算；非所有者仍需 `emergency_contact_for`。新增路由级测试按线上被拒请求原样重放（三项授权、同样偏好），不带修复时返回线上同一条 409。
- **范围**：tag `20260928-child-binding-v2` → main `87f3560`，服务端只改 `services/identity/binding_roles.py`；无 schema、compose、env、依赖变更。`release_ops.sh`（sha256 `f0ee5d60…`）PREV = `20260927-child-binding-v1` / `56d103b`；旧版备份 `release-ops.sh.pre-20260928-child-binding-v2`。
- **构建与上传**：5 个角色本机 linux/amd64，标签核对无误；seeded 上传（基座 `20260927-child-binding-v1`，实传 162 MB）双端校验 PASS。摘要：verifier `c8b2aac0…`，manifest `82c1237e…`，source `08b0e02b…`，images `a6a2f838…`。
- **五角色**：`verify-load` PASS → `freeze` PASS → `env` 首跑 FAIL（FunASR 样本识别为「慢慢说就好。」）；我的命令没在失败时停下，`schema` 紧接着跑了（PASS，无文件更新），随后 `env` 原样重跑 PASS → `cutover` PASS（09:41:48–09:42:42）→ `finish` 两次 FAIL（第 1 次 readiness 刷新同一 FunASR 样本失败；第 2 次 readiness 已 `ready` 但 systemd 定时刷新在同一样本失败）→ 第 3 次 PASS（09:52:52，readiness `ready` 且为新 tag，`Result=success`，外部 8443 就绪 200，全部 restarts=0）。media-edge 第 5 次重连连上新 bridge。
- **已知问题（已定位，修复待随下次整栈发布上线）**：FunASR 冒烟的「慢慢说就好。」失败不是 FunASR 模型或端点变化，也不是期望文本问题（冒烟没有音频 fixture，样本由豆包现场合成）。冒烟与生产都用 `fun-asr-realtime`、`max_sentence_silence=550`（代码默认值自 07-17 初始化起未变，冒烟脚本自 09-04 起未改）。失败的总是第 6 个样本，即 `low_magnetic` 音色（sad、0.95 语速）读「我在这里，慢慢说就好。」：逗号处的停顿约 555 ms（豆包词时间戳 1332→1887 ms），恰好卡在 550 ms 断句阈值上，所以 FunASR 有时把它断成「我在这里。」和「慢慢说就好。」两个 final。冒烟只保留最后一个 `sentence_end`，因此报 mismatch。09-28 在生产 agent 镜像里只读探针 15 次，复现 1 次（`low_magnetic` 第 0 轮）。生产链路本身会把同一轮里的所有 final 拼接（`orchestration/speech_timeline.py` `_canonical_text_from_segments`），设备与小程序不受影响。修复在 `scripts/provider_smoke_test.py`：按顺序累积全部 final，期望/禁止标记对拼接文本判断，词时间戳跨句单调；仍要求两个标记都识别出来，只识别出后半句照样 FAIL。修改版在生产镜像内实跑 2 次 PASS，其中 1 次命中拆句（2 个 final）。上线前 `/opt/memoria/current/scripts/` 仍是旧版，`env`/`finish`/定时刷新仍可能间歇失败，原样重跑即可。
- **上线后实测**：control-api 运行代码含新判定；control-api、agent、bridge 切换后无 error/traceback。开发板 07:41 起没有任何请求（发布前已离线）。10:39 用户以「给孩子使用」完成绑定（`e8a27e45` v3，勾选长期记忆），绑定真正放行。
- **遗留数据（09-28 10:07 已清理）**：失败的孩子绑定在 Identity 留下 3 个未绑定的孩子人物（09-27 22:20、23:19，09-28 00:13）。经用户同意，在一次性 control-api 容器里对每个人物按解绑路由的做法跑 `forget_subject_plans` + `SubjectDeletionService.delete_subject(redact_identity=True)`：删除台账 3 条 `completed`，人物 `disabled`、名字改为「已删除的使用人」，Identity 审计/outbox 与配网绑定/激活表中不再含原名。按设计保留不含内容的 `guardian_of` 关系与人物占位行（Identity 删除保护，不做物理删除）。
- **回滚**（未实跑）：`TAG=20260928-child-binding-v2 COMMIT=87f3560824ef16cd74a07133f8a921ce835c56ba release-ops.sh rollback`，6 个角色按 `20260927-child-binding-v1` 整栈 compose 重建。

## 2026-09-28 整栈发布 20260927-child-binding-v1（孩子绑定 + 冲突提示）

- **起因**：「给孩子使用」绑定在线上两次 `POST /device-bindings` 409 `binding_conflict`（09-27 22:20、23:19，意图 `parent_for_child`），即 PR #67 的 Identity 缺陷。失败后领取停在 `binding_committing`，10 分钟内挡住同账号的新会话（`POST /device-claims` 409），小程序误显示「设备正在被其他账号设置」。
- **范围**：tag `20260927-child-binding-v1` → main `56d103b`。服务端实际变更只有 `services/identity/binding_roles.py`、`service.py`；无 schema、compose、env、依赖变更。`release_ops.sh`（PR #70，sha256 `394ed8d9…`）PREV 改为 `20260927-unbind-release-v1` / `00a94cb`，六个目标都在 PREV 整栈 compose 上（09-27 23:30 只读核对），去掉 control-api 组件链；旧版备份 `release-ops.sh.pre-20260927-child-binding-v1`。
- **构建与上传**：5 个角色本机 linux/amd64 用 compose 定义构建，revision/version 标签核对无误。制品在构建机 scratchpad（verifier 必须在源码树外）。seeded 上传（基座 `20260927-unbind-release-v1`，实传 193 MB）双端校验 PASS。摘要：verifier `c8b2aac0…`，manifest `eebe1fb7…`，source `081813dd…`，images `84591c71…`。
- **五角色**：`verify-load` PASS → `freeze` PASS → `env` PASS（env 键无变化，全部 provider smoke 一次通过）→ `schema` PASS（无文件更新）→ `cutover` PASS（00:06:30–00:07:24，全部 healthy）→ `finish` 首跑 FAIL（readiness 刷新的 provider smoke 报 `Doubao word timestamp alignment is degraded`，外部 TTS 抖动；此时 readiness `not_ready`、外部就绪 503），原样重跑 PASS（readiness `ready` 且为新 tag，定时刷新 `Result=success`，外部 8443 就绪 200，全部 restarts=0）。media-edge 在第 4 次重连连上新 bridge。
- **上线后实测**：control-api 容器内确认 `parent_emergency_contact_verified` 已在运行代码；control-api、agent、bridge 启动 5 分钟内无 error/traceback；开发板照常轮询激活清单（未绑定，409）。**更正**：00:13 真机「给孩子使用」仍 409 `binding_conflict`——该判定只认 pending 声明，而绑定路由 `attest_binding_relationship` 写的是 active 认定（线上 `identity_relationship_source_confirmed=f`、`identity_relationship_active=t`），修复在 v1 从未命中；由 `20260928-child-binding-v2` 修正，见上节。
- **小程序**：体验版 `0.2.20260927.6`（main `56d103b`，编译预检 190 个文件，包 1.58 MB）已上传（**需在公众平台设为体验版**）。认领冲突提示不再说「其他账号」，改为说明有一次未完成的认领、最多 10 分钟后释放；绑定冲突改为在绑定页原地重试，不再提示换码。
- **回滚**（未实跑）：`TAG=20260927-child-binding-v1 COMMIT=56d103b62cfedfb0d79b51d13c4075853e1fb931 release-ops.sh rollback`，6 个角色按 `20260927-unbind-release-v1` 整栈 compose 重建；schema 不回滚（本次未改）。

## 2026-09-27 重扫已上云机器人的二维码不再卡在「连接云端」（PR #68）

- **现象**：机器人已连上 Wi‑Fi，小程序却显示「还没连上 Memoria 云端」。线上 nginx 日志与 `device_onboarding_events`：22:49 会话 `onb_5270…` 1 秒内通过在线证明并 `claim_reserved`；22:54、22:56 重扫同一张码，introspect 正确返回该会话，但小程序一律重走 BLE 与 Wi‑Fi，机器人 `POST …/challenge` 每 4 秒 422（`issue_challenge` 只接受 `ble_connecting`…`device_online`），90 秒后超时。Wi‑Fi 与到 `aigcnice.com:8443` 的 TLS 正常。
- **修复**（main `d6eec9c`）：introspect 返回已上线的 onboarding 会话时直接接续：`device_online` 预留领取，之后的状态取回已有领取（与 `resume` 共用）。`node --test` 305/305，main CI 全绿。体验版 `0.2.20260927.5`（CI 机器人 1，编译预检 190 个文件，包 1.58 MB）已上传（**需在公众平台设为体验版**）；手机上未验证。
- **机器人侧**：配网随机码每次进入配网重新生成，重启或轻点屏幕即换新码，新码不受影响。

## 2026-09-27 配网体验与孩子绑定修复（PR #67）

- **绑定冲突根因**：「给孩子使用」时 Identity 要求紧急联系人与孩子之间有 `emergency_contact_for` 关系，而声明的监护人本人（账号所有者）没有这条关系 → `ModeConstraintError` → 小程序显示「设备绑定状态发生冲突」。修复在 `services/identity/binding_roles.py`：所有者监护人且 `guardian_of` 已源确认时，可兼任孩子的紧急联系人。判定只认 pending 声明，随 `20260927-child-binding-v1` 上线后仍未命中真实绑定；`20260928-child-binding-v2`（PR #72）修正。
- **小程序**：扫机器人码进入时先提示微信登录（`scan_device` 原因文案），登录后自动回到配网；人格选择改为左右滑动的重叠卡片（吉祥物、性格、音色、试听），试听音频为生产豆包 TTS 预先渲染、打包在 `assets/voices/`（5 个 mp3，共 156 KB），不走实时媒体；绑定冲突、领取冲突或过期时提示「轻点机器人屏幕换一张新二维码」，并给出重新扫码按钮。体验版 `0.2.20260927.4` 已上传（**需在公众平台设为体验版**）。
- **固件 build 8**：二维码标题改为「欢迎使用 Memoria」，说明文字改为「微信扫一扫 开始配网」，不再在绑定前显示伙伴名；patch `0030` 让字体资源在二维码显示前加载，避免文字残缺几秒；在二维码界面轻点屏幕会换一张新码（3 秒防抖）。开发板已 USB 刷入；build 8（sha256 `046492ee…`）已签名发布为 `current.json`。
- **开发板状态**：服务端还留着一个卡住的领取（`binding_committing`）。重新扫码会新建会话，不影响。

## 2026-09-27 微信扫一扫直达配网页（PR #66）

- **问题**：体验版 `0.2.20260926` 早于 #57，严格契约拒收 introspect 的 `purpose`（「包含未声明字段 purpose」）→ 体验版 `0.2.20260927` 已上传。微信「扫一扫」只显示裸载荷 → 固件构建 4 把二维码改为 `https://aigcnice.com/memoria-bind/?b=<载荷>`，小程序拆包，体验版 `0.2.20260927.1` 已上传（**需在公众平台设为体验版**）。
- **线上**：WMS 的 443 server 块新增 `include /etc/nginx/snippets/memoria-bind.conf;`（备份 `/etc/nginx/wms.pre-20260927-bind-link`），`/var/www/memoria-bind/index.html` 说明页；访问日志关闭。构建 4（sha256 `f1e06234…`）已签名发布为 `current.json`；开发板 USB 刷入构建 4（未绑定设备不跑 OTA）。
- **公众平台规则**：前缀 `https://aigcnice.com/memoria-bind/`、页面 `pages/device-onboarding/index`。校验文件 `w1ET0CkeeZ.txt` 已放在 `/var/www/memoria-bind/`（公网 200、字节一致；不入仓库，迁移服务器时要一并带走）。**待用户**：公众平台点校验、保存；发布后对正式版生效。
- **风险**：`/etc/nginx/ssl/aigcnice.com_bundle.crt`（443 与 8443 上 aigcnice.com 共用）原定 2026-10-18 到期，**2026-09-28 已换为续期证书，至 2026-12-17**（见当日证书一节），非 certbot 管理，需在到期前续期，否则设备、小程序与该规则同时失效。

## 2026-09-27 整栈发布 20260927-unbind-release-v1（重新配网 + 解绑释放设备）

- **范围**：tag `20260927-unbind-release-v1` → main `00a94cb`（PR #57 已绑定设备只更新 Wi-Fi；#60/#61 小程序内联 svg 图标替换；#62 解绑时释放 device_fleet 绑定；#63 `release_ops.sh` 认 control-api 的 OTA 组件链）。schema：`services/device_fleet/bootstrap_postgres_schema.sql` 新增 `device_onboarding_sessions.purpose`（加列带默认值，旧代码不受影响）。compose、env 无变更。合并后 main CI（`00a94cb`）全绿后才切流。
- **构建与上传**：5 个角色镜像（agent、control-api、device-media-gateway、miniprogram-gateway、speaker-model）本机 linux/amd64 用 compose 定义构建，revision/version 标签核对无误；media-edge 无代码变更未重建。seeded 上传（基座 `20260926-minor-safety-v1`，实传 174 MB）双端校验 PASS。制品摘要：verifier `c8b2aac0…`，manifest `1737f3c1…`，source `099470ec…`，images `de7b3496…`。发布脚本以仓库版安装（sha256 `c15b1eca…`），旧版备份 `release-ops.sh.pre-20260927-unbind-release-v1`。
- **五角色**：`verify-load` PASS → `freeze` PASS（control-api 按 `20260927-device-ota` 组件链、其余按整栈 compose 校验；回滚标签、pre-state、env 备份、`pg_dump`、SQLite 备份在 `.cutover/`）→ `env` 首跑 FAIL（真实 FunASR 冒烟 6 个样本中 1 个识别为「慢慢说就好。」，根因见 09-28 v2 节「已知问题」），原样重跑 PASS（env 键无变化，全部 provider smoke 通过）→ `schema` PASS → `cutover` PASS（20:36:59–20:38:17，全部 healthy）→ `finish` PASS（readiness `ready` 且为新 tag，定时刷新 `Result=success`，外部 8443 就绪 200，全部 restarts=0）。bridge 重建时 media-edge 断开，第 3 次重连成功。
- **数据修复**：发布前只读核对生产 fleet 只有 1 台设备，即开发板 `dev_atk_a4cb8fd6095c`：fleet `bound`（binding `d1c67b2e…`，activation v3），Identity 同一 binding `revoked`。发布后在 control-api 容器内调用新代码 `release_device_binding(reason=identity_revoked_repair)` → `True`：设备 `provisioned`、binding/claim `released`，activation_version 保持 3。
- **真机**：开发板（构建 3）在 10 分钟复查点 display-profile 409 → 清单 409 → `Device released by the server; restarting into nearby bootstrap` → 重启后 3 s 显示二维码并广播 `MEM-095C`，激活后台重试。09-28 已完成重新绑定（「给孩子使用」，`e8a27e45` v3）并多次对话验收，见当日各节。
- **回滚**（未实跑）：`TAG=20260927-unbind-release-v1 COMMIT=00a94cbc6ee760739e9c36c912a8b5ff8ab354b7 release-ops.sh rollback`：control-api 按 `20260927-device-ota` 组件链重建，其余 5 个按 `20260926-minor-safety-v1` 整栈 compose 重建；schema 不回滚（加列向前兼容）。开发板数据修复不随回滚恢复（旧代码下设备保持 `provisioned`，可正常重新认领）。

## 2026-09-27 小程序体验版 0.2.20260927

- **范围**：main `00a94cb`（含 PR #57 已绑定设备「重新配网」小程序流程；PR #60/#61 内联 `<svg>` 改为 CSS 绘制的勾与折叠箭头，配网步骤勾、设备页抽屉选中勾、首页与数字分身折叠箭头恢复显示）。
- **上传**：Node 24.16.0 经 `upload:test`（CI 机器人 1，dry-run 编译预检 185 个文件，包 1.42 MB）上传成功；上传前 `node --test` 291/291 通过、main CI 全绿。**提交审核与正式发布需在公众平台手动完成。**
- **服务端**：与整栈发布 `20260927-unbind-release-v1` 同为 main `00a94cb`，重新配网与解绑释放 fleet 绑定的服务端改动已上线，体验版可直接用于开发板重新绑定验收。

## 2026-09-27 设备在线升级（OTA）上线 + 解绑误判回归修复（PR #58）

- **范围**：PR #58（main `b317481`）。固件：display-profile 409 先核对 Activation Manifest，只有清单也 409 才清 `activation_v` 并重启进未绑定流程（PR #56 版本在音频运行时开 BLE，AFE 任务创建失败、设备不能对话）；BLE 配网面停止时释放 NimBLE；签名 OTA（`memoria_firmware_update.*`、patch `0029`、`publish_firmware_release.py`）。control-api：设备签名的 `GET /v1/devices/{id}/firmware-release` 与 `.../{build}/image`。
- **发布**：`deploy_control_component.sh` 因 main 上 `scripts/release_ops.sh` 常量改动（PR #55）越界而拒绝，按 `20260925-control-binding-actor` 先例用 hotfix 分支 `hotfix/20260927-device-ota`（`048a83a` + 仅 `services/control_api/app/device_firmware.py`、`routes/device_onboarding.py` 与测试），tag `20260927-device-ota` → `42b5c54`；main CI（run `36311652118`）全绿后 dry-run PASS → `--cutover` PASS（PG schema/RLS 校验、单容器重建）。切后 healthy、restarts=0，其余容器未动，外部 ready 200，新接口无证书 401。
- **nginx（线上就地改）**：`/var/lib/nginx/proxy` 属 `nobody` 0700 而 worker 是 `www-data`，任何超出内存缓冲的代理响应都会被截断（首次 OTA 下载在 79% 断开，设备按哈希/长度校验拒收）。线上 `/etc/nginx/snippets/memoria-https.conf` 只在 `location ^~ /memoria-api/` 加 `proxy_max_temp_file_size 0`（备份 `.pre-20260927-ota`，`nginx -t` 通过后 reload）；仓库文件同步此行。**注意**：线上片段与仓库仍不一致（仓库已下线 H5，线上未部署），不要整份覆盖。
- **固件发布**：私钥 `~/.config/memoria/secrets/firmware-release-ed25519.key`（本机，600，需离线备份），公钥 `b9fc4ad5…b009a`。服务器目录 `/var/lib/memoria/firmware-releases/memoria-esp-vocat/`（= control-api `/data/firmware-releases`），当前 `current.json` → 构建 3（`2.4.2+m3`，sha256 `b18cd9b9…`）。SSH 用户 `ubuntu`，上传脚本在家目录暂存后 `sudo -n install`。
- **真机（开发板 `dev_atk_a4cb8fd6095c`）**：刷前回读 NVS+identity（`0x9000–0x20000`）备份在 `firmware/esp32/artifacts/backups/pre-ota-20260927/`。构建 1 USB 刷入后首次 OTA 暴露电量计 I2C 超时 → `ESP_ERROR_CHECK` abort（写 flash 期间）→ 下载中反复重启；已撤回发布、改为读失败沿用上次值，构建 2 USB 刷入。构建 2 → 3 在线升级 PASS：空闲 30 s 发现，83 s 下载，校验后写 `ota_1`，空闲重启，以构建 3 从 `ota_1` 启动并在激活答复后 `confirmed`，AFE 正常。**未测**：回滚（新镜像确认前复位回旧槽）。
- **已知缺口（已于同日 `20260927-unbind-release-v1` 修复）**：小程序「解除绑定」只撤销 Identity 绑定、不释放 device_fleet 绑定；开发板数据已修复，见上一节。

## 2026-09-26 小程序体验版 0.2.20260926（PR #56）

- **范围**：main `0b2cb43`（PR #56）。小程序全部页面统一 mist 浅色主题（删除 `.theme-dark` 与 sky/night/warm 背景图，导航栏/tab 栏统一，次要文字等 token 调深到 WCAG AA）；`DEVICE_ALREADY_BOUND` 提示改为先解绑再扫码。
- **上传**：Node 24.16.0 经 `upload:test`（CI 机器人 1，编译预检 185 个文件，包 1.41 MB）上传成功。**提交审核与正式发布需在公众平台手动完成。**
- **固件**：同一 PR 的固件改动（patch `0028` 配网模式必出二维码、在线解绑 409 后重出二维码、联网阶段吉祥物缩小留出字幕带）已编译通过，**未刷机、未真机验证**（开发板未连接）。
- **已知缺口（已于 09-27 `20260927-unbind-release-v1` 修复）**：设备页「重新配网（不解除绑定）」对已绑定设备曾被服务端 `DEVICE_ALREADY_BOUND` 拒绝。

## 2026-09-26 整栈发布 20260926-minor-safety-v1

- **范围**：tag `20260926-minor-safety-v1` → main `048a83a`（PR #52 故障注入测试与 TODOLIST 清理；PR #53 ASR 救援 sidecar 可重建与「我。」幻觉修复；PR #54 P0-04 八项产品决定与孩子危机提醒入队缺陷修复）。compose、env 无变更；identity 与 guardian 两个 schema 文件的函数有更新。合并后 main CI（run `36238700971`）全绿后才切流。6 个镜像本机 linux/amd64 全量构建，revision/version 标签核对无误；seeded 上传（基座 `20260926-edge-flush-v1`，实传 175 MB）双端校验 PASS；media-edge 镜像单独 scp、校验和核对后导入。发布脚本以仓库版安装（sha256 `c9239d69…`，`PREV_TAG=20260926-edge-flush-v1`），旧版备份 `release-ops.sh.pre-20260926-minor-safety-v1`。
- **五角色**：`verify-load` PASS → `freeze` PASS → `env` PASS（env 键无变化，声纹开关断言与真实 provider smoke 通过）→ `schema` PASS（`services/identity/postgres_schema.sql`、`services/guardian/postgres_schema.sql` 就地更新并执行升级与契约校验；只读确认生产库 `identity_relationship_declared_for_binding` 已接受老人代理认定、`guardian_enqueue_declared_notification` 已去掉只认待确认声明的检查）→ `cutover` PASS（19:37:06–19:37:59，全部 healthy）→ `finish` PASS（readiness `ready` 且为新 tag，定时刷新 `Result=success`，外部 8443 就绪 200）。
- **media-edge（单独切换）**：新链为新发布树 compose + `component-releases/20260926-minor-safety-v1/media-edge-component.override.yml`，新旧渲染配置除 build context 外一致；切换后 healthy、restarts=0。本次无 Go 代码变更，切换只为版本一致。
- **上线后实测**：7 个容器均为新 tag、healthy、restarts=0；readiness `smokes: "passed"`、`warnings: []`；公网设备入口未带凭证 401；bridge 7001 有 1 条来自 media-edge 的已建立连接；control-api、agent、media-edge 启动 5 分钟内无 error/traceback。设备当时空闲，P0-04 的设备行为未观察到。
- **未随整栈变更**：ASR 救援 sidecar 仍是线上原容器（未重建）；P1-02 的两项线上调整（禁用 swap、救援音频上限 12s）未执行，需另行授权。危机推送仍关闭（`MEMORIA_GUARDIAN_PUSH_ENABLED=false`），提醒只在小程序可见。
- **回滚**（未实跑）：`TAG=20260926-minor-safety-v1 COMMIT=048a83a… release-ops.sh rollback` 把 6 个角色按 `20260926-edge-flush-v1` 整栈 compose 重建；schema 不回滚（函数只放宽了判定：旧代码调用新函数仍可工作）。media-edge 回滚改用 `media-edge-rollback.override.yml`。
- **本地制品**：`outputs/release/20260926-minor-safety-v1/` 与 `outputs/release/20260926-minor-safety-media-edge/`（ignored，约 3 GB），验收结束后可删除。

## 2026-09-26 整栈发布 20260926-edge-flush-v1

- **范围**：tag `20260926-edge-flush-v1` → main `fa8a97d`（PR #49 发布脚本跟上线上链、archive 库存储共享连接池；PR #50 media-edge 关闭前送达 `session.error`、Opus 编解码器加锁、readiness 报告刷新逾期）。无 schema、compose、env 变更。合并后 main CI（run `36229760442`）全绿后才切流。6 个镜像本机 linux/amd64 全量构建，revision/version 标签核对无误；seeded 上传（基座 `20260926-persona-subject-v1`，实传 177 MB）双端校验 PASS；media-edge 镜像单独 scp、校验和核对后导入。
- **修复要点（随本次上线）**：① media-edge 三条关闭路径（拒绝 hello、拒绝控制帧、Voice Core 断流）原先把 `session.error` 入队后立即写关闭帧并清空队列，复现 20 次中 19–20 次设备只收到关闭帧或直接 1006；固件只从 `session.error` 判断终止/可重试，于是终止性拒绝被当成断网而续连。现关闭前在 250ms 内先写完 P0 控制帧。② 连接关闭销毁 Opus 编码器时 Voice Core 接收协程可能仍在编码，main 上 `go test -race -count=10` 三轮三次在 `opus_encode` SIGBUS；编解码与销毁加锁后 3×10 轮零崩溃。③ archive DSN 的 10 个存储原先各建一个池（合计上限 90，生产 `max_connections=50`），现共享一个上限 20 的池，临时 PG 启动后连接 10 → 2；行为变化：归档与声纹语句超时 10s → 15s，技能与成长由无超时改为 15s。
- **发布脚本**：首次使用仓库版 `scripts/release_ops.sh`（sha256 `7f68cfad…`，`PREV_TAG=20260926-persona-subject-v1`），旧版备份为 `/root/memoria-release/release-ops.sh.pre-20260926-edge-flush-v1`。
- **五角色**：`verify-load` PASS → `freeze` PASS（新版对 6 个目标容器的整栈 compose 链与 `current` 校验通过；回滚标签、pre-state、env 备份、`pg_dump`、SQLite 备份在 `.cutover/`）→ `env` PASS（env 键无变化；`validate_production`、`verify_env`、agent 与 bridge 声纹开关断言、真实 provider smoke `FunASR, QwenRealtimeSearch, Qwen, Doubao, InterruptSemantic`）→ `schema` PASS（无文件变更，权威库契约校验通过）→ `cutover` PASS（16:41:04–16:42:14，全部 healthy）→ `finish` PASS（readiness `ready` 且为新 tag，定时刷新 `Result=success`，外部 8443 就绪 200，所有带健康检查的容器 healthy、restarts=0）。
- **media-edge（单独切换）**：新链为新发布树 compose + `component-releases/20260926-edge-flush-v1/media-edge-component.override.yml`；新旧渲染配置除 build context 路径外完全一致（设备 WSS 开启、WebRTC 关闭、`python_authoritative`、端口仅 `127.0.0.1:8794`）。切换后 healthy、restarts=0，设备 WSS 监听启动；bridge 7001 上有 1 条已建立连接；公网 `memoria-device-edge` 未带凭证返回 401。
- **上线后实测**：`/health/ready` 带 `smokes: "passed"`、`warnings: []`（P1-09 字段生效）；PostgreSQL 客户端连接 `memoria_app` 为 1 条（发布前 5 条空闲，archive 库 10 个存储共享一个池）；control-api 与 agent 启动 5 分钟内无 error/traceback。设备当时空闲，终止性拒绝的设备行为未观察到，列入真机验收。
- **回滚**（未实跑）：`TAG=20260926-edge-flush-v1 COMMIT=fa8a97d… release-ops.sh rollback` 把 6 个角色按 `20260926-persona-subject-v1` 整栈 compose 重建并把 `current` 指回；回滚目标已按使用人建人格索引，无需人格守卫。media-edge 回滚：同一发布树命令改用 `media-edge-rollback.override.yml`。schema 不回滚。
- **本地制品**：`outputs/release/20260926-edge-flush-v1/` 与 `outputs/release/20260926-edge-flush-media-edge/`（ignored，约 3 GB），验收结束后可删除。

## 2026-09-26 整栈发布 20260926-persona-subject-v1

- **范围**：tag `20260926-persona-subject-v1` → main `63cf5f8`（PR #42 减法整理、#44 发布脚本入库、#45 人格按使用人学习/监护小结随长期记忆/导出含人格/死链路删除）。6 个镜像在本机按 linux/amd64 全量构建（5 个角色 + media-edge），镜像 revision/version/role 标签核对无误；制品清单与源码归档经 `verify_release_source`、`create_release_manifest` 生成，seeded 上传（以 `20260925-full-stack-v1` 为基座，实传 177 MB）双端校验 PASS。发布脚本以仓库版本安装到 `/root/memoria-release/release-ops.sh`（旧版备份为 `release-ops.sh.pre-20260926-persona-subject-v1`）。
- **五角色（`release-ops.sh`，按步 fail-closed）**：`verify-load` PASS（摘要校验、导入前后 verifier、部署冒烟）→ `freeze` PASS（6 个目标容器链与 `current` 全部匹配；回滚标签、pre-state、env 备份、`pg_dump`、SQLite 备份在 `.cutover/`）→ `env` PASS（env 键无变化；候选 `validate_production`、`verify_env`；agent 与 bridge 声纹开关断言通过；真实 provider smoke `FunASR, QwenRealtimeSearch, Qwen, Doubao, InterruptSemantic` PASS）→ `schema` PASS（清单内文件无变化，权威库契约校验通过）→ `cutover` PASS（speaker-model → control-api → agent+bridge → 两个网关，约 60 秒，全部 healthy）→ `finish` PASS（readiness `ready` 且为新 tag，定时刷新 `Result=success`，外部 8443 就绪 200，所有带健康检查的容器 healthy、restarts=0）。
- **人格表迁移（control-api 启动时自动执行）**：三张表 `subject_id` 非空、`FORCE ROW LEVEL SECURITY` 已恢复、现有 5 条特征全部回填为账号本人，新唯一约束 `persona_traits_account_subject_key`、`persona_versions_account_subject_version_key`、`speech_style_stats_pkey(account_id, subject_id, scene)` 就位；control-api 启动日志无错误。
- **media-edge（单独切换）**：镜像校验和与标签核对后导入；新链为新发布树 compose + `component-releases/20260926-persona-subject-v1/media-edge-component.override.yml`，不再包含 `/tmp/media-runtime.override.yml`（原文件仍在主机上，并备份在 `.cutover/`）。渲染配置核对：设备 WSS 开启、WebRTC 关闭、`python_authoritative`、端口仅 `127.0.0.1:8794`。切换后 healthy、restarts=0，设备 WSS 监听启动；公网 `memoria-device-edge` 入口未带凭证返回 401（路由到新 edge）。bridge 重启期间 media-edge 原有 gRPC 通道自动恢复（bridge 7001 上有来自 media-edge 的已建立连接）。设备当时空闲、无会话，设备重连未观察到，列入真机验收。
- **回滚**（未实跑）：五角色 `release-ops.sh rollback`（control-api 回 mascot-sync 组件链，其余回整栈镜像，`current` 指回整栈树）；若已有孩子/老人的生效人格版本，脚本默认拒绝，`ROLLBACK_SUPERSEDE_SUBJECT_PERSONA=1` 先将其标为已取代。回滚后旧代码写入人格会因唯一约束已替换而报错（后台捕获），人格读取不受影响。media-edge 回滚：同一命令改用 `media-edge-rollback.override.yml`。schema 不回滚。
- **真机验收清单（待执行）**：⓪ 先重新绑定设备并勾选长期记忆——2026-09-26 只读核对线上最近三天没有任何 `speech.utterance_finalized`，2336 条证据全部无主体；现有测试绑定早于绑定授权，设备不会把说话人认作绑定使用人，对话不入档，人格、监护小结、按使用人删除都没有数据来源；① 设备唤醒后连上新 media-edge 并完成一轮对话；② 孩子绑定的设备在家长页重开"长期记忆"开关（存量绑定不会自动获得监护小结授予），监护小结出现且只含孩子的聚合记录；③ 隔天孩子的回复体现自己的表达风格，账号本人人格不串入；④ 按孩子导出含人格计数、不含描述；⑤ P0-03 剩余矩阵与 TLS/WSS 重连观察。
- **本地制品**：`outputs/release/20260926-persona-subject-v1/`（ignored，约 3 GB），验收结束后可删除。

## 2026-09-26 减法整理（PR #42，已随 20260926-persona-subject-v1 上线）

- **范围**：main `00bb4b2`（PR #42 squash），净删约 3.09 万行，线上链路行为不变。删除无消费者的多主体 Go/TS/固件生成契约、Go 侧 WebRTC/WHIP 终端与 Go-shadow 会话 actor、Python 侧 shadow 协商与观察流、agent 中只被自身测试引用的 4 个模块；`packages/proto` 未改。删除前只读核对线上：media-edge `MEDIA_EDGE_INTERACTION_AUTHORITY=python_authoritative`、`MEDIA_EDGE_WEBRTC_ENABLED=false`，bridge `MEDIA_BRIDGE_GO_SHADOW_ENABLED=false`。
- **护栏**：行数预算覆盖全部 35 个超过 1,500 行的源模块（只降不升）；`tests/test_service_layering.py` 冻结 `services/` 跨包依赖图。
- **下次 media-edge 发布须知**：新镜像在 `MEDIA_EDGE_WEBRTC_ENABLED=true`、`go_shadow`/`go_authoritative` 或生产未开设备 WSS 时拒绝启动；仓库 compose 已固定 `MEDIA_EDGE_DEVICE_WSS_ENABLED: "true"`，发布后可把 `/tmp/media-runtime.override.yml` 移出 compose 链。`split_production_env.py` 接受但不再分发已退役的 shadow/WebRTC 变量，现有 env 文件无需改动。
- **固件**：补丁 `0025-playback-underrun-metering` 改为 `0026`、吉祥物补丁改为 `0027`，应用顺序不变；overlay 哈希变化，下次构建需重新 bootstrap upstream 缓存。
- **文档**：09-16 至 09-23 的过时章节原样迁入 `docs/HANDOFF-archive-0916-0923.md`。
- **验证**：本地 ruff、strict mypy、行数预算、完整 pytest（无失败）、Go vet/test/race、proto 可复现、媒体冒烟/回放、离线 E2E、小程序与固件主机测试通过；远端 CI 9 项全绿（python 首跑因 buf 下载断连失败，重跑通过）。未部署，未做设备验收。

## 2026-09-25 control-api 发布 20260925-device-mascot-sync（设备屏伙伴同步）

- **范围**：tag `20260925-device-mascot-sync` → main `9f02619`（PR #40 合并提交）。只动 control-api：`services/control_api/*` 内的选伙伴→设备人格同步（`companion_device_sync.py`、`routes/memory.py`、`routes/persona_assignment.py`）与设备签名只读接口 `GET /v1/devices/{id}/display-profile`（`device_display_profile.py`、`routes/device_onboarding.py`）。共享代码 `services/device_fleet`、`services/common` 未改，依赖输入未变，无 schema/nginx 变更。
- **执行**：`deploy_control_component.sh` dry-run PASS → `--cutover` PASS（候选镜像 build + artifact verify、权威 PG schema/RLS 校验、`resolve_target_images` 解析到候选、单容器重建）。基座 `memoria-control-api:20260925-full-stack-v1`（`sha256:de491809…`，revision `064ed61`）。
- **切后**：control-api healthy、restarts=0、StartedAt `2026-09-25T15:54:06Z`；agent、bridge、两个 gateway、media-edge 的 uptime 未变；外部 `https://aigcnice.com:8443/memoria-api/health/ready` 200；新接口无证书请求返回 401 `device_certificate_required`（符合契约）。
- **真机**：开发板（固件含伙伴吉祥物与 display-profile 轮询，见「屏幕：伙伴吉祥物」一节）在发布后首次空闲轮询即从星澜切到账号所选桃喜；复位后开机直接显示桃喜，激活后 1 s 轮询 `Display profile companion=taoxi version=f11c258e317b23d9`，此后每 20 s 一次 HTTPS 200、版本不变不再打日志。
- **小程序**：体验版 `0.2.20260925.3`（main `9f02619`，Node 24.16.0 上传；Node 25 不受 miniprogram-ci 支持）已上传，提交审核/正式发布需在公众平台手动完成。
- **回滚**：切回 `memoria-control-api:rollback-20260925-device-mascot-sync-pre-control`（即 full-stack-v1 镜像）；接口与同步均为增量，旧固件忽略，回滚无数据迁移。未实跑。

## 2026-09-25 整栈发布 20260925-full-stack-v1（保留豆包 TTS）

- **范围**：tag `20260925-full-stack-v1` → main `064ed61`（含 #33–#38：小程序吉祥物与回顾门禁、binding 查询、Runtime Profile 原地续期、确认唯一绑定主体、动作时入口、runbook 修正、Qwen-Audio TTS 回退）。5 个角色镜像（agent、control-api、device-media-gateway、miniprogram-gateway、speaker-model）在本机 linux/amd64 全量构建约 15 分钟；media-edge（代码未变）、postgres、minio、redis、livekit、sensevoice 未重建。
- **发布前**：生产库导出恢复到隔离临时容器演练 main 的升级：升级前 verify 失败（缺新角色，预期）、升级后 verify 通过、重复执行幂等、133→134 表、关键表行数一致，临时容器与导出已删。
- **执行**（`/root/memoria-release/release-ops.sh`，按步 fail-closed）：上传 seeded（实传 423MB）并双端校验 → verifier 导入前后均通过、`smoke_server_deployment.sh` PASS → 冻结（回滚 tag、pre-state、env 备份、`pg_dump`、SQLite 备份，均在 `.cutover/`）→ env：只新增 `MEMORIA_DB_MEMORY_MAINTENANCE_PASSWORD` 与 `MEMORIA_MEMORY_MAINTENANCE_DATABASE_URL`（服务器生成、未输出），control env 身份改为新 tag/commit；候选镜像内 `validate_production`、`verify_env`、真实 provider smoke（`FunASR, QwenRealtimeSearch, Qwen, Doubao, InterruptSemantic`）均 PASS → schema：原地 `cp` 5 个变更文件（备份在 `releases/20260827-architecture-split-v1/.pre-20260925-full-stack-v1-schema-backup/`），升级 + verify PASS，旧 control-api 全程 healthy → 分批切换 speaker-model → control-api → agent+bridge → 两个网关，约 70 秒，全部 healthy、restarts=0 → `current` 切换、`refresh_readiness.sh` PASS、systemd 定时单元 `Result=success`、外部就绪 200。非目标容器 StartedAt 与切前一致；media-edge 在 bridge 重启时断开设备会话，11:30:56 重连新 bridge 成功。
- **小程序复核**（开发者工具 + 生产）：runtime-profile 同会话续期至 epoch 5，`confirmed`/`adult_companion`；首页在线、设备页当前使用者「主人」无降级、回顾无报错；「我的」数字分身/原始语音/声音复刻入口开放，监护小结关闭。体验版 `0.2.20260925.2` 已上传（提交审核/正式发布需在公众平台手动完成）。
- **真机验收（20:31–20:35，Mac 扬声器播放 Tingting 语音代替用户，串口 + bridge 日志取证，收据 `outputs/acceptance/run-20260925-full-stack-device/`）**：设备开串口复位后在新 control-api 上激活成功。首轮问候被丢弃，原因 `no_reply reason=target_non_owner`：生产 `/etc/memoria-agent.env` 仍是 `MEMORIA_SPEAKER_AUTHORITY_ENABLED=true`（P1-11 模板与代码默认都是 false，发布脚本漏查）。已改为 false（原文件在 `.cutover/`），同镜像重建 agent+bridge，healthy、readiness 仍 ready。复测：唤醒应答 → 「你是谁」正常回答（4.7 s，首帧前 4.5 s）→ 南京天气走工具（先确认 2.9 s，realtime search 113 字，正文 21.5 s）→「那明天呢」追问走工具（搜索仅 18 字，正文 3.5 s）→「再见」按 `conversation_end_explicit` 不回复并回到待命，与 09-21/09-24 行为一致。每轮均有 `turn_committed → first_frame_sent → provider_completed → actual_heard → playback_ended`。`sensevoice rescue request failed` 与空文本 `ASR tail timeout` 丢弃在 09-24 旧版同样存在，非本次回归。Mac 音量已恢复（13、静音）。麦克风转写不可用（机器人音量低），只作观察。
- **待办**：① 用户本人听一次豆包声音；② 用户重新绑定设备以写入绑定时 consent grant，再验证 `memory_recall_private`（另需设备证书/attestation 有效）；③ 监护小结 consent 写入方已于 2026-09-26 在代码中补上（随长期记忆授予，见 TODOLIST P1-11，未部署）；④ media-edge 对易失 `/tmp/media-runtime.override.yml` 的依赖已随 2026-09-26 发布解除（新链不含该文件）；⑤ `release-ops.sh` 的两处缺陷（`finish` 状态循环遇无 healthcheck 容器中断、env 步骤不断言声纹开关）已于 2026-09-26 在仓库 `scripts/release_ops.sh` 修复并补回归，服务器副本待下次整栈发布前更新线上链常量后替换；⑥ 旧 20260827 树仍被数据层 bind mount，不可清理。
- **回滚**：`release-ops.sh rollback`（先恢复 env，再按原链重建各服务、`current` 指回 20260827 树）；schema 不回滚（向前兼容）。未实跑。

## 2026-09-25 main 回退 Qwen-Audio TTS 迁移（生产暂留豆包）

- **产品决定（用户 2026-09-25）**：生产暂留 Doubao TTS，Qwen-Audio 3.1 TTS 迁移（PR #28 的 `457d669`、`d6bd536`）回退待重新评估；重新启用 = revert 回退提交。分支 `revert/keep-doubao-tts`（未合并、未发布）。
- **影响**：main 整栈发布不再切换 TTS，Agent/Control 的 TTS 与复刻配置和线上 `3eede2f`/`d522426` 一致，现有 CosyVoice/豆包复刻继续可用、无 `reenrollment_required`；整栈发布仍需 schema 升级与两个 memory maintenance secret（见下节「暂缓的整栈发布」）。

## 2026-09-25 控制面确认一对一绑定的唯一主体上线

- **产品决定（用户 2026-09-25）**：小程序控制会话直接确认一对一绑定的唯一使用人，不再停在 `unconfirmed`/`unknown_safe`。
- **实现**：`sole_bound_subject_id()`（恰好一个 primary subject 且非 `family_shared`）；控制会话 start 与 renew 时确认，事件记 `reason_code=sole_bound_subject`（不冒充 `app_confirm` 或声纹）。确认他人（孩子/老人）须 binding 上有 active 关系（main：`guardian_of`/`delegate_for`；hotfix 线仅 `guardian_of`），并要求与 app 显式确认同样的 subject-switch 权限，否则保持未确认。app 路径不设 `device_bound`：监护人的 app 会话不等于孩子本人，监护人/子女拿不到对方私人记忆。能力仍全部由 policy/consent 决定，无 contract/schema 变更。
- **发布**：tag `20260925-confirm-bound-subject` → `d522426`（hotfix 线 = `85aa729` + 1 提交，control_api+session_runtime 真实 PG 821 passed）；dry-run PASS → cutover PASS，healthy、restarts=0。
- **线上复核**：同一控制会话续期到 epoch 3，`speaker_state=confirmed`、`service_mode=adult_companion`，能力 `chat`/`tutor`/`english_practice`，客户端校验 valid。
- **门禁入口仍不会打开**：`memory_recall_private` 需要已验证成人 + 该 binding 的记忆 consent grant + 设备证书/attestation 有效；hotfix 线没有 main 的 consent grant 写入（`services/consent/bound_subject.py` 与其 SQL），线上无 grant，需整栈发布 + consent schema 升级 + 存量 binding 回填。`guardian_summary_view` 需 active `guardian_of` + 对应 consent，两条线都没有写入方。`digital_self_preview`/`raw_audio_retention`/`voice_profile_create`/`voice_clone_use` 属于动作时决策（`PROFILE_ISSUE_DEFERRED_CAPABILITIES`），永远不出现在 runtime profile，小程序这几个入口按 profile 能力门禁的写法需要改。
- **hotfix 线既有缺陷（未修）**：binding 上存在 receipt 不依赖的 active 关系时，profile 签发 503 `current exact policy fence is invalid`；main 已在 `_PostgresIdentityRelationshipAuthority` 修复，线上当前未命中。

## 2026-09-25 Control API Runtime Profile 原地续期上线

- **缺陷**：binding 查询修好后，线上 runtime-profile 变为 403 `runtime_profile_rejected`。固定控制会话 `device-control:{device_id}` 的 profile TTL 5 分钟，Postgres 控制面没有续期路径（`current()` 过期即 `PersistentSessionDenied`，session_id 是主键不能复用），首个 5 分钟后永久 403。
- **修复**（main 同改动见 `fix/runtime-profile-renewal` PR）：`PostgresSessionRuntimeService.renew()` 复用 subject switch 的 rotation（重锁 binding、重读设备信任、重跑 policy/consent、新签 profile、`session_epoch`+1、`profile_rotated` outbox），CAS 保证并发只产生一个 live profile；过期不携带已确认主体。控制会话改为 `device-control:{device_id}:{binding_id}:{sha256(actor)[:16]}`，换绑后走新会话，旧会话按 superseded 严格拒绝；只有 `device-control:` 会话续期，设备媒体会话保持严格过期。路由拒绝时记录原因并返回固定 reason code（不透出 SQL）。无 schema 变更。
- **发布工具**：`deploy_control_component.sh` 放行 `services/session_runtime/*`（只在 Control 进程内运行，其他镜像不 import），仍拒绝 `services/session_runtime/*.sql`。
- **发布**：tag `20260925-runtime-profile-renewal` → `85aa729`（分支 `hotfix/20260925-runtime-profile-renewal` = `e6ab586` + 3 提交）；本地真实 PG 下 control_api + session_runtime 813 passed；dry-run PASS → cutover PASS，healthy、restarts=0，Agent/Bridge 等未动。
- **线上复核**：带登录读取 runtime-profile 200、客户端校验 valid，epoch 1、TTL 5 分钟；过期后（09:48:08Z）同一 session 续期为 epoch 2、新 TTL，5 秒后复读复用同一 profile。
- **仍开放**：一对一设备的 app 侧 profile 为 `unconfirmed`/`unknown_safe`，只有 `chat`/`english_practice`；数字分身、监护人摘要、隐私等门禁入口因此显示「尚未开放」，需要产品决定是否由控制面直接确认唯一绑定使用人。
- **暂缓的整栈发布**：main 整栈发布会把 TTS 从 Doubao 切到 Qwen-Audio（PR #28，无设备验收），并需要 schema 升级（`guardian_push_subscriptions` 等）与两个新 secret（`MEMORIA_DB_MEMORY_MAINTENANCE_PASSWORD`、`MEMORIA_MEMORY_MAINTENANCE_DATABASE_URL`）；用户 2026-09-25 选择先做 control-api hotfix，整栈待 TTS 设备验收后。

## 2026-09-25 Control API binding 查询 hotfix 上线 + 小程序 0.2.20260925 体验版

- **缺陷**：`PostgresMultiSubjectRuntimeControl.active_manifest` 查 binding 不带 `actor_person_id`；`identity_device_bindings` 按操作账号 FORCE RLS，生产因此对 `GET /v1/devices/{id}/runtime-profile`、resolve-subject、人格分配、memory-scope 一律 404 `binding_not_found`（自 `14b61c0`，2026-08-11）。测试未发现是因为 harness 用 SQLite，真实 PG 路由测试的假 identity 忽略 actor。
- **发布**：tag `20260925-control-binding-actor` → `e6ab586`（分支 `hotfix/20260925-control-binding-actor` = 线上 `ee57ad4` + 仅 `services/control_api/app/multi_subject_runtime.py`）；`deploy_control_component.sh` dry-run PASS → `--cutover` PASS（PG schema/RLS 校验、target 解析、单容器重建），healthy、restarts=0，其余容器未动。回滚点 `memoria-control-api:rollback-20260925-control-binding-actor-pre-control`。main 侧同改动与真实 PG 回归见 PR #33。
- **小程序**：体验版 `0.2.20260925`（1.4 MB）经开发者工具 CLI 上传（CI 密钥 IP 白名单不含当前出口 IP，未改白名单）；含吉祥物表情动画、Runtime Profile v2 校验兼容、回顾/首页摘要/我的统计去掉 `memory_recall_private` 门禁（产品决定：一对一，账号即使用者）。**提交审核与正式发布需在公众平台手动完成。**
- **复核**：binding 查询 404 已消除（随后暴露 403，见上一节）。

## 权威状态

```yaml
schema_version: 2
as_of_date: 2026-09-18
reviewed_source_commit: read_path_pg_parity_and_deletion_saga_d2318e4_ci_35501188784
current_worktree: clean_after_read_path_pg_parity_and_deletion_saga_commit
production_runtime: python_authoritative
production_media: go_media_edge_direct_voice_core
hardware_media_interaction_authority: python_authoritative
hardware_media_target_runtime: go_media_edge_direct_voice_core
hardware_media_rollback_runtime: previous_release_media_edge_direct_voice_core
current_work_order: vocat_interrupt_assist
code: committed_through_d2318e4_read_path_pg_parity_and_pg_deletion_saga
wired: subject_scoped_read_exits_plus_migration_read_paths_and_persona_capsule_subject_read_plus_operator_pg_subject_read
enabled: false_for_current_head
verified: local_and_authoritative_postgres_regression_for_subject_scope_migrations_read_paths_and_receipts_plus_pg_subject_read_parity_and_pg_deletion_saga_plus_ci_35501188784_python_5306_passed
guardian_declaration_scope: binding_scoped_owner_only_third_party_excluded
accountless_person_consent: person_scoped_grant_read_revoke_replay_unbind_revoke_and_export_verified_device_pending
production_readiness: ready_at_last_observation_not_refreshed_this_review
production_readiness_observed_at: 2026-09-16T11:37:57+08:00
student_safety_loop_verified: false
student_safety_local_scope: real_persistent_session_runtime_on_ephemeral_pg_with_sqlite_account_and_declared_guardian_outbox_real_pg_person_consent_gate_real_catalog_subject_key_isolation_and_read_path_binding_fence_closing_the_agent_facing_seams_after_a_legal_manager_change_including_context_prefetch_and_cached_plan_reuse_plus_repeatable_read_snapshot
guardian_authority_evidence: parent_declaration_only_never_verified_link
guardian_declaration_notification_basis: deliberate_identity_declaration_not_consent
postgres_contracts_for_new_paths: scoped_pg17_contracts_passed_including_member_write_read_snapshot_and_history_exit
agent_release_gate_wiring: image_built_and_gate_rerun_offline_as_runtime_user_passing
guardian_notification_delivery_channel: absent_outbox_only_status_pending
firmware_playback_supply_meter_verified: short_playback_software_queue_only_not_dma
pre_roll_code: not_implemented
firmware_face_hardware_verified: false
miniprogram_role: control_plane_only
miniprogram_development_version: 0.8.84
realtime_microphone_allowed: false
realtime_tts_playback_allowed: false
realtime_media_wss_allowed: false
livekit_room_allowed: false
offsite_backup_enabled: false
wal_retention_policy: unresolved_no_automatic_pruning_at_last_observation
direct_real_device_verified: false
full_duplex_verified: false
advertised_duplex_level: none
hardware_aec: pending
hardware_double_talk_matrix: pending_real_hardware
T1_T14: 0_pass_14_blocked_0_failed
device_id: dev_atk_a4cb8fd6095c
```

产品不得宣传全双工。小程序仅 profile 页允许经授权有界录制自定义音色样本，不承担实时对话、手机声纹登记或实时媒体回滚。登录账号、当前使用人、说话人确认和敏感授权不得互相替代。

## 板卡与固件

当前硬件 ESP-VoCat N32R16，`board=memoria-esp-vocat`；ES7210 双麦 + ES8311 输出，36dB 输入增益。hello 声明 simultaneous capture、software_post_gain_pre_i2s reference，但 `aec_reference_verified=false`。旧 ATK ES8388 已退役。

- 板上源码 `d1ad38fe0d9b36eb9057d8eb97e782b8cc8066d9`；upstream `e8d8a4010788afd60f0c8aa3b2e3d0a7bb8f02e5`；ESP-IDF 6.0.2；overlay `24531273fe03bc72980087efbb92314a95c5b25ec68c2e44eb28ae689a1ebbb3`。
- 当前 app 3,280,832 bytes，SHA256 `7d95c8a1c5319f4200f840ea988e448ca7411e508be19ae2a5dd9db0130ddb65`；ELF `da6ebdd16e4e7436ed1e126a94fad69a9e376faca681f474712743b5b55069f2`。2026-09-15 18:51 CST 匹配启动；仅 app-only `0x20000`。
- 证据目录 `outputs/acceptance/run-20260915-p0-03-metering-lifecycle-device/`：`postflash.json` 是不可变刷写收据（其中 `boot_verified=false`）；实际启动独立记录在 `boot-verification.json`，五代短播放与人工听感在 `session-1/audit.json`。不得回改原收据或把短播放软件队列计量称为 DMA/长稳验证。
- 唯一紧邻回滚为该目录 `rollback-app.bin`，3,280,512 bytes，SHA256 `dfba3d619084c6a27b9349b6b230d758238bf289808cefcb848589dca47d581d`；它来自 dirty 构建，以实际二进制为准，不能仅靠旧 HEAD 重建。
- 同目录 `protected/app-before-full-slot.bin` 为写前 `0x20000/0x3f0000` 全槽，SHA256 `cc175040af934575b7804ec01031ebf8543415c98c0084801682e8ce121da333`。身份区 `0x10000..0x1ffff` SHA256 `b7a717fa399ec1390391ca381b9b86c3202035c71695a95e417a4e0f1d084846`；身份/NVS/otadata/bootloader/分区表/assets 均须保护，禁止输出身份内容。
- `pre-roll` 未实现。固件声学、AEC residual、双讲、Exact DAC 与 T1–T14 仍未通过；普通制品清理须在新候选和可运行回滚核验后另行执行，不因精简文档删除实体证据。

## 当前语音缺口与下一验收

原始证据：`outputs/acceptance/run-20260916-p0-03-livekit181-device-acceptance/findings.md` 及同目录五段捕获。八种会话 A–H 均使用上述板卡与 1.8.1 线上版本；原始文件保留，本节纠正其中超出证据的归因。

- H（epoch 1963 / session `de598a18`）同会话完成三天天气→续问→播后告别，但 ACK→正文 `1.969s` 超过 1.5s 门，设备 VAD end→首帧 `4.557s`；A 第二问为 `3.748s`。功能成功不等于时延稳定。
- F 的九天天气 `48.28s`、设备 2413 帧、supply_waits=0，操作员确认完整；C 的 `44.18s` 也完成。B/D 桥侧音频长度 `58.88s/57.46s`，但设备在开始后数秒就停滞，1.5–1.6s 处先有 `366ms/412ms` supply wait，speaking/控制台冻结直到复位。尚无“约 55s 阈值”证据；Edge drop=0 也不能排除 WS writer、网络、接收/解码/播放路径。
- B 在 `20.23s` 挂钟触发旧 TTS 总超时，错误终态又延后约 `38.6s`；D 未见同类 TTS 错误。已有本地部分音频故障注入通过有界 CANCEL_GENERATION/ERROR/权威 listening 断言（见上方软件收据），但设备实际 cancel/flush/退出 speaking 的时序未复验，仍不能解释 B/D 两次停滞。
- 历史 G（epoch 1962）ASR final 先于迟到 VAD，受理时静默预算 0，无新 turn，最终 owner_silence_timeout。静默预算三态与迟到关闭已有本地修复和区分度回归，当前候选未复跑 G 真机，完整 10s/60s 交接矩阵仍待补；不再称修复前复现为当前 HEAD 失败。E/F/H 播后告别成功，C 的迟到告别失败；A 无有效告别输入，G 未到告别步骤，不计算“3/5 成功率”。

下一次设备窗口先确认已冻结并实际启用目标候选；执行顺序及修复前置见 P0-03/P0-04/P1-01：

1. 开串口可能复位，先等 `activating→idle` 和心跳再讲话；唤醒词“茉莉”。无人配合或设备未连接时只做离线检查，不自行刷机或播放自动代测。
2. 同一候选重跑三天天气→续问→播后告别，至少三轮；另测 >45s 长答、B/D 同类长答、临近静默续问，以及已下发部分音频后 provider 失败。每项分别判定功能、时延、终态、听感，不跨 release 累加通过数。
3. 同时取 Bridge/Edge WS writer/设备接收、解码、播放消费及任务/锁状态，绑定 session/stream/turn/generation/tool fence。保留 supply/prestart/boundary/close_dropped/outside、delivery ledger、指标差分、PCM RMS/削波；缺观测先补观测，不先加预缓冲或改阈值。
4. 功能口径（2026-09-17 晚四确定，本阶段只验功能）：能对话、能打断（button/keyword 按签名放行范围，中断后有界退出并回 listening）、长时间对话稳定、每次对话内容汇总可查（双方话轮与汇总以服务端日志/会话记录为准，操作员可读；无可读出口记缺口、不算通过）。不手工注入 profile，不绕声纹/准入门；HTTP 文本正确不是设备已说出，以终端回执与人工听感为准。学生危机场景的设备交付与通知 outbox 链（app_confirm 确认使用人及年龄、固定话术逐字交付、终端回执、outbox 绑定/幂等/家长读取）defer 到安全专项窗口，软件矩阵证据保留；通知发送 worker 按用户决定暂缓。

使用新目录，绑定板上收据（不是本次重新回读的证明）：

```bash
uv run --no-project --with pyserial --with esptool scripts/voice_session_capture.py \
  --out "$CAPTURE_DIR" \
  --firmware-receipt outputs/acceptance/run-20260915-p0-03-metering-lifecycle-device/postflash.json \
  --server-logs --duration 900
uv run python scripts/voice_session_report.py "$CAPTURE_DIR"
```

`--preflight-only` 不触碰设备；`--boot-reset` 仅在明确需要时使用。捕获必须有结束时间、逐路退出原因、无未解释 serial/cleanup error；缺终态不能报 healthy。Actual Heard 需设备终端证据和用户听感共同确认。

## 唤醒词（P2-05）设备证据

- 2026-09-16 原始矩阵 `outputs/acceptance/run-20260916-p2-05-wake-matrix-1/` 保留不改写。Tingting 合成 TTS + 0.25s/0.4s 静音垫可唤醒该板；系统输出静音与部分 voice 空渲染会伪造零召回，工具已有校验。gain 1.0 为 4/8，gain 0.3 为 8/8，合计 12/16；gain 不是测得的近/远距离。
- 首次 activating→idle 后，四次失败刺激分别在约 7.35/17.54/27.71/37.98s，首次成功约 49.59s。单次启动、先高后低的播放顺序不足证明固定 45–50s 预热，后 12/12 是事后子集；60s 默认等待仅实验参数。成功刺激起点→唤醒行中位 1.445s 含前导静音与采集时序，不是精确声学延迟。
- 去重后新唤醒事件为 0，原 TV=1 为上一试次滞后重复；但 TV 133.038s 内 idle=0，约 92.382s 为 speaking/KWS off；small_talk 124.471s 内 idle≈100.909s，quiet 300.001s 均为日志可见 idle。不能据此报电视/多人各两分钟有效零误唤醒，配置日志也不自动证明整窗 detector 持续启用。
- `092dcf4` 已有门控/去重初修，仍存在本轮复现的曝光高估与 fixture/CI 缺口（见 P2-05）。先修工具再按多次冷启动、随机/交错增益、真实音源/物理距离预定义协议采数；本轮无设备新数据，不改 `advertised_duplex_level` / `aec_reference_verified`。

## 屏幕：伙伴吉祥物（替换白描对话脸，2026-09-25）

- **状态**：`code=done`（overlay 新增 `memoria_mascot_{pack,scene,display}`、patch `0027`（原编号 0026，2026-09-26 为消除 0025 重号顺延）、`memoria_display_hooks`，白描脸源文件与测试已删除）；`wired=开发板已刷`（app `0x20000` + assets `0x800000`，identity/nvs/otadata 未动；刷前回读备份在 `firmware/esp32/artifacts/backups/pre-mascot-20260925/device-readback/`，stub 读 `0x322000` 起会断，需 `--no-stub`）；`enabled=true`（control-api `20260925-device-mascot-sync` 已上线，真机首轮轮询已切换伙伴）；`hardware_verified=false`（串口只证明启动、解码 139 ms、空闲约 240 重绘/分钟、每次约 8 ms 纯合成；画面需亲眼确认）。
- **手机同步链路**：小程序保存 `companion_id` → control-api 为该账号作为 owner/admin 的每个 active binding 的 primary subject 写 persona assignment（幂等、失败只记日志、`next_session` 投影，不打断进行中的回复）→ 设备空闲时每 20 s 签名 `GET /v1/devices/{id}/display-profile`（与 activation-manifest 同签名对象，仅 path 不同；409=未绑定）→ `display_version` 变化即换装并写 NVS `memoria_ui/companion`。已随 control-api `20260925-device-mascot-sync` 上线（无 schema/nginx 变更）。
- **注意**：「我的」页与伙伴页每次保存都带 `companion_id`，会把设备页对主使用人单独分配的人格（含自建人格）改回账号伙伴（一对一产品下符合"选TA陪伴为准"）；绑定页选的伙伴自 2026-09-28 起也写成账号伙伴。若要只在值变化时同步，改 `companion_device_sync.py` 一处即可。
- **构建**：`common.sh` 现导出 `IDF_COMPONENT_CHECK_NEW_VERSION=0`；否则组件管理器读取 registry 最新 esp_video 的 esp_h264 规则，CMake 连跑两次后报 `Missing required kconfig option after retry`。全新 clone + `apply-overlay.sh`（0001–0026 全部干净应用）+ `build.sh` + `check-overlay.sh` 已通过；app `0x325850`（剩 20%），assets 5.8 MB / 8 MB。

| 看什么 | 期望 |
| --- | --- |
| 开机 | 黑屏→暖光球→光圈展开→`memoria` 字标→伙伴弹入落地→眨眼→挥手；无白屏闪烁 |
| 待机 | 呼吸、2.4–5.6 s 眨眼、20–38 s 一次小动作；3 分钟后瞌睡且背光变暗 |
| 拍一下 / 摇晃 | 开心跳 / 晕乎乎；都不开麦 |
| 唤醒后 | 聆听姿势 + 光环呼吸；说完后思考姿势 + 彗星光环 |
| 说话 | 服务端心情对应姿势 + 口型开合；结束后心情停约 2 s |
| 未绑定 | 白色圆角二维码卡片可扫、上方「你好，我是星澜」 |
| 小程序换伙伴 | 空闲 ≤20 s 内挥手换装，下一轮对话换声音；重启后保持 |

以同代 `assistant_expression→screen.expression` 和串口 `MemoriaMascot: emotion=` 为准。照片存入本次 ignored 验收目录，全部亲眼确认后才签收 `hardware_verified`；回滚 = 用回读备份写回 `0x20000` 与 `0x800000`。

## 永久运维参考与历史归档

永久运维材料已从会话流水中拆出，主交接只保留入口和变更边界：

- [发布、恢复与回滚运维手册](docs/runbooks/release-rollback.md)：生产拓扑、安全边界、发布前门禁、制品上传、数据恢复和回滚验收底线。
- [空间治理运维基线](docs/runbooks/operations-space-governance.md)：磁盘巡检、Docker 镜像保留、验收归档和 systemd 基线。
- [删除域与 seal 契约](docs/compliance/delete-domains.md)：2026-09-20 删除范围结论、17 表约束、已知合规缺口和不可归属面。
- [2026-09-20 及更早历史归档](docs/HANDOFF-archive-before-0920.md)：已移出的发布、设备窗口、F2 和待命复现证据；删除/seal 原文在上面的合规文档中保留。
- [2026-09-16 至 2026-09-23 历史归档](docs/HANDOFF-archive-0916-0923.md)：09-18 审查基线与候选边界、截至 09-16 的生产收据，以及 09-21 至 09-23 的发布、评测和设备窗口节；均已被 09-25 整栈发布取代。

后续永久运维规则只更新上述 runbook；会话证据继续按日期追加到本文件。

## 2026-09-24 缺陷 A 真实设备复测（核心边界通过，P0-03 未关闭）

- **候选与采集**：真实设备采集窗口为 `2026-09-24 11:45:34.122697`–`12:15:34.489031` CST，持续 1800s；服务端候选为 `memoria-agent:20260921-defect-a-followup-endpoint` / commit `2a33a50e85d09dc61944ac860e311d247a1020e2`。本轮未刷机、未向串口写数据、未重启服务；固件版本未从板上重新读取。
- **核心续问判定：通过**：同一主会话中 3/5/8s 停顿的“后天呢？”分别形成独立 `turn_id=3/4/5`，均有非空 boundary/endpoint、`actual_heard=true`、`playback_ended=true`；未见旧的回声与追问合并。逐格 boundary/endpoint 与日志链接见[完整收据](docs/acceptance/run-20260924-defect-a-retest-live/findings.md)。
- **长稳判定：基础通过，带连接观察项**：约第 10、12、14 分钟的三次人工唤醒均独立应答并回到 idle；无二次复位、panic、服务重启或 uptime reset。但期间多次出现 TLS/WebSocket 断开并自动重连，重连频率、期间体验和根因仍待收敛。
- **未完成边界**：“今天适合散步吗？”只完成了“我稍等，查询一下”的首段播放，最终工具查询回答在首帧前被 `conversation_end_explicit` / `output_task_cancelled` 取消；>45s/B/D 长答、部分下发失败、待机/表情及点屏/摇晃/短拍/BOOT 回归仍未覆盖。BMI2 I2C 超时为独立硬件观察项，不归因于缺陷 A。
- **状态**：本轮将缺陷 A 从“待真机复测”推进为“核心续问边界真实设备证据通过”，但 `P0-03` 保持 `[ ]`；`direct_real_device_verified=false`、`full_duplex_verified=false` 不变。
