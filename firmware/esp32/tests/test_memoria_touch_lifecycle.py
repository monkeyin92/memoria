"""Execute production board methods with the real offline menu state machine."""

import pathlib
import shutil
import subprocess

import pytest

MEMORIA_DIR = pathlib.Path(__file__).parents[1] / "overlay/files/main/memoria"
BOARD = MEMORIA_DIR.parent / "boards/memoria/esp-vocat/memoria_esp_vocat.cc"


def _board_method(source: str, signature: str) -> str:
    start = source.index(signature)
    brace = source.index("{", start)
    depth = 1
    end = brace + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


@pytest.fixture(scope="module")
def lifecycle_tool(tmp_path_factory):
    board = BOARD.read_text()
    methods = "\n".join(
        _board_method(board, s)
        for s in (
            "memoria::TapDevice TapDeviceFor(",
            "void ApplyOfflineMenuOutput(",
            "void DispatchTapWakeAction(",
            "void MaybeEnterDeepSleepFromWifiHold(",
            "static bool DeviceInConversation(",
            "void TickOfflineMenuNetwork(",
        )
    )
    code = (
        r"""
#include "memoria_power_policy.h"
#include "memoria_offline_menu.h"
#include <atomic>
#include <functional>
#include <vector>
#include <string>
#include <cassert>
#define ESP_LOGI(...) ((void)0)
#define pdMS_TO_TICKS(x) (x)
unsigned now=1000; bool online=true, playing=false;
std::function<void()> on_delay;
void vTaskDelay(unsigned) { if(on_delay) on_delay(); }
long long esp_timer_get_time() { return now * 1000LL; }
enum DeviceState { kDeviceStateStarting, kDeviceStateIdle, kDeviceStateWifiConfiguring,
                   kDeviceStateListening, kDeviceStateSpeaking };
struct Audio { bool IsPlaybackIdle() {return !playing;} };
struct Application {
 static Application& GetInstance() {static Application a; return a;}
 DeviceState state=kDeviceStateIdle;
 std::vector<std::function<void()>> tasks;
 Audio audio;
 DeviceState GetDeviceState() {return state;}
 Audio& GetAudioService() {return audio;}
 template<class F> void Schedule(F f) {tasks.push_back(f);}
 void WakeWordInvoke(const char*) {state=kDeviceStateListening;}
 void Run() {auto f=tasks.front(); tasks.erase(tasks.begin()); f();}
};
namespace memoria {
PowerPolicyRegistry& PowerPolicyRegistry::GetInstance() {static PowerPolicyRegistry p; return p;}
void PowerPolicyRegistry::SetPhase(PowerPhase p) {phase_=p;}
bool SendListeningResume() {return true;}
}
struct Board {
 std::atomic<bool> listening_capture_closed_{false}, offline_story_cancel_{false};
 std::atomic<unsigned> wifi_hold_since_ms_{0}, offline_play_generation_{0}, offline_prompt_generation_{0};
 static constexpr unsigned kWifiHoldNotArmed=0, kOfflineClipGapMs=1600;
 memoria::OfflineMenuMachine offline_menu_;
 int cues=0, sleeps=0;
 static bool NetworkLooksOnline() {return online;}
 void SyncOfflineMenuScreen() {}
 void EnterWifiConfigMode() {}
 void EnterTapDeepSleep() {++sleeps;}
 bool PlayLocalAssetClip(const char*) {++cues; return true;}
"""
        + methods
        + r"""
};
int main(int argc,char**argv) {
 std::string test=argv[1]; Board b;
 auto& app=Application::GetInstance(); auto& power=memoria::PowerPolicyRegistry::GetInstance();
 if(test=="resume") {
  app.state=kDeviceStateListening; power.SetPhase(memoria::PowerPhase::kWifiHold);
  b.wifi_hold_since_ms_=1;
  b.DispatchTapWakeAction(memoria::TapWakeAction::kResumeListening,"tap",now,50,true,false);
  assert(power.phase()==memoria::PowerPhase::kConversing);
  assert(b.wifi_hold_since_ms_==Board::kWifiHoldNotArmed);
  now=memoria::PowerPolicy::kDefaultWifiHoldMs+1000; app.state=kDeviceStateIdle; b.MaybeEnterDeepSleepFromWifiHold(); assert(b.sleeps==0);
 } else if((test=="recovery" || test=="resume_recovery")) {
  b.offline_menu_.OnTap(now,50,false,true);
  auto action=memoria::TapWakeAction::kStartConversation;
  if(test=="resume_recovery") {action=memoria::TapWakeAction::kResumeListening; app.state=kDeviceStateListening;}
  b.DispatchTapWakeAction(action,"tap",now,50,true,true);
  b.TickOfflineMenuNetwork();
  assert(b.offline_menu_.mode()==memoria::OfflineMode::kInactive);
  assert(app.tasks.empty()); assert(b.cues==1);
 } else if(test=="network_control") {
  b.offline_menu_.OnTap(now,50,false,true); b.TickOfflineMenuNetwork();
  assert(app.tasks.size()==1); app.Run(); assert(b.cues==2);
 } else {
  auto out=b.offline_menu_.OnTap(now,50,false,true);
  b.ApplyOfflineMenuOutput(out);
  if(test=="queued") assert(b.TapDeviceFor(kDeviceStateIdle)==memoria::TapDevice::kBusy);
  if(test=="cancel") {
   memoria::OfflineMenuOutput stop; stop.stop_story=true; b.ApplyOfflineMenuOutput(stop);
   app.Run(); assert(b.TapDeviceFor(kDeviceStateIdle)==memoria::TapDevice::kIdle); return 0;
  }
  if(test=="queued_replace") {
   b.ApplyOfflineMenuOutput(out); app.Run();
   assert(b.TapDeviceFor(kDeviceStateIdle)==memoria::TapDevice::kBusy);
  }
  int gaps=0;
  on_delay=[&] {
   ++gaps; playing=false;
   assert(b.TapDeviceFor(kDeviceStateIdle)==memoria::TapDevice::kBusy);
   if(test=="replace" && gaps==1) b.ApplyOfflineMenuOutput(out);
  };
  app.Run();
  if(test=="replace") {
   assert(b.TapDeviceFor(kDeviceStateIdle)==memoria::TapDevice::kBusy);
   on_delay=nullptr; app.Run();
  }
  assert(b.TapDeviceFor(kDeviceStateIdle)==memoria::TapDevice::kIdle);
  playing=true; assert(b.TapDeviceFor(kDeviceStateIdle)==memoria::TapDevice::kBusy);
 }
}
"""
    )
    root = tmp_path_factory.mktemp("touch_lifecycle")
    source = root / "test.cc"
    source.write_text(code)
    binary = root / "test"
    subprocess.run(
        [
            shutil.which("clang++") or "g++",
            "-std=c++20",
            "-I",
            str(MEMORIA_DIR),
            str(source),
            "-o",
            str(binary),
        ],
        check=True,
    )
    return binary


