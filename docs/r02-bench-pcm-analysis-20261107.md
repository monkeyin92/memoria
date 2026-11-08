# R02 current build 28 bench PCM diagnostic

## Scope

This diagnostic compared the current device audio lanes for the same stop materials used by R02. It used the bench image built from commit `8d80ddb98e5a86efc717f3008c7ca4c70a6ffa1a`, the resident serial logger, and 13 fixed 3-second journals.

The run used the existing stop audio files. It did not synthesize child-facing audio and it did not call paid Volc ASR. The Mac output was volume 30 and unmuted for every playback.

The raw receipt is [run-20261107-190940-r02-bench](../outputs/acceptance/run-20261107-190940-r02-bench/). The rerunnable capture and analysis tools are `capture_bench.py` and `analyze_bench.py` in that directory. `bench-analysis.json` contains the per-journal metrics.

## Receipt

The build record identifies the bench app as `memoria-esp-vocat-bench-app.bin`, 3,886,400 bytes, SHA-256 `d229459ecb350d0a93325535503b3ebd9907ad5d1457ce3388c4bdcbf8e0fdfd`. The main worktree does not retain that binary after the temporary build worktree was removed. The recorded generated configuration contained `CONFIG_MEMORIA_BENCH_SERIAL=y` and no `CONFIG_MEMORIA_KEEP_AWAKE=y`. The build record reports 115 bench assertions passed.

Before flashing, `flash_backup.py` saved the complete `ota_0` slot and the protected regions in [the device backup](../firmware/esp32/artifacts/backups/r02-bench-20261107-190940/). Every backup file matched the chip-computed MD5.

The run captured two controls and 11 duplex windows. The controls were near-end-only playback of a stop clip and robot playback without a near-end stop clip. The near-end-only control is not a pure-noise window. The duplex windows covered each of the eight stop materials once and repeated `停一下` three times. All 13 journals passed the PCMD checks. Every journal had zero drops and zero cuts.

The operation record says that the product image was restored from `ota0-before.bin` at `0x20000`, followed by the 8 KiB `0xff` otadata file at `0xd000`, and that both writes returned `Hash of data verified`. The retained MD5 files prove that boot and partition table, NVS, otadata, PHY, identity, `ota_0`, and assets matched their pre-flash values after restore. The identity region is therefore covered by the MD5 comparison; no separate namespace probe is retained.

The restored app contains `MEMORIA_FIRMWARE_BUILD=28` and `MEMORIA_KEEP_AWAKE_BUILD=1`. The retained post-restore log shows the keep-awake marker, successful connections to `aginice.cn`, and HTTP connection closures. The system output ended at volume 30 with output unmuted. The serial port was released after verification. No separate `ping=ok` line is retained in this receipt.

## Measured lanes

The values below are whole-window RMS levels in dBFS. The duplex rows contain 11 journals. The NCC values compare the saved stop stimulus with the device lane and show the largest absolute normalized correlation at any lag.

| Window | `play_pre_i2s` | `pre_afe_mic` | `pre_afe_ref` | `post_afe` | Stimulus NCC to mic | Stimulus NCC to post |
|---|---:|---:|---:|---:|---:|---:|
| Near-end only | empty | -45.06 | -330.31 | -45.77 | 0.157 | 0.157 |
| Play only | -21.41 | -38.32 | -21.36 | -68.09 | not applicable | not applicable |
| Duplex median | -21.67 | -38.64 | -21.80 | -47.07 | 0.144 | 0.167 |
| Duplex range | -22.70 to -20.40 | -39.38 to -37.03 | -22.74 to -20.43 | -47.64 to -45.88 | 0.084 to 0.219 | 0.108 to 0.267 |

The current build has playback-level energy in `pre_afe_ref` during the play-only and duplex windows. This differs from the historical build 24 journal where the reference lane was near silent. The level comparison does not prove timing alignment. The current `play_to_ref_ncc` calculation is low, so this run does not claim waveform alignment from NCC.

The play-only window has a `pre_afe_mic` level of -38.32 dBFS and a `post_afe` level of -68.09 dBFS. This shows that the post-AFE lane suppresses the pure playback condition in this window. It does not show whether the AFE preserves a near-end stop word during double talk.

The duplex stop stimulus correlations overlap the near-end-only control. This control includes the same kind of stop material and is not a noise floor. The run has no pure-noise window, so the NCC values cannot establish a detection threshold. The saved lane data cannot establish that the device microphone received an intelligible stop word, and it cannot establish that the post-AFE stage removed a word that was clearly present at its input.

## Decision boundary

The run establishes the following facts.

- The bench recorder, serial transport, and journal verifier worked on build 28. No journal had a drop, cut, CRC error, or framing error.
- The device output lane and the software AEC reference both carried playback-level energy in the current build.
- The post-AFE lane strongly reduced the pure playback control.
- The duplex stop material at Mac volume 30 overlapped the near-end-only control. The run did not include a pure-noise window, so it did not produce a noise baseline.

