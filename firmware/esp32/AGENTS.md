# AGENTS.md — firmware/esp32

## OVERVIEW
ESP32-S3 儿童陪伴机器人固件：以固定 commit 的 78/xiaozhi-esp32 upstream 加 overlay 维护，绝不直接改 vendored 源码。

## STRUCTURE
```
esp32/
├── overlay/        # patches/ + files/（唯一手写的固件代码）
├── scripts/        # build/flash/check/publish 工具链
├── tests/          # 主机侧 pytest + C++ harness
├── artifacts/      # 构建产物（gitignored）
├── .cache/         # locked upstream 克隆（gitignored）
└── upstream.lock   # 所有 pin（repo/commit/IDF/board/python）
```

## WHERE TO LOOK
| 任务 | 位置 |
|------|------|
| 设备端协议、激活、会话状态机 | `overlay/files/main/memoria/` |
| 面板 UI、mascot、表情、触摸 | `overlay/files/main/boards/memoria/esp-vocat/` |
| 构建、刷写、发布、身份保护 | `scripts/` |
| 固件行为测试 | `tests/` |
| 完整门禁 | `scripts/check-overlay.sh` |

## CONVENTIONS
- Overlay 流程：`bootstrap.sh` 克隆 locked upstream 到 `.cache/upstream`，`apply-overlay.sh` 按文件名序打 `overlay/patches/NNNN-slug.patch`（append-only 序号，永不复用）再整树覆盖 `overlay/files/**`，写 `.memoria-overlay.sha256` 标记幂等。
- 产品专属代码一律 `#if CONFIG_BOARD_TYPE_MEMORIA_ESP_VOCAT` 门控；构建变体不是 Kconfig 而是 config manifest：`config.bench.json` = 产品 + `CONFIG_MEMORIA_BENCH_SERIAL=y`+`CONFIG_LV_USE_SNAPSHOT=y`，`config.keep-awake.json` = 产品 + `CONFIG_MEMORIA_KEEP_AWAKE=y`。
- 所有 shell：`set -euo pipefail` + source `scripts/common.sh`；所有 pin 来自 `upstream.lock`；TLS 强制 certifi，拒绝 `GIT_SSL_NO_VERIFY`。
- 分区表 `partitions/v2/16m.csv`：`memoria_identity` 64K @ 0x10000（身份分区），双 OTA，`assets` spiffs 8M。

## ANTI-PATTERNS
- 永不擦除 `memoria_identity`（0x10000）：全片擦除、`merged.bin`、`DANGER_FULL_ERASE` 一律被 `identity_guard.py` / `flash.sh` 拒绝，除非显式 `--allow-danger-full-erase`。
- bench / keep-awake 镜像"never publish it, never ship it"——`publish_firmware_release.py` 因 build marker 拒绝；`--bench` 与 `--keep-awake` 互斥。
- 产品 `config.json` 不得出现 `CONFIG_MEMORIA_BENCH_SERIAL` / `CONFIG_MEMORIA_KEEP_AWAKE`；产品 sdkconfig 保持 tap-only（无 keyword wake）、device AEC、`CONFIG_OTA_URL=""`、16MB flash。
- 不改 upstream 源码、不新增未经 `check-overlay.sh` 守护的 flash 计划；patch 依赖序号连续性。
- OTA 必须 fail closed：`UpgradeFirmware` 覆写为 "Memoria OTA is disabled"，MCP upgrade 工具已移除。

## NOTES
- 每个子目录（memoria / esp-vocat / scripts / tests）有各自的 AGENTS.md，细节勿在此重复。
- 门禁：`cd firmware/esp32 && ./scripts/bootstrap.sh && ./scripts/build.sh --clean && ./scripts/check-overlay.sh`；CI 用 `espressif/idf:v6.0.2` 容器。
- 评分 8（file 3 + subdir 2 + code 2 + config 1），8-15 区间，属独立子树顶层知识域，故创建。
