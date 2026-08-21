# Memoria Device Playback Fence Contract Release 20260821-194700-device-playback-fence-contract

## 发布目标

- 修复 ESP32 播放回执与 Go Media Edge 之间遗漏 `session_epoch` 的跨语言 fence 契约漂移。
- 让设备 `playback.started/progress/ended/error` 和 `button.stop` 使用与 Edge/Voice Core 相同的完整
  `session_epoch + turn_id + generation_id + tool_epoch`。
- 保持 fail-closed：缺少或带零 `session_epoch` 的旧设备回执不进入当前 generation；不改变 ASR、TTS
  生成、权限或交互权威边界。

## 候选范围

- 源码/tag：`20260821-194700-device-playback-fence-contract`，tag 固定到本次代码与门禁提交。
- 切换：Go Media Edge；随后刷写当前目标 ESP32-S3 板卡固件。
- 保持不变：Agent、Voice Core Media Bridge、Control API、LiveKit、Speaker Model、网关、H5、小程序、
  Nginx、数据库、Redis、MinIO 与设备身份/NVS。

## 本地门禁

- Media Edge `go test ./...` 通过。
- 固件针对性 `test_playback_receipts_v2_carry_full_fence_and_source_precision` 通过。
- ESP-IDF 6.0.2 clean build、merge-bin、overlay gate 和 `git diff --check` 通过。
- 固件产物：app `2,951,648` bytes，merged `13,577,086` bytes；身份区必须保持刷前/刷后逐字节一致。
- 固件源码测试中另有一个既有的 transport callback 字符串断言失败；该失败不由本候选改动引入，不能
  与本次编译/契约门禁混报为通过。

## 验收边界

- Media Edge 切流和固件刷写完成后，服务器健康/readiness 只记为 `enabled + production runtime verified`，
  不替代真机播放证据。
- 真机复验必须不在播放中再次按键，采集同一 session/generation 的 ASR final、首帧、完整 playback
  receipts、WSS close cause、`playback_ended` 与 Actual Heard。
- 在修复后真机形成完整播放终态前，保持 `direct_real_device_verified=false`、`full_duplex_verified=false`
  和 T1–T14 `0 pass / 14 blocked / 0 failed`。

## 当前状态

- `code + wired + local verified`（2026-08-21）；生产 Edge 切流与固件刷写待执行。