@pytest.mark.parametrize(
    "scenario",
    [
        "resume",
        "recovery",
        "resume_recovery",
        "network_control",
        "queued",
        "gap",
        "replace",
        "queued_replace",
        "cancel",
    ],
)
def test_touch_lifecycle(lifecycle_tool, scenario):
    subprocess.run([str(lifecycle_tool), scenario], check=True)


def test_protocol_reset_clears_closed_capture(tmp_path):
    import re

    source = (MEMORIA_DIR / "memoria_protocol.cc").read_text()
    header = (MEMORIA_DIR / "memoria_protocol.h").read_text()
    reset = _board_method(source, "void MemoriaProtocol::ResetSessionState()")
    names = set(re.findall(r"\b\w+_\b", reset))
    members = []
    for name in sorted(names):
        if name == "session_id_":
            members.append("std::string session_id_;")
            continue
        match = re.search(r"^    ([^\n;]+\b" + name + r"(?:\s*=[^;]+|\{[^;]*\})?);", header, re.M)
        assert match, name
        members.append(match[1] + ";")
    code = (
        r"""
#include <atomic>
#include <cstdint>
#include <deque>
#include <functional>
#include <mutex>
#include <string>
#include <cassert>
#include "memoria_vad_start.h"
using memoria::VadStartGate;
struct GenerationFence {};
struct DroppedFrameCounters {};
struct TransportAction {};
enum class ProfileApplyMode {kNextSession};
struct MemoriaAudioFrame {static constexpr unsigned kDownlinkFrameSamples24k=480;};
constexpr unsigned kDownlinkSampleRate24k=24000, kProtocolVersionV1=1, kProtocolVersionV2=2;
bool closed=true; int notifications=0;
void NotifyListeningClosed(bool value) {closed=value; ++notifications;}
struct MemoriaProtocol {
"""
        + "\n".join(members)
        + r"""
void ResetSessionState();
};
"""
        + reset
        + r"""
int main() {
 MemoriaProtocol p;
 p.protocol_version_=kProtocolVersionV2; p.session_id_="old"; p.stream_epoch_=7;
 p.ResetSessionState();
 assert(!closed && notifications==1);
 assert(p.session_id_.empty() && p.stream_epoch_==0);
 assert(p.resumable_session_id_=="old" && p.resumable_stream_epoch_==7);
 closed=true; p.terminal_session_close_=true; p.ResetSessionState();
 assert(!closed && notifications==2);
}
"""
    )
    path = tmp_path / "reset.cc"
    path.write_text(code)
    binary = tmp_path / "reset"
    subprocess.run(
        [
            shutil.which("clang++") or "g++",
            "-std=c++20",
            "-I",
            str(MEMORIA_DIR),
            str(path),
            "-o",
            str(binary),
        ],
        check=True,
    )
    subprocess.run([str(binary)], check=True)
