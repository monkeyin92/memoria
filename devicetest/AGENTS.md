# AGENTS.md — devicetest

## OVERVIEW
真机验收测试：拍唤醒 soak、安全卡、R02 停词/回插话门禁的驱动、评分与报表。14 个 Python/bash 源文件 + 3 个场景 JSON；目录里其余 800+ 文件是运行产物（日志/音频/转储）。得分 6，低于 8 但为独立知识域（root 只有指针，无处覆盖），故创建。

## WHERE TO LOOK
| 任务 | 位置 |
|------|------|
| 主驱动（串口/日志观察/唤醒门禁） | `voice_soak_tapwake.py` (813 L) |
| R02 门禁（stop/backchannel/内容/危机） | `r02_gates.py` |
| 安全 soak 评分与切条 | `safety_soak_report.py` / `safety_card5_score.py` |
| 每 turn 延迟拆分报表 | `tapwake_report.py` |
| 场景数据 | `scenarios/child_tapwake.json` / `safety_tiers_5x.json` / `stop_focus.json` |
| 一键卡 5 包装 | `precheck_card5.sh` / `run_soak_card5.sh` / `run_safety_card5.sh` / `run_r02_card5.sh` |

## CONVENTIONS
- 双入口：`python3 devicetest/<mod>.py`（shell 脚本调用）与 `python -m devicetest.<mod>`（评分/门禁）；`sys.path.insert` 到 `scripts/` 后 import，`# noqa: E402`；repo root 为 CWD。
- 场景 schema：顶层 JSON 数组（`run_soak_card5.sh` 兼容 `{"steps":[...]}`）；step 字段 `tag/say/rate/interrupt{after,say,kind}/expect/wait_state/gap/end_timeout`；tag 变 turn id `t{NNN}-{tag}`，重复 tag 允许。
- 门禁默认值：pre 门 idle-only 180s、turn 门 30s（卡 5 传 60s）、post-scenario 等待 210s；阈值常量钉在代码里（`STOP_P90_LIMIT_S=0.8` 等），不在配置。
- 运行目录 `devicetest/<kind>-YYYYmmdd-HHMMSS[-tag]`；`safety-latest-dir.txt` 指向最近一次安全 run；`--out` 用 `exist_ok=False`，包装脚本不得预建目录。
- 固定词表来自 Notion R02 卡与门禁页（`STOP_WORDS` 8x10 / `BACKCHANNEL_WORDS` 6x10 / `CRISIS_LINES` 5），不许替换。

## ANTI-PATTERNS
- 串口独占：`voice_soak_serial_logger.py` 是唯一可开机器人 USB 口的进程，driver 只 tail `--serial-log`；`open_port()` 永不 reset 板子——依赖 `activating -> idle` 的场景必须由包装脚本先显式 reset。
- 唤醒只走 USB `wake` 命令，"Never by playing the wake word"；连续拒唤醒不得强制整段 idle。
- 数据诚实：缺的点留 `缺` 永不估算；不可评分的打断（`interrupt_too_late_to_stop` 等）只报告、不计 pass；clip 切不出写 `.missing` 文件，绝不静默跳过。
- `run_safety_card5.sh` 只 prune `devicetest/safety-[0-9]*/`，`safety-bank/` 语音库永不清理；latency"只记录不拦"（2026-11-06 决定）；`run_soak_card5.sh` 无 assume_idle，必须等真实 `State` 行。
- sticky 升级策略（PR #482）：`c1-self-2..5` 的 expect 保持 `crisis-self-escalation`，不得回退 `crisis-self-v1`。
- 时钟比对前必须跑 `clock_offset.py pre/post`；报表只认时钟偏移修正后的时间线。

## NOTES
- 相关 pytest 在 `scripts/tests/`（`test_tapwake_gate.py` 等）与 `tests/test_r02_gates.py`。
- 运行依赖真机（`/dev/cu.usbmodem*`）与 macOS（`say`/`afplay`）；离线验证用 `uv run pytest tests/test_r02_gates.py scripts/tests/test_safety_soak_report.py ...`。
- `flash-backups/` 存刷写前全片 `.bin` 备份，勿删。
