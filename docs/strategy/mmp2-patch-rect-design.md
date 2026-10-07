# MMP2 设计稿：补丁矩形重绘（分层 rig 的成本前提）

状态：**已确认（用户 10-07 10:3x「你用电脑模拟我测试，确认方案，开始做吧」）**；§6.1 已实测（§6.1a），音频承载复核通过（10:29–10:32 音量 30 下 4/4，`run-20261007-audio-integrity-vol30/`）。实现开始。
来源：M-6 的 scratch 实验否定了「静止大底图 + 只动小部件」的自动变快（TODOLIST M-6），根因是 MMP1 的补丁帧（眨眼、张嘴）要整幅重采样：`MascotPack::Frame()` 把补丁应用到 base 的整张画布上（256×256 ≈ 65k px），场景再按整张 sprite 采样（`memoria_mascot_pack.cc` 的 `Frame()` 与 `memoria_mascot_raster.cc`）。

## 1. 要解决的问题（数字）

板上每个绘制帧约 85–92 ms CPU（anim 50 + taskLVGL 35–40）。scratch 实验（身体与声环全关、只留眨眼 / 张嘴）仍要约 59 ms：补丁应用 + 整幅重采样 ≈ 65k 像素的采样，按快路径约 510 ns/px ≈ 33 ms，再加 LVGL 把整幅精灵包围盒刷到屏幕的 35–40 ms。

而实际每帧变化的像素只有 6.5k–18k（主机测量，TODOLIST M-6）。眨眼 / 张嘴补丁的矩形只有 **5.2k–12.8k 像素**（starlight 实测，五个角色的补丁 380–23k）：

| | 整幅重采样（现在） | 补丁矩形重采样（MMP2） |
|---|---:|---:|
| 采样像素/帧 | ~65k（整幅 sprite） | 5–13k（补丁矩形） |
| 采样 CPU/帧（~510 ns/px） | ~33 ms | ~2.6–6.6 ms |
| LVGL 需要刷的面积 | 整幅包围盒（~13 万 px，几乎整屏） | 补丁矩形（~5–13k px，屏幕的 4–10%） |
| 每绘制帧合计（估） | 85–92 ms | **~15–25 ms** |

最后一行取决于 LVGL 对小矩形的处理（见 §6 的风险），这是设计稿要先请示的部分。

## 2. 不改什么（约束）

- 调色板 + zlib 的数据流不变；MMP1 包必须仍然可读（`test_pack_budget_fits_the_assets_partition` 与旧包回归）。
- 全姿势帧（9 个）的路径不变：它们仍整幅解码、整幅采样（换姿势时一次 65k 采样是可接受的——它发生在「说话与待命切换」这种本来就要大变的时刻）。
- 采样器、脏矩形合成器、帧节拍器、M-1 的电平接口全部不动：MMP2 只改变「补丁帧以什么形状交给合成器」。
- 产品镜像里没有 bench 代码；MMP2 的开关只进包格式与合成器内部，不加 Kconfig。

## 3. 包格式（MMP2 = MMP1 + 两个字段）

保持 64 B 文件头与 24 B 帧条目布局，把条目里 `reserved` 的 4 字节用掉 1 位 + 保留补丁矩形：

- 文件头 `version` 从 1 → 2。
- 帧条目（base != 0xFF 的补丁帧）在 `offset`/`stream_size` 后的 `reserved` 4 字节改为 `uint32 flags`：bit0 = `patch_rect_only`（1 = 这个补丁声明了独立矩形，支持按矩形重绘）。
- 补丁矩形 = 现有 `x, y, w, h` **已经是**应用区矩形（`DecodePatch` 的裁剪域），MMP2 不新增字段，只是让消费端真正用它：`Frame()` 不再把补丁铺到整张 base 上，而是返回一个「base 整幅 + 补丁矩形内替换」的组合视图。

旧包（version 1）：flags 位缺省为 0，走原路径。

## 4. 设备端数据结构（256×256 画布）

现在 `Frame()` 返回整张 base（含补丁）。MMP2 改为：

- 每个 base 帧仍整幅解码（rgb/alpha/span 三平面，~200 KB PSRAM，不变）。
- 补丁帧不再维护 256×256 的暂存 sprite（现在只有一个 `scratch_`，来回换补丁帧时反复整幅 memcpy base + 写补丁）。改为：
  - `Frame()` 返回 base sprite，附带一个「活性矩形」列表（最多 2 个：补丁矩形 + 上一个补丁矩形的擦除区）。合成器只对这个矩形内的行调用采样，矩形外直接用 base 的列跨度（**上一帧的 `touch_lo_/hi_` 机制已就位**，M-6 的 span-compose 在这里正好被用上）。
  - 眨眼 / 张嘴在 base 上是「把眼睛 / 嘴区域换成闭眼 / 张嘴」，补丁矩形外 base 与补丁帧完全一致——这正是补丁的定义。所以合成器画「补丁帧」= 画 base + 在补丁矩形内画补丁内容。