The run does not identify a hardware, capture, AEC, NS, DTLN, uplink, or production-ASR root cause. It also does not change the R02 stop gate or the recorded 0.8-second latency metric.

The next separating test is a volume-50 near-end-only, play-only, and duplex comparison using the same journal format, plus a separate pure-noise window. Follow it with a pre-AFE versus post-AFE comparison in a quiet environment. The current result is sufficient to avoid changing the stop vocabulary or claiming that cloud latency explains this acoustic failure.

## Volume-50 follow-up attempt

The follow-up used a newly built bench app from the same source commit. The build record reports 3,886,400 bytes and SHA-256 `1cab84fe21da935bf1aa2c8bd2d296265844255235f0e935fea6346e51e930fa`. The run had a separate backup, logger socket, and receipt directory. Mac output was set to 50, with input 91, alert 100, and output unmuted.

The run produced no valid PCM journal. The bench device entered `phase wifi_hold -> deep_sleep` at device uptime 204523 ms before the first planned window completed, and the USB serial node disappeared. The capture process was stopped. The partial receipt is [run-20261107-205811-r02-bench-vol50](../outputs/acceptance/run-20261107-205811-r02-bench-vol50/), with the failure recorded in `ABORTED.json`.

This is an operational failure, not a volume-50 acoustic result. It shows that the current bench image cannot support the expanded control plan without a wake or keep-awake strategy. No volume-50 NCC, RMS, or word-intelligibility conclusion is valid.

The product image was restored from the second pre-flash backup. The post-restore MD5 values for boot and partition table, NVS, otadata, PHY, identity, `ota_0`, and assets equal the new pre-flash snapshot. The product logger returned `ping=ok`, its socket was released, and Mac output was restored to 30. The restore receipt is `final-state.json` in the same run directory.

## 2026-11-07 current build 28 bench keep-awake diagnostic

The second diagnostic used the new `--bench-keep-awake` profile. The profile enables `CONFIG_MEMORIA_BENCH_SERIAL=y` and `CONFIG_MEMORIA_KEEP_AWAKE=y`. The combined app is [memoria-esp-vocat-bench-keep-awake-app.bin](../firmware/esp32/artifacts/memoria-esp-vocat-bench-keep-awake-app.bin), with SHA-256 `1b873844a377d21a7264c61a4caf281645b64379ab0e1f20af68051ebf236e51`. The build record names source commit `8d80ddb98e5a86efc717f3008c7ca4c70a6ffa1a`; the build used a worktree with uncommitted profile changes, so the commit alone is not a complete reproduction key.

The full receipt is [run-20261107-230946-r02-bench-keep-awake](../outputs/acceptance/run-20261107-230946-r02-bench-keep-awake/). Its capture summary records operator-controlled output volume per case. The observed output volumes were 25 and 50, so this run is not a fixed-volume comparison and volume is not a gate.

The keep-awake profile kept the USB serial connection available after the screen turned off. At `up_ms=221907`, the bench status reported `screen_off=1 sleeping=0`. The latest recorded screen-off status was `up_ms=342607`, and the serial log contains zero `deep_sleep` lines. This proves that the profile prevented the operational USB loss in this run. It does not establish behavior for every future bench run.

The capture produced 13 journals. All 13 were intact, with zero drops and zero cuts. The duplex set contains eight stop materials and three repeated `停一下` cases. The mixed-volume duplex median was `pre_afe_mic=-38.93 dBFS`, `pre_afe_ref=-23.04 dBFS`, and `post_afe=-49.15 dBFS` across 11 journals. Stimulus NCC medians were `0.1151` to `pre_afe_mic` and `0.1427` to `post_afe`. The near-end control was a stop clip, not a pure-noise window, so these values do not define a noise threshold or prove word intelligibility.

The result confirms that the build 28 recorder, lane transport, and journal checks worked with the keep-awake profile. It still cannot separate hardware, capture, AEC, NS, DTLN, uplink, or production ASR as the cause of the R02 stop failures. The 0.8-second value remains a recorded metric.

After capture, the run wrote [memoria-esp-vocat-app.bin](../firmware/esp32/artifacts/memoria-esp-vocat-app.bin) with SHA-256 `91b803600ca966172f5ce87324126bb0b2ba179c6a34e212508050adb05531f7`. The artifact contains the build 28 marker, and the retained strings check found no `MEMORIA_BENCH_BUILD=1;` or `MEMORIA_KEEP_AWAKE_BUILD=1;` marker. The post-restore log reports `wifi hold started hold_ms=180000`.

The pre-flash `ota_0` backup contained `MEMORIA_KEEP_AWAKE_BUILD=1`, so writing the product app switched the device from the keep-awake image to the product build. It was not a byte-identical restoration of the pre-flash app. Boot and partition table, `otadata`, PHY, identity, and assets matched their pre-flash MD5 values. `ota_0` differed because the product app was written. NVS also differed between the two retained snapshots, from `571b6df5cb25db36104ead086e43431b` to `f78de0276c1df61ccb2fece369eccd83`. The available snapshots cannot determine whether that NVS difference came during the bench run or product boot. The serial owner was released after the restore.
