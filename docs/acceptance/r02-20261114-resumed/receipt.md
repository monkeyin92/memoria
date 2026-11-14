# build36 安静窗口恢复验收（2026-11-14）

**结论：闭窗预检与Safety通过，完整Soak停止检查失败；正式R02的155步均未启动。可听卡顿仍未验收。** 结构化结果见[summary.json](summary.json)。

## 候选与范围

生产始终为memoria-agent:20261114-stutter-v2，revision d43fe7f1762b4f84445de66c9729d6dc099cf67f，image sha256:ec877b61cceb8019a38a1d5e4ab3672c03daab7800ef1767cc763bc053359172；前后实测healthy/restarts0、reply_debug=false。目标dev_atk_a4cb8fd6095c，MAC 90:e5:b1:d7:83:2c，USB /dev/cu.usbmodem1101；实际启动build36/ota_0、activating→idle。没有生产配置、镜像或固件变更。

389条素材映射SHA-256复核通过，不当作389次独立合成。预先冻结Safety20、完整Soak45及R02 A/B/C/D=80/60/10/5，主刺激及打断音频均存在。使用本worktree的devicetest/voice_soak_tapwake.py，单一串口owner，每次刺激前检查监听、开麦及屏幕状态日志；没有使用硬编码旧checkout/build28/镜像或截断Soak的wrapper。测试output30，原完整音量13/94/100/mutedfalse。

## 门禁结果

| 阶段 | 实际结果 | 验证边界 |
| --- | --- | --- |
| 40秒短预检 | 2/2主刺激有播报，尾窗Idle | 空ASR兜底重启监听窗口，没有证明closed→resume，不算完整预检PASS。 |
| 80秒闭窗复检 | PASS；13:20:56.855 closed=true，13:21:46.940 resume_listening；2/2播报，45秒尾窗Idle | 本段安静间隔没有额外播报；未改capture30秒、owner180秒阈值。 |
| Safety20 | SAFETY=PASS；四类各5/5，0 no_reply | 固定模板计数1/9/5/5；210秒纯尾窗内13:34:29.138真实Idle。不是人工逐字听音认证。 |
| 完整Soak45 | **FAIL**；44个主刺激均有reply，0 no_reply | 另4次停止/内容打断中两次纯停止失败、两次内容打断成功；1次附和正确继续播放。 |
| Soak静默及尾窗 | 185.4秒静默步真实Idle于13:51:44.751；最后3步均有播报；告别后13:52:39.860真实Idle | 全45步完成，deadline未到；240秒纯尾窗verified_idle，没有插入额外告别或reset。 |
| 正式R02 | **0/155，前置阻塞，未执行** | 四组仅冻结，不沿用旧候选PASS。 |

40秒预检的额外播报有ASR tail timeout / empty+vendor_silent证据，此前有VAD却无识别文本；不能直接认定背景人声、ASR幻听或孩子输入。旧样本保留，另做80秒有效复检，见[precheck-capture-gate.json](precheck-capture-gate.json)。

Soak严格判定失败项为两次stop_not_confirmed及P90 2.23秒/max4.58秒超原时延线。按当前Card5既有“时延只记录”政策重算，**仍因两次停止失败而FAIL**。原负延迟−0.03秒没有钳制；播放进程起止不等同声学音素边界。严格、policy、keepawake三份verdict均exit1；driver exit0仅表示场景和尾窗完成。keepawake的screen-off标记不能单独证明真实Idle，本报告另核实际StateMachine转换。

## 停止失败及同波形对照

| 步骤 | 离线最终证据 | 结论 |
| --- | --- | --- |
| t012-story-stop，脚本“别说了。” | 同delivery自然playback_completed；刺激开始仍有43.171秒音频；后端final为“别说呢。”、stop_word=False | not_stopped。只证明预设意图与后端文本不同；源TTS发音及具体音频处理层根因未定。 |
| t016-long-stop，脚本“停。” | 13:41:06.695–08.389刺激，旧答仍有10.287秒；窗口仍vad.idle，未见开段或可关联final；最后自然完成 | not_stopped。日志缺项不等于识别器内部没有任何结果。 |
| t017/t019内容打断 | 同delivery取消及设备停播确认 | 两次stopped；语音结束至停播0.962/1.474秒，不充当正式C组或精确声学验收。 |
| t018附和 | still_speaking_after_interrupt=true，原答自然结束 | 本例保持播放符合预期。 |

t012/t016在线先报unverified/missing_delivery_terminal；真实terminal落盘后才离线判定not_stopped/playback_completed。没有改写原timeline，最终审计见[soak-interruptions-finalized.json](soak-interruptions-finalized.json)。输入窗口按epoch裁取，避免混用13:40与13:41。

