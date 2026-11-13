"""Compile actual patched admission/priority functions with host boundary stubs."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).parents[1]
PROTOCOL = ROOT / 'overlay/files/main/memoria/memoria_protocol.cc'


def function(source: str, signature: str) -> str:
    start = source.index(signature)
    brace = source.index('{', start)
    depth = 1
    end = brace + 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[start:end]


def run_cpp(tmp_path: Path, source: str) -> str:
    compiler = shutil.which('clang++') or shutil.which('g++')
    assert compiler, 'host compiler required'
    cpp, binary = tmp_path / 'probe.cc', tmp_path / 'probe'
    cpp.write_text(source)
    subprocess.run([compiler, '-std=c++17', '-pthread', '-Wall', '-Wextra',
                    '-Werror', str(cpp), '-o', str(binary)], check=True)
    result = subprocess.run([str(binary)], text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def load_audio_source(tmp_path: Path) -> tuple[str, str]:
    upstream = Path(os.environ.get('MEMORIA_ESP32_UPSTREAM_DIR', ROOT / '.cache/upstream'))
    # Start from locked Git objects, never the generated cache's working files.
    target = tmp_path / 'main/audio'
    target.mkdir(parents=True)
    for name in ('audio_service.cc', 'audio_service.h'):
        content = subprocess.check_output(
            ['git', '-C', str(upstream), 'show', f'HEAD:main/audio/{name}'])
        (target / name).write_bytes(content)
    for patch in sorted((ROOT / 'overlay/patches').glob('*.patch')):
        subprocess.run(['git', 'apply', '--unsafe-paths',
                        '--include=main/audio/audio_service.cc',
                        '--include=main/audio/audio_service.h', str(patch)],
                       cwd=tmp_path, check=True, capture_output=True)
    return (target / 'audio_service.cc').read_text(), (target / 'audio_service.h').read_text()


AUDIO_STUB = r'''
#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <cstdio>
#include <memory>
#include <mutex>
#include <deque>
#include <future>
#include <chrono>
static_assert(MAX_DECODE_PACKETS_IN_QUEUE == 120);
struct AudioStreamPacket { uint32_t generation_id = 1; };
std::promise<void> log_entered, release_log;
std::atomic<bool> logged{false};
#define ESP_LOGW(...) do { logged = true; log_entered.set_value(); release_log.get_future().wait(); } while (0)
struct AudioService {
 enum ServerPacketAdmit { kServerPacketAdmitted, kServerPacketInvalid,
 kServerPacketStopped, kServerPacketGenerationMismatch, kServerPacketQueueFull };
 std::mutex audio_queue_mutex_;
 std::condition_variable audio_queue_cv_;
 std::atomic<bool> service_stopped_{false};
 std::atomic<uint32_t> server_decode_queue_drops_{0};
 uint32_t accepted_server_generation_ = 1;
 bool playback_drained_notified_ = true;
 std::deque<std::unique_ptr<AudioStreamPacket>> audio_decode_queue_;
 ServerPacketAdmit PushServerPacketToDecodeQueue(std::unique_ptr<AudioStreamPacket>);
};
'''


def test_queue_admission_never_waits_for_serial(tmp_path: Path) -> None:
    source, header = load_audio_source(tmp_path)
    # Use the target board's actual frame duration and queue limit macros.
    macros = '\n'.join(re.search(rf'^#define {name} .+$', header, re.M)[0]
                       for name in ('OPUS_FRAME_DURATION_MS', 'MAX_DECODE_PACKETS_IN_QUEUE'))
    body = function(source, 'AudioService::ServerPacketAdmit AudioService::PushServerPacketToDecodeQueue(')
    output = run_cpp(tmp_path, macros + '\n' + AUDIO_STUB + body + r'''
int main() {
 AudioService a;
 auto push = [&](uint32_t generation) {
   auto p = std::make_unique<AudioStreamPacket>(); p->generation_id = generation;
   return a.PushServerPacketToDecodeQueue(std::move(p));
 };
 if (a.PushServerPacketToDecodeQueue(nullptr) != AudioService::kServerPacketInvalid ||
     push(0) != AudioService::kServerPacketInvalid ||
     push(2) != AudioService::kServerPacketGenerationMismatch) return 1;
 a.service_stopped_ = true;
 if (push(1) != AudioService::kServerPacketStopped) return 2;
 a.service_stopped_ = false;
 for (int i = 0; i < 120; ++i)
   if (push(1) != AudioService::kServerPacketAdmitted) return 3;
 auto pending = std::async(std::launch::async, [&] { return push(1); });
 bool ready = pending.wait_for(std::chrono::milliseconds(200)) == std::future_status::ready;
 // The logger blocks like a full UART. A second consumer must still get the lock.
 if (!ready && log_entered.get_future().wait_for(std::chrono::seconds(1)) !=
     std::future_status::ready) { release_log.set_value(); return 8; }
 bool lock_available = a.audio_queue_mutex_.try_lock();
 if (lock_available) a.audio_queue_mutex_.unlock();
 release_log.set_value();
 if (pending.get() != AudioService::kServerPacketQueueFull) return 4;
 std::printf("serial_called=%d consumer_lock_available=%d admission_completed=%d depth=%zu drops=%u\n",
    int(logged.load()), int(lock_available), int(ready), a.audio_decode_queue_.size(),
    a.server_decode_queue_drops_.load());
 if (!ready || !lock_available || logged) return 5;
 if (a.audio_decode_queue_.size() != 120 || a.server_decode_queue_drops_ != 1) return 6;
 if (push(2) != AudioService::kServerPacketGenerationMismatch || a.server_decode_queue_drops_ != 1) return 7;
 for (int i = 0; i < 39; ++i)
   if (push(1) != AudioService::kServerPacketQueueFull) return 9;
 if (a.server_decode_queue_drops_ != 40) return 10;
 std::puts("capacity=120 cumulative_drops=40 mismatch/stop/invalid controls passed");
}
''')
    print(output.strip())


def check_receive_priority_fence_and_no_demotion(tmp_path: Path, board: int) -> None:
    body = function(PROTOCOL.read_text(), 'void MemoriaProtocol::ObserveMediaReceiveTask(')
    output = run_cpp(tmp_path, f'#define CONFIG_BOARD_TYPE_MEMORIA_ESP_VOCAT {board}\n' + r'''
#include <atomic>
#include <cstdint>
#include <cstdio>
#include <mutex>
using UBaseType_t = unsigned;
unsigned priority = 1, sets = 0;
unsigned uxTaskPriorityGet(void*) { return priority; }
void vTaskPrioritySet(void*, unsigned next) { priority = next; ++sets; }
struct MemoriaProtocol {
 std::recursive_mutex playback_state_mutex_;
 std::atomic<uint32_t> websocket_attempt_id_{2};
 uint32_t receive_priority_attempt_ = 0;
 unsigned receive_priority_before_ = 0, receive_priority_actual_ = 0;
 bool receive_priority_pending_ = false;
 void ObserveMediaReceiveTask(uint32_t, bool);
};
''' + body + r'''
int main() {
 MemoriaProtocol p;
 p.ObserveMediaReceiveTask(1, true); // retired attempt
 p.ObserveMediaReceiveTask(2, false); // shared/other transport
 if (priority != 1 || sets || p.receive_priority_pending_) return 1;
 p.ObserveMediaReceiveTask(2, true);
#if CONFIG_BOARD_TYPE_MEMORIA_ESP_VOCAT
 if (priority != 4 || sets != 1 || p.receive_priority_actual_ != 4 ||
     p.receive_priority_before_ != 1 || !p.receive_priority_pending_) return 2;
 p.receive_priority_pending_ = false;
 p.ObserveMediaReceiveTask(2, true);
 if (sets != 1 || p.receive_priority_pending_) return 3;
 p.websocket_attempt_id_ = 3;
 priority = 7;
 p.ObserveMediaReceiveTask(2, true);
 if (sets != 1 || p.receive_priority_attempt_ != 2) return 4;
 p.ObserveMediaReceiveTask(3, true);
 if (priority != 7 || sets != 1 || p.receive_priority_actual_ != 7 ||
     p.receive_priority_attempt_ != 3 || !p.receive_priority_pending_) return 5;
#else
 if (priority != 1 || sets || p.receive_priority_pending_) return 6;
#endif
 std::printf("priority fence/high-priority/board controls passed board=%d\n",
             CONFIG_BOARD_TYPE_MEMORIA_ESP_VOCAT);
}
''')
    print(output.strip())


def test_callback_and_owner_wiring() -> None:
    source = PROTOCOL.read_text()
    callback = source[source.index('websocket->OnData('):source.index('websocket->OnPong(')]
    assert callback.index('ObserveMediaReceiveTask(') < callback.index('state_lock(')
    assert callback.index('websocket_attempt !=') < callback.index('HandleDownlink(')
    assert 'dedicated_wifi_wss' in callback
    assert 'Board::GetInstance().GetBoardType() == "wifi"' in source
    assert 'session.websocket_url.rfind("wss://", 0) == 0' in source
    poll = function(source, 'void MemoriaProtocol::PollTransportLiveness()')
    assert poll.index('media receive task attempt=') < poll.index('protocol_version_ ==')


def test_receive_priority_survives_mutex_inheritance(tmp_path: Path) -> None:
    body = function(PROTOCOL.read_text(), 'void MemoriaProtocol::ObserveMediaReceiveTask(').replace(
        'std::recursive_mutex', 'InheritingMutex'
    )
    output = run_cpp(tmp_path, '#define CONFIG_BOARD_TYPE_MEMORIA_ESP_VOCAT 1\n' + r'''
#include <atomic>
#include <cstdint>
#include <cstdio>
#include <mutex>
using UBaseType_t = unsigned;
unsigned base_priority = 1;
bool inherited = false;
unsigned uxTaskPriorityGet(void*) { return inherited ? 7 : base_priority; }
void vTaskPrioritySet(void*, unsigned next) { base_priority = next; }
struct InheritingMutex {
 void lock() { inherited = true; }
 void unlock() { inherited = false; }
};
struct MemoriaProtocol {
 InheritingMutex playback_state_mutex_;
 std::atomic<uint32_t> websocket_attempt_id_{2};
 uint32_t receive_priority_attempt_ = 0;
 unsigned receive_priority_before_ = 0, receive_priority_actual_ = 0;
 bool receive_priority_pending_ = false;
 void ObserveMediaReceiveTask(uint32_t, bool);
};
''' + body + r'''
int main() {
 MemoriaProtocol p;
 p.ObserveMediaReceiveTask(2, true);
 std::printf("priority after state mutex released=%u\n", base_priority);
 return base_priority == 4 ? 0 : 1;
}
''')
    print(output.strip())


def test_receive_priority_target_board(tmp_path: Path) -> None:
    check_receive_priority_fence_and_no_demotion(tmp_path, 1)


def test_receive_priority_other_board(tmp_path: Path) -> None:
    check_receive_priority_fence_and_no_demotion(tmp_path, 0)


if __name__ == '__main__':
    # The IDF build environment intentionally has no pytest dependency.
    with TemporaryDirectory(prefix='memoria-media-realtime-') as directory:
        temporary = Path(directory)
        for check in (test_queue_admission_never_waits_for_serial,
                      test_receive_priority_target_board,
                      test_receive_priority_other_board,
                      test_receive_priority_survives_mutex_inheritance):
            workspace = temporary / check.__name__
            workspace.mkdir()
            check(workspace)
        test_callback_and_owner_wiring()
    print('media realtime host checks passed')
