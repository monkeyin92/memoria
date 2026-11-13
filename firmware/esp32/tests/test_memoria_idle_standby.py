"""Run real product board standby methods across the former sleep deadline."""

import pathlib
import re
import shutil
import subprocess

import pytest


def _board_method(source: str, signature: str) -> str:
    start = source.index(signature)
    brace = source.index("{", start)
    depth, end = 1, brace + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


ROOT = pathlib.Path(__file__).parents[1]
MEMORIA = ROOT / "overlay/files/main/memoria"
BOARD = MEMORIA.parent / "boards/memoria/esp-vocat/memoria_esp_vocat.cc"


def test_product_idle_stays_connected_with_mic_closed(tmp_path):
    board = BOARD.read_text()
    worker = _board_method(board, "static void usb_command_task(")
    methods = "\n".join(
        _board_method(board, signature)
        for signature in ("void OnIdleScreenOff(", "void EnterTapDeepSleep(")
    )
    code = (
        r"""
#include "memoria_power_policy.h"
#include "memoria_mic_gate.h"
#include "memoria_usb_command.h"
#include <cstdint>
#include <cstddef>
#include <stdexcept>
#define CONFIG_MEMORIA_BENCH_SERIAL 0
#define ESP_LOGW(...) ((void)0)
#define O_RDONLY 0
int open(const char*,int) {return 1;}
long read(int,void*,size_t) {return -1;}
void vTaskDelete(void*) {}
constexpr int kUsbPollMs=20;
#define pdMS_TO_TICKS(x) (x)
int cycles=0;
void vTaskDelay(int) {if(++cycles>=4) throw 1;}
#include <atomic>
#include <cassert>
#include <initializer_list>
#define CONFIG_MEMORIA_KEEP_AWAKE 0
#define ESP_LOGI(...) ((void)0)
#define ESP_ERROR_CHECK(x) ((void)(x))
constexpr int TP_PIN_NUM_INT=10;
unsigned now=1000; int sleeps=0;
long long esp_timer_get_time() {return now*1000LL;}
int esp_sleep_enable_ext0_wakeup(int,int) {return 0;}
void esp_deep_sleep_start() {++sleeps;}
enum DeviceState {kDeviceStateIdle, kDeviceStateListening};
struct Application {
 static Application& GetInstance() {static Application app;return app;}
 DeviceState state=kDeviceStateIdle;
 DeviceState GetDeviceState() {return state;}
};
namespace memoria {
PowerPolicyRegistry& PowerPolicyRegistry::GetInstance() {static PowerPolicyRegistry p;return p;}
void PowerPolicyRegistry::SetPhase(PowerPhase p) {phase_=p;}
}
struct Board {
 void HandleUsbWake() {} void HandleUsbSleepOrPowerOff(bool) {} void HandleUsbTemp() {}

"""
        + methods
        + "\n" + worker.replace("MemoriaEspVocat", "Board")
        + r"""
};
int main() {
 Board b; auto& power=memoria::PowerPolicyRegistry::GetInstance();
 power.SetPhase(memoria::PowerPhase::kConversing);
 b.OnIdleScreenOff();
 assert(!memoria::MicWantedOpen(power.phase()));
 assert(memoria::MicGateTransition(true,power.phase())==memoria::MicGateAction::kClose);
 for (auto state : {kDeviceStateIdle, kDeviceStateListening}) {
 Application::GetInstance().state=state;
 for (unsigned elapsed : {180001u, 3600000u, 172800000u}) {
  now=1000+elapsed; cycles=0;
"""
        + "try {Board::usb_command_task(&b);} catch(int) {} assert(cycles>=4);\n"
        + r"""
  assert(sleeps==0);
  assert(power.phase()==memoria::PowerPhase::kWifiHold);
 }
 }
 b.EnterTapDeepSleep();
 assert(sleeps==1);
 assert(power.phase()==memoria::PowerPhase::kDeepSleep);
}
"""
    )
    source = tmp_path / "standby.cc"
    source.write_text(code)
    binary = tmp_path / "standby"
    subprocess.run(
        [
            shutil.which("clang++") or "g++",
            "-std=c++20",
            "-I",
            str(MEMORIA),
            str(source),
            "-o",
            str(binary),
        ],
        check=True,
    )
    subprocess.run([str(binary)], check=True)
    _assert_explicit_sleep_calls(board)