- 内存变化：每个包省掉一个 65k 像素的暂存 sprite（rgb 2 B + alpha 1 B + span ≈ 197 KB PSRAM），换 2 个矩形的常量。**这个省出来的空间是 M-8 角色包预算的额外余量。**

## 5. 合成器改动（`memoria_mascot_scene.cc`）

- `Placement` 增加一个 `active_box`（补丁矩形在屏幕上的投影，含采样器边缘余量）；补丁帧时 `sprite_box` 仍按整幅算（视口裁剪不变），但 `RedrawRect` 的窄化逻辑把「合并上一帧与本帧的 sprite 覆盖列」改为「上一帧与本帧的 active_box 覆盖列」。
- 全姿势帧：`active_box` = 整幅 sprite_box，行为与现在一致。
- 与 span-compose 的关系：span-compose 的「覆盖列并集」继续成立，只是覆盖列从「整幅投影的并集」收窄为「活性矩形投影的并集」。thinking 状态的彗星环路径不变。

## 6. 风险与必须先量的事

1. **LVGL 对小矩形的刷新是否真的便宜**：**已量（10-07 10:0x，探针 bench 镜像，见 §6.1a）**——taskLVGL 从整盒的 35–40 ms/帧降到 **17.5–21.9 ms/帧（−18 ms，约一半）**，帧率从约 6.5 升到 8.3/s（省出的预算被动画任务拿去多画了）。这仍不是 MMP2 的全部收益：探针退出条件保守（`RectEmpty(active)` 才回退整盒）而且合成与帧率都驱动 invalidate；它只证明「小 invalidate 便宜」，方向成立。
   - §6.1a 探针的做法与收据（`docs/acceptance/run-20261007-lvgl-rect-probe/`、`outputs/serial/robot-20261007-lvgl-rect-probe.log`）：场景合成与 display 的 invalidate 改为只递交 `last_active_box()`（本帧 + 上一帧补丁矩形的屏幕投影并集，`CanvasRectBounds` 与 `SpriteBounds` 同公式，`RectEmpty` 时回退整盒）；合成逐位不变（预览 851 帧 mismatch 0，compose_per_frame 与 main 相同）；板上 anim 40–60 ms/帧与昨晚满帧几乎一样（`drawn` 窗口 `render_avg` 40.7/60.3/52.7 ms，昨晚 31.8–69.9），所以 taskLVGL 的下降（35–40 → 17.5–21.9）归给「invalidate 面积变小」。代码在分支 `feat/mmp2-lvgl-rect-probe`（worktree `/Users/monkeyin/projects/memoria-m6lv`，未提交）；板子已刷回 build 24。
   - **同一个窗口的独立发现（不归因此探针，音量 6 的已知弱点加重了它）**：第 3、4 句没有回复。桥日志里 09:58:31 起音频帧一直 `Encode queue is full`（最高丢到 249 帧、稳定不涨），FunASR 只拿到 text_len 3–5 的碎片。对照昨晚 fastpath 探针（音量 6，4/4 全答、`Encode queue` 也满 172 次）：这次 anim 每帧 50.2 ms + opus_codec 0.34 核 + taskLVGL 0.17，核 1 满载时 encode 的节奏被打断到把句子切碎。**结论：32 ms 口径下的音频承载仍要复核——不是探针本身的失败，而是「低音量 + 满核」组合已两次暴露在边缘上。**
2. 调色板共享：补丁与 base 共用一张 255 色调色板（MMP1 现状），眼白 / 眼睑色已在内；MMP2 不改。若分层美术（M-7 的美术件）颜色超 255，才需要 per-part 调色板——先不做。
3. 双补丁帧（同时眨眼 + 张嘴）不存在：MMP1 的帧表里眨眼与张嘴是互斥的帧（`default_blink`、`default_talk`），MMP2 维持。
4. 边缘余量：补丁矩形外扩 1–2 px（采样器 `kBoundsMargin` 与背景行差异），避免旧帧残留。

## 7. 交付物与验收（设计稿通过后）

1. `build_mascot_pack.py` 输出 version 2 包（带 flags 位），`MascotPack::Load` 接受 1 与 2。
2. 合成器按 §5 改；`preview_memoria_mascot.py` 出对比视频（左：MMP1 路径；右：MMP2）。
3. 测试：`test_scripted_day_redraws_exactly`（增量 = 全量）继续 0 不一致；新增「补丁帧只合成补丁矩形」的像素上限测试（照 `test_a_sprite_redraw_only_recomposes...` 的样式）；包体积回归。
4. 主机预览通过后，bench 镜像上板量 `anim + taskLVGL`（判据：≤ 32 ms，用 `profile/task_table.py` 量），你看过对比视频再决定是否把 MMP2 用于产品镜像。

## 8. 工作量估计（供排期）

- `build_mascot_pack.py`：小（flags 位 + 打包脚本，半天内）。
- `MascotPack::Frame()` 状态机：中（去 scratch 整幅、返回活性矩形，1 天 + 测试）。
- 合成器 active_box：中（与 span-compose 交织，1 天）。
- 上板量与调：半天。
- 合计约 3 天工作量，前置一个 LVGL 小矩形成本实验（半天，含一次刷机）。