完整Soak后另做不计门禁的near-only诊断，原失败波形逐字节复制，音量仍30。机器不播报且新鲜监听时，第一条“别说了”命中stop_word=True、no_reply_in_flight及interrupt_command_only忽略控制，无回复是正常行为。第二条“停”有VAD，随后提交text_len=2的普通轮次并生成105帧/2.10秒回答；**实际识别原文未记录，尚不能确定识别错还是后续判定差异**。同版词法函数对字面“停/停。”均为True，未扩词表或盲调阈值。不能把两次失败统一归因于双讲回声。见[near-only-result.json](near-only-result.json)、[同波形哈希](near-only-control-bank.json)。真实告别后13:58:24.088 Idle，45秒尾窗通过，不混入45或155分母。

## 卡顿复测

长答session 35191016-081e-4958-a66c-75b39bcb35de、stream3203：太阳系2797/2797帧、55.94秒，服务端maxgap27ms，设备一次76ms软件供音等待；恐龙2993/2993帧、59.86秒，maxgap33ms，设备153/49ms两次等待、累计202ms。整组decode drops=0，问答和告别有播报，45秒尾窗Idle。用户回复“没留意，无法判断”，故**无丢帧不等于无可听卡顿**。

完整Soak另见更长等待：13:42:44.493，session 2b4b65a9-7980-47ac-a868-841daa48c8ed、turn23/gen26软件队列等待583ms；该代服务端467帧/9340ms、maxgap39ms，设备467帧且自然完成，未发生decode drops。整组服务端最大间隔196ms属于其他代，不能串错成这次583ms的来源。稳定发送不能证明设备端无停顿；这次证据不支持归因于原先866ms同流文本断供，发送点之后的网络、接收、解码、调度及等待记账仍待区分，见[Soak播放汇总](soak-playback-summary.json)。

独立只读源码核对：first_output_latency包含generation公告后的取包、解码、调度、预缓冲和首次codec输出，并非纯预缓冲时长；0049仅首PCM阶段最多320ms/16项，不消除播放中断供。exact_confirmed多次可能是耗尽恢复后的重复watermark确认，不是丢帧证据。软件等待包含唤醒及重新取锁，日志位于恢复提交后，没有帧序号、队深或精确起止；尚不能区分网络最后一跳、收包、解码或调度，也没有I2S/DMA underrun证据。

## 录音、内容审查与证据缺口

四份现场WAV为outputs/acceptance/r02-resumed-20261114-{room,gates-room,soak-room,near-room}.wav，相关run有session_mic.wav及来源清单；均为Mac房间录音，非设备PCM。WAV样本时长与ffmpeg最终PTS不一致，例如Soak为1200.878与1282.35秒。**没有包时间戳，间隙位置和原因未定，不能认定连续或逐字对齐声学证据**，见[audio-capture-alignment.json](audio-capture-alignment.json)。Flow返回FLOW_MCP_READ_ONLY=1，没有转写或绕过限制。预设刺激不冒充实际识别文本。

只读刷新Notion R02/Card5当前原文：0.8秒保留严格reducer输出但仅记录，不自动豁免C组1/2秒；三次打断后追问单列、不混入155。11-13更正要求“取消信号/设备停播/按实际播放更新上下文”三段独立收据，不能由总计替代。生产d43fe7f1仅正常完成路径记录debug累计文本，取消分支无emitted快照；approximate回执不能推进逐字Actual Heard，仅打开debug不能补齐第三段。实际听到内容、上下文更新及本轮回答逐字审查仍未验证。没有回写Notion、开启生产debug或发布本地内容候选d954dc8f。

## 清理与复核

设备最后真实Idle；全部本次driver、ffmpeg及logger已结束，USB无owner、命令socket已删除，Mac恢复13/94/100/mutedfalse，见[cleanup.json](cleanup.json)。只读bench status被生产非bench镜像忽略、exit1，不冒充状态回执、没有为此重刷；Idle依据仍为实际serial转换。生产最终回读见[final-production.json](final-production.json)。时钟pre offset/error=0.0236/0.0366秒，post=0.0380/0.0443秒。

原始run在outputs/acceptance/run-20261114-resumed-{precheck,precheck-capture,stutter,safety,soak,near-only}。日志和WAV不进Git，manifest、结果和哈希在本目录。离线复核：

~~~sh
PY=/Users/monkeyin/projects/memoria/.venv/bin/python
RUN=outputs/acceptance/run-20261114-resumed-soak
$PY scripts/voice_soak_evidence.py "$RUN"
$PY scripts/voice_soak_verdict.py "$RUN"
$PY scripts/voice_soak_verdict.py "$RUN" --reply-latency-record-only
$PY devicetest/keepawake_verdict.py "$RUN" --serial "$RUN/serial.log" --gate-silence --reply-latency-record-only
~~~

下一步先取得失败短词的实际ASR原文与可对齐输入证据，再按根因修复并重跑同候选前置；本轮不继续正式R02。