def test_closed_listening_stops_capture_and_touch_resume_reopens(tmp_path):
    board = BOARD.read_text()
    callback = _board_method(board, "memoria::SetListeningClosedHandler(") + ");"
    signature = "void ApplyListeningCapture("
    method = _board_method(board, signature) if signature in board else ""
    code = (
        r"""
#include <atomic>
#include <cassert>
#include <functional>
#include <vector>
enum DeviceState {kDeviceStateIdle, kDeviceStateListening};
struct Audio {
 bool processing=true, wake_word=false; int changes=0;
 void EnableVoiceProcessing(bool value, bool = true) {processing=value;++changes;}
 bool IsAudioProcessorRunning() {return processing;}
 void EnableWakeWordDetection(bool value) {wake_word=value;}
};
struct Application {
 static Application& GetInstance() {static Application a;return a;}
 DeviceState state=kDeviceStateListening; Audio audio;
 std::vector<std::function<void()>> tasks;
 DeviceState GetDeviceState() {return state;}
 Audio& GetAudioService() {return audio;}
 void Schedule(std::function<void()> f) {tasks.push_back(f);}
 void Run() {auto pending=tasks;tasks.clear();for(auto& f:pending) f();}
};
namespace memoria {
 std::function<void(bool)> handler;
 void SetListeningClosedHandler(std::function<void(bool)> f) {handler=f;}
}
struct Display {void SetListeningClosed(bool) {}};
struct Board {
 std::atomic<bool> listening_capture_closed_{false};
 Display display; Display* display_=&display;
"""
        + method
        + "\nvoid Initialize() {\n"
        + callback
        + r"""
 }
};
int main() {
 Board b;b.Initialize();auto& app=Application::GetInstance();
 memoria::handler(true);app.Run();assert(!app.audio.processing && !app.audio.wake_word);
 memoria::handler(false);app.Run();assert(app.audio.processing && !app.audio.wake_word);
 int changes=app.audio.changes;memoria::handler(false);app.Run();assert(app.audio.changes==changes);
 memoria::handler(true);memoria::handler(false);app.Run();assert(app.audio.processing);
 memoria::handler(true);app.Run();app.state=kDeviceStateIdle;
 memoria::handler(false);app.Run();assert(!app.audio.processing);
}
"""
    )
    source = tmp_path / "capture.cc"
    source.write_text(code)
    binary = tmp_path / "capture"
    subprocess.run(
        [shutil.which("clang++") or "g++", "-std=c++20", str(source), "-o", str(binary)], check=True
    )
    subprocess.run([str(binary)], check=True)


def test_closed_capture_discards_queued_uplink_and_resume_sends(tmp_path):
    protocol = (MEMORIA / "memoria_protocol.cc").read_text()
    send = _board_method(protocol, "bool MemoriaProtocol::SendAudio(")
    code = (
        r"""
#include "memoria_audio_frame.h"
#include <atomic>
#include <cassert>
#include <memory>
#include <vector>
#define ESP_LOGE(...) ((void)0)
#define ESP_LOGW(...) ((void)0)
namespace memoria {
std::atomic<bool> g_listening_closed{false};
constexpr unsigned kUplinkSampleRate=16000, kFrameMs=20, kMaxVadSpeechSamples=1000000;
struct AudioStreamPacket {
 std::vector<uint8_t> payload{1,2,3}; int sample_rate=16000,frame_duration=20;
};
struct Socket {int sends=0; bool Send(const uint8_t*,size_t,bool) {++sends;return true;}};
struct MemoriaProtocol {
 Socket socket; Socket* websocket_=&socket;
 unsigned stream_epoch_=1,uplink_sequence_=0;
 uint64_t uplink_sample_start_=0,vad_started_sample_=0;bool vad_active_=false;
 bool IsAudioChannelOpened() {return true;}
 void TrackUplinkVadFrame() {} void SendVadState(bool) {}
 bool SendAudio(std::unique_ptr<AudioStreamPacket>);
};
"""
        + send
        + r"""
}
int main() {
 using namespace memoria;MemoriaProtocol p;
 g_listening_closed=true;
 for(int i=0;i<10;++i) assert(p.SendAudio(std::make_unique<AudioStreamPacket>()));
 assert(p.socket.sends==0 && p.uplink_sequence_==0 && p.uplink_sample_start_==0);
 g_listening_closed=false;
 assert(p.SendAudio(std::make_unique<AudioStreamPacket>()));
 assert(p.socket.sends==1 && p.uplink_sequence_==1);
}
"""
    )
    source = tmp_path / "uplink.cc"
    source.write_text(code)
    binary = tmp_path / "uplink"
    subprocess.run(
        [
            shutil.which("clang++") or "g++",
            "-std=c++20",
            "-I",
            str(MEMORIA),
            str(source),
            str(MEMORIA / "memoria_audio_frame.cc"),
            "-o",
            str(binary),
        ],
        check=True,
    )
    subprocess.run([str(binary)], check=True)


