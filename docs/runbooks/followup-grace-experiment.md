# 播放后续问宽限实验（TODOLIST N-14 第 3 档）

## 要回答的问题

机器人说完一段话之后，孩子的下一句没有设备 VAD 边沿，服务端只能靠 ASR 终稿定端点：终稿到达后固定再等 **1.2 s**（`media_session_playback_stop.py` 的 `_PLAYBACK_FOLLOWUP_ENDPOINT_GRACE_S`），其间没有新的终稿才提交。把它改短能让每一次这样的后续问早开口 0.1–0.9 s；代价是孩子句子中间的停顿可能把一句话切成两次提交。**判据**：0.6 s 及更短的停顿上，切分率不比 1.2 s 基线高，才可以把默认值改短。

读代码得到的预期（实验要验证的假设）：这条路径只在「同一句话产生了两条终稿」时才有差别——停顿短于 FunASR 自己的断句阈值时只有一条终稿，宽限无关；停顿更长时第一条终稿到达后要等第二条**终稿**（说完才到，不是部分结果）在宽限内到达，第二半句稍长就赶不上 1.2 s，所以真正受宽限影响的是**第二半句很短**的句子（「……吗」「……呀」）。因此句子里要有短后半句，也要有长后半句。

## 旋钮

`MEDIA_PLAYBACK_FOLLOWUP_GRACE_S`（bridge 的环境变量，0.3–1.2 s，只能比默认短；不设、写错、超出范围都是 1.2 s，并记一条警告日志）。默认行为不变；旋钮随下一次整栈发布上线。

## 步骤

1. 准备句子：`sentences.json`，每条 `{"tag","a","b"}`，建议 10 句（半数后半句 ≤ 4 个字）。
2. 渲染两半：`voice_soak_pause_split.py items` → `voice_soak_bank.py render`（生产豆包音色）。
3. 拼接静音与场景：`voice_soak_pause_split.py clips --pauses 0,0.3,0.6,0.9,1.2 --repeat 10`（0 秒是不切的对照；每轮每个停顿长度各一次，句子逐轮错位，不会有某句总配同一个停顿）。
4. 真机一轮（**需要用户放开**：Mac 音量 50、USB 唤醒、先起常驻串口记录进程，见 `docs/acceptance/run-20261001-longsoak/findings.md` 的方法一节）：先在 1.2 s（不设旋钮）上跑基线，再把旋钮设成候选值（建议 0.7）重跑同一份场景。改旋钮是生产环境变更：写 `/etc/memoria-agent.env`（先备份）并重启 bridge，须用户当场授权；做完把变量去掉再重启即回到默认。
5. 看数：`voice_soak_pause_split.py report --run <run 目录>`，每个停顿长度的「一次提交 / 两次及以上」与说完到开口的中位数；另看 `media playback-followup endpoint`、`media pending turn split` 的日志。
6. 看宽限到底省了多少：每次提交有一行 `media turn commit timing`，`final_to_start_ms` 是「最后一个终稿 → 提交开始」，改旋钮后它应当约少 1.2 s 与候选值之差；`python scripts/voice_commit_timing.py <bridge.log>` 汇总成 p50 / p90（含两种云端分类器调用的耗时；提交有没有等结束对话判定看 `close_ms`，`classifier verdict wait` 行也含终稿时的背景调用，不能用来数）。缩短宽限要在 TODOLIST N-14 ⑦（结束对话判定在终稿时起跑）上线之后做，否则宽限短于分类器的 p90 0.82 s 时，提交又要等分类器，会低估宽限的收益。这几行日志不含原文。

## 边界

单设备、Mac 合成音、安静房间；停顿是拼接的数字静音，真实孩子的停顿会带呼吸和语气词。数字只用来决定「改短会不会明显多切」，不是对孩子真实说话习惯的统计。
