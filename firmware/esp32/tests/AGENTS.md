# AGENTS.md — firmware/esp32/tests

## OVERVIEW
固件行为测试：42 个 pytest 模块 + C++ host harness，两类风格——真代码编译测试与源文本/策略断言。得分 12，独立知识域。

## WHERE TO LOOK
| 任务 | 位置 |
|------|------|
| 协议整体（源文本级，最重） | `test_memoria_protocol_source.py` (2304 L, 92 用例) |
| 真实 patch 行为编译测试 | `test_memoria_playback_supply_meter.py` / `test_memoria_media_realtime.py` |
| 身份/擦除/发布守卫 | `test_identity_guard.py` / `test_memoria_local_data_erase.py` / `test_publish_firmware_release.py` |
| 表情/动画像素级 | `test_memoria_dot_face.py` / `test_memoria_mascot*.py` |
| host fake 与工具 | `erase_host/` / `activation_retry_host/` / `diag_pcm_tool.cc` |
| mascot 二进制夹具 | `fixtures/mascots/` |

## CONVENTIONS
- 两型区分：harness 型在 Python 里内嵌 `HARNESS = r"""..."""` C++（stdin 命令行协议），用 `shutil.which("clang++") or shutil.which("g++")` 编译（缺编译器 `pytest.skip`）；策略型直接读 overlay 源码/patch/manifest 断言子串与 `str.index` 顺序。
- 每个固件 header 模块都要有 `test_header_stays_free_of_esp_dependencies`（host 可编译）与 host 行为测试对；真实 patch 文本被拼进 harness（如 `ResetDecoder` 观察块）——upstream 改函数名即碎，属已知脆弱点。
- 路径一律 `Path(__file__).parents[1]`（firmware 根）锚定，绝不依赖 CWD；upstream 源码 fixture 从 locked git 对象 `git show HEAD:<path>` 重建 + `git apply --unsafe-paths --include=<path>` 重打 patch，不读 `.cache/`。
- `test_memoria_media_realtime.py` 的 `run_cpp`/`load_audio_source` 被 playout_prefill 与 speaking_first_frame 经 `runpy.run_path` 复用——改它影响 3 个文件。
- 脚本被测对象经 `importlib.util.spec_from_file_location` 加载，不进 sys.path。
- Docstring 引用 TODOLIST id（N-10 / M-1 / P2-08）做溯源。

## ANTI-PATTERNS
- release-blocker 追踪：overlay 里 `TODO(release-blocker): ...` 必须以同样文字、未勾选形式出现在 `HANDOFF.md` 的 `## 发布前检查清单`，否则 `test_release_blockers.py` 失败（现存 blocker：USB serial wake）。
- 不弱化验证：编译必须带 `-Wall -Wextra -Werror`；mascot 系编译必须带 ASan/UBSan；dot_face 固定美术签名 `CLOSED_CALM_SIG`，改 calm 脸像素需同步改测试。
- 测试断言禁拍的场景包括：`/dev/secondary` 不得出现在 `main/memoria/*.cc`；`usb_wake` 不得进 patch；擦除 host fake 故意不声明 `nvs_erase_all` 等，误用即编译失败。
- mascot `.mmp` 不跨机器字节比对（Pillow 重采样 x86/ARM 有差），比解码帧 + 阈值；stale pack 靠重建命令提示，不要手改二进制。
- 永不弱化门禁：AIGC 标签、资产预算 `< 1.6MB`、pack manifest 与 `memoria_offline_menu.h` id 序一致性都是必须项。

## NOTES
- 评分 12（file 3 + subdir 2 + code 2 + symbol 2 + export 2 + ref 1，LSP 未测得处按报告符号面保守计）。
- 无编译器时大量用例 skip 不算失败；`test_memoria_protocol_source.py` 载入约 20 个源文件，固件改名会大面积碎。
- 运行：`uv run pytest firmware/esp32/tests -q`；上游缓存用 `MEMORIA_ESP32_UPSTREAM_DIR` 覆盖。