def test_closed_listening_allows_control_poll_and_ota(tmp_path):
    protocol = (MEMORIA / "memoria_protocol.cc").read_text()
    predicate = _board_method(protocol, "bool IsConnectedStandby(")
    code = (
        r"""
#include <atomic>
#include <cassert>
enum State {kDeviceStateIdle,kDeviceStateListening,kDeviceStateSpeaking};
struct Application {
 State state=kDeviceStateIdle;
 static Application& GetInstance() {static Application app;return app;}
 State GetDeviceState() {return state;}
};
std::atomic<bool> g_listening_closed{false};
"""
        + predicate
        + r"""
int main() {
 auto& app=Application::GetInstance();assert(IsConnectedStandby());
 app.state=kDeviceStateListening;assert(!IsConnectedStandby());
 g_listening_closed=true;assert(IsConnectedStandby());
 g_listening_closed=false;assert(!IsConnectedStandby());
 app.state=kDeviceStateSpeaking;g_listening_closed=true;assert(!IsConnectedStandby());
}
"""
    )
    source = tmp_path / "poll.cc"
    source.write_text(code)
    binary = tmp_path / "poll"
    subprocess.run(
        [shutil.which("clang++") or "g++", "-std=c++20", str(source), "-o", str(binary)], check=True
    )
    subprocess.run([str(binary)], check=True)
    assert "if (IsConnectedStandby())" in _board_method(
        protocol, "void MemoriaProtocol::DisplayProfileTask("
    )
    assert "return IsConnectedStandby();" in _board_method(
        protocol, "void MemoriaProtocol::CheckFirmwareUpdate("
    )


def _assert_explicit_sleep_calls(board):
    allowed = [
        _board_method(board, "void ApplyOfflineMenuOutput("),
        _board_method(board, "void DispatchTapWakeAction("),
    ]
    assert all(len(re.findall(r"\bEnterTapDeepSleep\s*\(", body)) == 1 for body in allowed)
    remainder = board
    for body in allowed + [_board_method(board, "void EnterTapDeepSleep(")]:
        remainder = remainder.replace(body, "")
    assert not re.search(r"\bEnterTapDeepSleep\s*\(", remainder)
    assert "if (out.enter_deep_sleep)" in allowed[0]
    night = allowed[1].split("case memoria::TapWakeAction::kNightSoothing:")[1].split("case ")[0]
    assert "EnterTapDeepSleep();" in night
    assert board.count("esp_deep_sleep_start(") == 1
    assert "esp_deep_sleep_start(" in _board_method(board, "void EnterTapDeepSleep(")


def test_auto_sleep_call_mutation_is_rejected():
    board = BOARD.read_text()
    _assert_explicit_sleep_calls(board)
    mutated = board.replace("vTaskDelay(pdMS_TO_TICKS(kUsbPollMs));",
                            "self->RenamedIdleDeadline(); vTaskDelay(pdMS_TO_TICKS(kUsbPollMs));")
    mutated += "\nvoid RenamedIdleDeadline() { EnterTapDeepSleep(); }\n"
    with pytest.raises(AssertionError):
        _assert_explicit_sleep_calls(mutated)
    direct = board + "\nvoid RenamedIdleDeadline() { esp_deep_sleep_start(); }\n"
    with pytest.raises(AssertionError):
        _assert_explicit_sleep_calls(direct)


