# Offline story menu (2026-10-14)

Owner-approved product change. One PR. Do not merge from this agent. Do not flash.

## Problem

When Wi-Fi is down, a screen tap must not open a cloud conversation. The device plays fixed local lines and a three-story menu. All audio is placeholder Opus until the owner picks a voice.

Constraints from main @27bf17b and offline-lines v2.1:

- Tap wake, quiet hours, keep-awake, pat/shake, and BOOT provisioning stay as they are.
- `CONFIG_MMAP_FILE_NAME_LENGTH=32`. Upstream packs `DEFAULT_ASSETS_EXTRA_FILES` by basename, so `GetAssetData` keys are flat names such as `offline-wake-no-network-v1.ogg`, not subdirectory paths.
- Assets partition 8 MiB with >=0.5 MiB headroom.

## Usage (caller's view)

```cpp
memoria::OfflineMenuMachine menu;
menu.OnDeepSleepWake(now_ms);                 // first tap after deep sleep: wake only
auto out = menu.OnTap(now_ms, hold_ms, online, quiet, device_idle, in_conversation);
for (auto id : out.clips) PlayLocalAssetClip(ClipFile(id));
if (out.enter_deep_sleep) EnterTapDeepSleep();

menu.OnNetworkDown(now_ms, in_conversation);  // mid-chat vs idle standby
menu.OnNetworkUp(now_ms);
menu.OnTimer(now_ms);                         // 20s reconnect, 30s menu idle
menu.OnStoryFinished(now_ms);
```

Host tests compile the header with g++ and drive the same events through a stdin harness, matching `test_memoria_power_policy.py`.

## Shape

`OfflineMode` enum owns the offline UX. Power phases stay separate.

| Mode | Meaning |
|------|---------|
| `kInactive` | Online path; offline machine ignores taps |
| `kAwakeReconnecting` | After deep-sleep wake while offline; 20s retry; taps play reconnecting line |
| `kStandbyOffline` | Idle drop: silent background retry; next tap opens wake+menu |
| `kMidReconnecting` | Mid-chat drop after 4+5+6; 20s; taps play #7 |
| `kMenu` | Item 1..3 selected |
| `kPlaying` | Story playing for current item |
| `kPendingOnlineAfterStory` | Network restored during play; finish story then #10 |

Events are pure inputs. Outputs are ordered clip ids, optional `start_story` / `stop_story` / `enter_deep_sleep` / `retry_network` flags. The board shell plays clips, starts/stops story Ogg, and calls `EnterTapDeepSleep` (respecting keep-awake).

Hold classification: `hold_ms < 800` is a tap; `hold_ms >= 800` is a long press. Press time is measured in the touch task.

## Synthesis decision

Candidate A (separate `memoria_offline_menu.h`) is the base. Candidate B (extend `PowerPhase`) lost because quiet-hours and WifiHold already own that enum, and host tests for tap wake would grow unrelated states. Graft from B: reuse the host-compilable header + stdin harness pattern from `memoria_power_policy.h`.

## Tradeoffs accepted

- We accept polling or board-observed Wi-Fi edges instead of a new Application patch, so the offline UX stays board-local.
- We accept flattened asset basenames (upstream packer). Manifests under `stories/` and `offline_lines/` stay host-side; packing overwrites a copied `index.json` with the real assets index.
- We accept placeholder sine Ogg until voice selection.

## Alternatives considered

1. Fold offline modes into `PowerPhase`. Rejected: mixes conversation power with menu UX; enlarges every tap-wake test.
2. Drive the menu from Application patches only. Rejected: board already owns touch hold and local clips; scattering transitions would re-encode the same table.

## Resolved product calls (were "待技术部定")

1. Long-press stop while playing: replay current item (#14/#15/#16) then #17; restart 30s idle timer.
2. Network drop while idle/standby (not in conversation): play nothing; background retry only. Next tap if still offline: #1+#2+#3 then menu.
3. Menu idle into deep sleep: stop reconnecting. On wake: retry 20s; if still offline, play #1+#2+#3 then menu.

## Next implementation step

Add `memoria_offline_menu.h`, rename story_03, generate offline line placeholders, wire the board, add host tests and asset checks.
