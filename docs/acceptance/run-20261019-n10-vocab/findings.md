# 2026-10-19 N-10 停止词热词表上线与首轮真机验证（未达标）

用户 10-19 授权「按方案一来，你帮我到服务器上设置」「机器可以通过 USB wake 唤醒」，并确认「允许」改生产 env（[DECISIONS #018](../../../share/DECISIONS.md)）。本轮不宣称 N-10 / N-8 / N-5 关闭，不改三个全局 verified 标志。

## 结果摘要

| 项 | 值 |
|---|---|
| 热词表 | `fun-asr-realtime`，词条「停」权重 5、「别说了」权重 5，状态 OK |
| 生产改动 | `/etc/memoria-agent.env` 增加 `FUNASR_VOCABULARY_ID`，只重建 bridge（10-19 09:24 CST），release tag 仍为 `20261016-readiness-b54189c` |
| 场景 | 「给我讲一个故事吧→别说了」与「介绍一下大熊猫→停」各 5 次，共 10 步，既有 Doubao 语音库素材，Mac 音量 30，USB `wake` |
| 有回复 | 8 / 10（t001 唤醒后第一句与 t009 无回复） |
| 打断素材送出 | 7 次（t002、t004、t005、t006、t007、t008、t010）；t003 回复 2.87 s 就播完，没来得及打断 |
| **真正停下** | **0 / 7**（`voice_soak_evidence.py` 重算，七条都是 `not_stopped / playback_completed`） |
| bridge 的停止判定 | 全程 `stop_word=True` 只出现 1 次，且是 `no_reply_in_flight`（回复已播完）；播放期间的终稿都是 `stop_word=False` / `not_stop_word`；0 次 `media spoken stop interrupted reply` |
| 其他 | t004 说完到开口 26.07 s（单次离群，未查）；串口 `Encode queue is full` 0 行；E/W 级串口日志 16 行（未逐条看） |

原始证据在本机 gitignored `outputs/acceptance/run-20261019-n10-vocab/` 与 `outputs/serial/robot-20261019-n10-vocab.log`。

## 目标与结论

N-10 的验收口径是真机「停」识别率 ≥ 80%（10 次）。本轮 0 / 7，**未达标**。

**这一轮本来就分不出热词有没有用。** TODOLIST N-10 已记着：音量 30 下口头停止历史上就是 1 / 24（放大 2 倍 1 / 12），音量 50 才有 8 / 10（第十一轮）和 25 / 46 = 54 %（第十八轮）。音量 30 的基线接近 0，0 / 7 与它一致，改善没有空间显现。要看热词的效应，须在音量 50 下和上述基线比。我在跑之前没先对照这条记录，是我的疏漏；当时按 memory 的规矩没有自己调高扬声器音量。

不能由本轮得出的结论：热词表是否被 ASR 实际采用（日志不记识别文本，也不记 vocabulary id），所以「热词无效」与「热词有效但被别的因素盖住」在本轮证据里分不开。可以支持的推断是：10-13 三窗采集已量到双讲时停止词在 post-AEC 上行里与只播假峰同级（NCC ≈ 0.18 / 0.21），[复测收据](../run-20261013-triwindow-retest/findings.md)；这与本轮「播放期间终稿里没有停止词」一致，但本轮没有重新采 PCM，仍是推断。

## 过程中纠正的三个错误（都是我这边的）

1. **变量名**：我先把热词表 ID 写进了自己起名的 `MEMORIA_SPEECH_STOP_WORD_VOCABULARY_ID`，代码实际读 `FUNASR_VOCABULARY_ID`（`services/agent/src/providers/funasr_stt.py:95`）。已删掉误加的行，改成正确变量名；改前备份在服务器 `/etc/memoria-agent.env.bak-n10-20261019`（root 0600）。
2. **目标模型**：第一张热词表建在 `qwen-audio-3.1-asr-flash-streaming`，而生产 `FUNASR_MODEL=fun-asr-realtime`，热词表绑定模型，换不了。已新建绑定 `fun-asr-realtime` 的一张，并删除第一张。
3. **冒烟的覆盖范围**：切换前在隔离容器里跑 `provider_smoke_test` 全部 PASS，但该脚本自己构造 `FunASRConfig`，不读 `FUNASR_VOCABULARY_ID`，所以这个 PASS 不能证明热词表可用；热词表只在真实 bridge 里被用到。

## 回滚

`sudo cp -p /etc/memoria-agent.env.bak-n10-20261019 /etc/memoria-agent.env`，再按 [发布与回滚手册](../../runbooks/release-rollback.md) 的 env 变更方式重建 bridge（`sudo env MEMORIA_RELEASE_TAG=20261016-readiness-b54189c MEMORIA_RELEASE_COMMIT=b54189c5f13d240d24dbb10a6b9e604fb36b96d1 docker compose -f docker-compose.production.yml --profile media-runtime up -d --no-deps --no-build --force-recreate voice-core-media-bridge`，在线上发布树 `/opt/memoria/current` 里执行）。重建会切断一条空闲的设备 WSS，先确认 `media_active_sessions 0.0`。

## 条件与限制

- 设备 MAC `90:e5:b1:d7:83:2c`，串口启动记录 `MEMORIA_FIRMWARE_BUILD=25; slot=ota_0`，激活清单 v8，companion `starlight`。**HANDOFF 之前只登记到 build 24**，build 25 的来历本轮没有核对。
- 一台设备、一个房间、合成音；Mac 开测前音量 13 且静音，测试 30，驱动恢复了音量但没有恢复静音，已手动补回静音。
- 没有在音量 50 下复测：停止在音量 30 下本来就最难评估，这一轮只能算「首轮观察」，不是验收。
- 热词对正常对话的副作用（单字「停」权重 5 可能把孩子说的别的话识别成停）本轮没有量。
- 脚本 `scripts/create_stop_word_vocabulary.py`（`--model`、`--delete`）本轮新增，尚未提交。
