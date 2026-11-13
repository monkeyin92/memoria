# 对话修复与网络提示音部署记录

2026-11-13。目标设备 `dev_atk_a4cb8fd6095c`，MAC `90:e5:b1:d7:83:2c`。

## 服务端

发布候选 `20261113-dialogue-hotfix-v1`，提交 `9efcf9d7feb0d9abd219a6cb0f567c9ed9d6c2b1`。基于线上 `ad40fe60`，仅更新本次 agent 修复、测试和行预算。

修复要求真实 ASR final 才可结束会话。缺 final 的告别 partial 不提升为 final。分类器异常记录 warning 并等待真实 final。联网故事使用白名单 `LIVE_LOOKUP_FILLER`，随后继续最终回答。

独立 Claude 评审无阻塞。生产基线完整测试 4083 passed、61 skipped、2 warnings；追加分类器异常回归及 endpoint 测试 49 passed。Ruff、33 项行预算和 213 文件 mypy 通过。发布脚本再次执行门禁，未跳过。

## 设备 assets

仅写 assets，保留 app build33。原 assets 为 8 MiB，备份 MD5 `7840e4bce899b8eaacc10f9bc9a29008`，与芯片一致。身份备份被 git 忽略并设为 0600。

三份网络重连与恢复提示通过生产配置实际重合成，使用 `warm_companion`、`zh_female_shaoergushi_uranus_bigtts`、`seed-tts-2.0`。独立评审确认音频包改变、标签与编码正确、包内其余 35 个 entry 不变。

原候选 2,493,448 bytes，SHA-256 `f8c78f641527a684e735460ea286ff918b64ca0aff1dc3c5783a1cba18fd1d5a`。按 4 KiB 补齐后 2,494,464 bytes，MD5 `cdbaf38015dcd7ed26fcaedbe61f2ee2`，写入 `0x800000`，末端 `0xa61000`。esptool 写入哈希通过。重启前非 assets 各分区 MD5 已与写前逐一匹配。独立读回 2,494,464 bytes 与候选逐字节一致，未写尾部的芯片 MD5 与旧备份尾部相同。所有非 assets 分区均未改变。读回遇到三个 64 KiB 传输块错误，既有读取脚本以 2 KiB 重试成功，最终完整比对通过。

备份、读回和校验脚本位于被 git 忽略的 `firmware/esp32/artifacts/network-prompts-20261113/`。串口日志位于本目录 `serial.log`，不入版本控制。

## 验收边界

设备于 22:12 重新启动，assets 校验和检查通过，22:13:03 串口确认 `MEMORIA_FIRMWARE_BUILD=33; slot=ota_1`。服务端切流与真机联网、final 告别结果见下节。未做人工听音验收，不将文件或传输哈希视为声学验收。


## 部署与实际链路结果

22:15:31，正式组件脚本完成切流。线上 `memoria-agent:20261113-dialogue-hotfix-v1`，revision `9efcf9d7feb0d9abd219a6cb0f567c9ed9d6c2b1`，healthy，restart 0。三份修复源码 SHA-256 与本 worktree 一致。回滚镜像 `memoria-agent:rollback-20261113-dialogue-hotfix-v1-pre` 保留，image ID 为 `sha256:14a9d4a6ca4b9298b12526867fe1c4a69e426316a2cb7cba95a99e4d5f39b522`。部署前核对运行容器的 services 和 packages 源码与 `ad40fe60` 一致，无手工源码漂移。Control API、Edge 和数据服务未切换。

真机 session `154ab015-e824-43f0-9a01-79aedec0cb9a`、stream epoch 3192。串口持续监控，确认麦克风开启、监听恢复后，以 Mac 扬声器播放“给我讲一个孙悟空的故事”。DeepSeek search HTTP 200，5 个结果。generation 2 过渡语和 generation 3 最终回答均有 `actual_heard=True` 及 `playback_ended=True` 回执。没有原白名单异常。随后播放“好的，再见”，22:16:49 的 ASR `source=final rule_match=True` 正常走 `conversation_end_explicit`，设备回到 wifi_hold。partial 修订场景仍以回归测试验证，没有伪称通过自然语音复现。

本次未修复卡顿。generation 3 的服务端 pacing 为 1009 frames、20180 ms、max_gap 26 ms；设备 output 969 frames，解码队列 depth 120 时丢包 40 条，supply_waits 7、supply_max 1944 ms、supply_total 3150 ms。设备播放队列供音不足和解码队列溢出已证实，尚未区分最后一跳网络突发与设备调度原因。播放回执不能证明无卡顿。三种本地网络提示的真机逐项听感仍未验收。
