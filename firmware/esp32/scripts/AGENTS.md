# AGENTS.md — firmware/esp32/scripts

## OVERVIEW
固件构建/刷写/门禁/发布/资源生成工具链：8 bash + 9 Python CLI，全部顶层平铺、被 `tests/` 逐个测试。得分 12，独立知识域。

## WHERE TO LOOK
| 任务 | 位置 |
|------|------|
| 公共库（pin/env/Python 选择/TLS） | `common.sh`（所有 .sh source 它） |
| overlay 上树与幂等标记 | `bootstrap.sh` / `apply-overlay.sh` |
| 构建三变体 / 刷写 / 监视 | `build.sh` / `flash.sh` / `monitor.sh` |
| 总门禁（断言一切约定） | `check-overlay.sh` (709 L) |
| 身份分区保护 / 烧录前 verdict | `identity_guard.py` |
| OTA 发布签名/上传/撤回 | `publish_firmware_release.py` |
| mascot/离线音频资源生成 | `build_mascot_pack.py` / `build_offline_stories.py` / `check_device_ogg_aigc.py` |

## CONVENTIONS
- 每个 `.sh`：`#!/usr/bin/env bash` + `set -euo pipefail` + `SCRIPT_DIR=...` + source `common.sh`；`check-overlay.sh` 重新逐项验证这些。
- 不直接调 `idf.py`：标准序是 `select_python` → TLS 配置 → `configure_idf_python_env` → `find_idf_path` → source export.sh → 再 TLS → `need_command idf.py`；构建前 `cd "$MEMORIA_UPSTREAM_DIR"` 并断言 `$PWD`。
- Python：`from __future__ import annotations`、argparse 子命令、`main(argv=None) -> int` + `raise SystemExit(main())`；引用 repo 根用 `Path(__file__).resolve().parents[3]` 插 `sys.path` + `# noqa: E402`。
- 环境覆盖：`MEMORIA_ESP32_UPSTREAM_DIR` / `MEMORIA_ESP32_ARTIFACT_DIR` / `MEMORIA_ESP_IDF_PATH` / `MEMORIA_PYTHON_BIN` / `MEMORIA_FIRMWARE_LANGUAGE` / `MEMORIA_FIRMWARE_WAKE_WORD_MODEL`。
- 产物命名 `artifacts/memoria-esp-vocat{,-bench,-keep-awake}-{app,bootloader,partition-table}.bin`；全片镜像只允许 `<name>-DANGER_FULL_ERASE.bin` 且构建后即删 merged 镜像。
- bash 兼容 macOS bash 3.2：空数组展开加 `${arr[@]+"${arr[@]}"}`。

## ANTI-PATTERNS
- `identity_guard.py` 扫描一切 erase 写法：拒绝全片擦除、无分区 `nvs_flash_erase()`、擦 `memoria_identity`、未登记 allowlist 的 `esp_partition_erase_range`/`esp_flash_erase_region`；`flash.sh` 拒绝 merged/DANGER 镜像名，除非 `--allow-danger-full-erase`。
- bench/keep-awake 镜像绝不发布、绝不发船（`build.sh` 字面禁令，`publish_firmware_release.py` 按 marker/文件名拒绝）；两 flag 互斥；两个开关不得进产品 `config.json`。
- `build_offline_stories.py` 永不写音频、永不起子进程（ffmpeg 解码留在 `check_device_ogg_aigc.py`）；资产名 <= 32 字符；`assets` 8M 分区留 >= 0.5M 余量；`_placeholder` 结尾的 clip 不得发布。
- 秘密：seed 文件必须 mode 0600 且恰好 32 字节；`provision_identity` 不回显身份值/seed、不回传 NVS 生成器输出、不覆盖既有 `--output`；发布私钥必须 mode 600 Ed25519。
- 同 build 已有不同 `app.bin` 时发布器拒绝（"bump the build"）；OTA 上传 `--device` 与 `--global` 互斥。

## NOTES
- 评分 12（file 3 + code 2 + config 1 + symbol 2 + export 2 + ref 2，LSP 未测得处按报告符号面保守计）。
- `__pycache__/` 已入库，属噪音勿再增。