@pytest.fixture(scope="module")
def production(tmp_path_factory):
    tree = tmp_path_factory.mktemp("patched-capture")
    upstream = ROOT / ".cache/upstream"
    paths = ["main/application.cc", "main/audio/audio_service.cc", "main/audio/audio_service.h"]
    for relative in paths:
        dest = tree / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(subprocess.check_output(["git", "-C", str(upstream), "show", f"HEAD:{relative}"]))
    for patch in sorted((ROOT / "overlay/patches").glob("*.patch")):
        subprocess.run(["git", "apply", *[f"--include={p}" for p in paths], str(patch.resolve())],
                       cwd=tree, check=True, capture_output=True)
    return tree


@pytest.mark.parametrize("scenario", ["touch", "recovery", "deferred", "normal", "speaking"])
def test_real_capture_resume(production, tmp_path, scenario):
    app = (production / "main/application.cc").read_text()
    audio = (production / "main/audio/audio_service.cc").read_text()
    header = (production / "main/audio/audio_service.h").read_text()
    start = _board_method(app, "void Application::StartListeningAudio(")
    state = _board_method(app, "void Application::HandleStateChangedEvent(")
    listening = state[state.index("        case kDeviceStateListening:"):state.index("        case kDeviceStateSpeaking:")]
    enable = _board_method(audio, "void AudioService::EnableVoiceProcessing(")
    declaration = header[header.index("    void EnableVoiceProcessing("):].split(";", 1)[0] + ";"
    board = BOARD.read_text()
    callback = _board_method(board, "memoria::SetListeningClosedHandler(") + ");"
    capture = _board_method(board, "void ApplyListeningCapture(")
    getter = _board_method((ROOT / "overlay/files/main/memoria/memoria_protocol.cc").read_text(),
                           "bool IsListeningClosed(")
    code = r'''
#include "memoria_power_policy.h"
#include <atomic>
#include <cassert>
#include <functional>
#include <mutex>
#include <vector>
#include <string>
#define CONFIG_BOARD_TYPE_MEMORIA_ESP_VOCAT 1
#define ESP_LOGD(...) ((void)0)
#define AS_EVENT_AUDIO_PROCESSOR_RUNNING 1
void xEventGroupSetBits(int& bits,int mask) {bits|=mask;}
void xEventGroupClearBits(int& bits,int mask) {bits&=~mask;}
void esp_ae_rate_cvt_reset(void*) {}
namespace Lang {namespace Strings {const char* LISTENING="listening";} namespace Sounds {int OGG_POPUP=1;}}
namespace memoria {
 std::atomic<bool> g_listening_closed{false}; std::function<void(bool)> handler;
__CLOSED_GETTER__
 void SetListeningClosedHandler(std::function<void(bool)> f) {handler=f;}
 void Notify(bool value) {g_listening_closed=value;handler(value);}
 PowerPolicyRegistry& PowerPolicyRegistry::GetInstance() {static PowerPolicyRegistry p;return p;}
 void PowerPolicyRegistry::SetPhase(PowerPhase p) {phase_=p;}
}
struct Engine {void EnableVoiceProcessing(bool) {}};
struct AudioService {
 Engine engine; Engine* audio_engine_=&engine; bool audio_engine_initialized_=true;
 int event_group_=0,resets=0;bool audio_input_need_warmup_=false;bool wake=false;
 std::mutex input_resampler_mutex_;void* input_resampler_=nullptr;
 std::vector<int> playback;
 bool InitializeAudioEngine() {return true;}
 void ResetDecoder() {++resets;playback.clear();}
 bool IsAudioProcessorRunning() {return event_group_&AS_EVENT_AUDIO_PROCESSOR_RUNNING;}
 void EnableWakeWordDetection(bool value) {wake=value;}
 bool IsPlaybackIdle() {return playback.empty();}
 void PlaySound(int) {playback.push_back(1);}
''' + declaration + r'''
};
enum State {kDeviceStateIdle,kDeviceStateRecovering,kDeviceStateListening,kDeviceStateSpeaking};
constexpr int kListeningModeAutoStop=1;
struct Protocol {int starts=0;void SendStartListening(int) {++starts;}};
struct Display {void SetStatus(const char*) {} void SetEmotion(const char*) {} void SetListeningClosed(bool) {}};
struct Application {
 State state=kDeviceStateListening;AudioService audio_service_;Display display_object;
 Protocol proto;Protocol* protocol_=&proto;int listening_mode_=0;
 bool pending_listening_start_=false,play_popup_on_listening_=false;
 std::vector<std::function<void()>> tasks;
 static Application& GetInstance() {static Application a;return a;}
 State GetDeviceState() {return state;} AudioService& GetAudioService() {return audio_service_;}
 void Schedule(std::function<void()> f) {tasks.push_back(f);}
 void Run() {auto q=tasks;tasks.clear();for(auto& f:q) f();}
 void ConfigureWakeWordForListening() {audio_service_.EnableWakeWordDetection(false);}
 void StartListeningAudio();
 void EnterListening() {state=kDeviceStateListening;auto* display=&display_object;switch(state) {
''' + listening + r'''
 default:break;}}
};
struct Board {
 std::atomic<bool> listening_capture_closed_{false};Display display;Display* display_=&display;
''' + capture + "\nvoid Initialize() {" + callback + "}\n};\n" + enable + "\n" + start + r'''
int main(int argc,char** argv) {
 Board board;board.Initialize();auto& app=Application::GetInstance();auto& audio=app.audio_service_;
 std::string scenario=argv[1];
 if(scenario=="touch") {
  memoria::Notify(true);app.Run();assert(!audio.IsAudioProcessorRunning());
  audio.playback={1,2,3,4,5};int resets=audio.resets;
  memoria::Notify(false);app.Run();assert(audio.IsAudioProcessorRunning());
  assert(audio.playback.size()==5 && audio.resets==resets);
  memoria::Notify(false);app.Run();assert(audio.resets==resets);
  memoria::Notify(true);memoria::Notify(false);app.Run();assert(audio.IsAudioProcessorRunning());
 } else if(scenario=="speaking") {
  audio.event_group_ |= AS_EVENT_AUDIO_PROCESSOR_RUNNING;
  app.state=kDeviceStateSpeaking;memoria::Notify(true);app.Run();app.EnterListening();
  assert(!audio.IsAudioProcessorRunning() && !audio.wake);
  assert(memoria::PowerPolicyRegistry::GetInstance().phase()!=memoria::PowerPhase::kConversing);
 } else if(scenario=="recovery") {
  app.state=kDeviceStateRecovering;memoria::Notify(true);app.Run();
  app.EnterListening();assert(!audio.IsAudioProcessorRunning() && !audio.wake);
  assert(app.proto.starts==0);
  memoria::Notify(false);app.Run();assert(audio.IsAudioProcessorRunning());
 } else if(scenario=="deferred") {
  app.EnterListening();audio.EnableVoiceProcessing(false);
  memoria::Notify(true);app.Run();app.pending_listening_start_=true;
  app.StartListeningAudio();assert(!audio.IsAudioProcessorRunning() && !audio.wake);
  app.state=kDeviceStateIdle;memoria::Notify(false);app.Run();app.StartListeningAudio();
  assert(!audio.IsAudioProcessorRunning());
 } else {
  audio.playback={1,2};app.EnterListening();assert(audio.IsAudioProcessorRunning());
  assert(audio.resets==0 && audio.playback.size()==2);
  app.StartListeningAudio();assert(audio.resets==0);
  audio.EnableVoiceProcessing(false);audio.EnableVoiceProcessing(true);
  assert(audio.resets==1 && audio.playback.empty());
 }
}
'''
    code = code.replace("__CLOSED_GETTER__", getter)
    source = tmp_path / "capture.cc"
    source.write_text(code)
    binary = tmp_path / "capture"
    subprocess.run([shutil.which("clang++") or "g++", "-std=c++20", "-I",
                    str(ROOT / "overlay/files/main/memoria"), str(source), "-o", str(binary)], check=True)
    subprocess.run([str(binary), scenario], check=True)


def test_usb_periodic_auto_sleep_mutation_aborts(tmp_path, monkeypatch):
    board = BOARD.read_text().replace("vTaskDelay(pdMS_TO_TICKS(kUsbPollMs));",
                                    "self->EnterTapDeepSleep(); vTaskDelay(pdMS_TO_TICKS(kUsbPollMs));")
    mutated = tmp_path / "board.cc"
    mutated.write_text(board)
    monkeypatch.setitem(globals(), "BOARD", mutated)
    with pytest.raises(subprocess.CalledProcessError) as failure:
        test_product_idle_stays_connected_with_mic_closed(tmp_path)
    assert failure.value.returncode == -6
