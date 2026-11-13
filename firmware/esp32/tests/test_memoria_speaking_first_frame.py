"""Execute applied Speaking and decoder reset functions with host boundaries."""
from __future__ import annotations

import os
import re
import runpy
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).parents[1]
HELPERS = runpy.run_path(str(ROOT / 'tests/test_memoria_media_realtime.py'))
function, run_cpp = HELPERS['function'], HELPERS['run_cpp']


def test_first_frame(tmp_path: Path) -> None:
    upstream = Path(os.environ.get('MEMORIA_ESP32_UPSTREAM_DIR', ROOT / '.cache/upstream'))
    locked = re.search(r'^UPSTREAM_REF=(.+)$', (ROOT / 'upstream.lock').read_text(), re.MULTILINE)
    assert locked is not None
    actual = subprocess.check_output(['git', '-C', str(upstream), 'rev-parse', 'HEAD'], text=True).strip()
    assert actual == locked.group(1), 'host fixture must use the pinned upstream revision'
    # Build the fixture from pinned Git objects and every current patch, rather
    # than trusting an already-generated cache from a different candidate.
    fixture = tmp_path / 'overlay'
    paths = ('main/audio/audio_service.cc', 'main/audio/audio_service.h', 'main/application.cc')
    for relative in paths:
        target = fixture / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(subprocess.check_output(
            ['git', '-C', str(upstream), 'show', f'HEAD:{relative}']))
    for patch in sorted((ROOT / 'overlay/patches').glob('*.patch')):
        subprocess.run(['git', 'apply', '--unsafe-paths',
                        *(f'--include={relative}' for relative in paths), str(patch)],
                       cwd=fixture, check=True, capture_output=True)
    audio = (fixture / 'main/audio/audio_service.cc').read_text()
    app = (fixture / 'main/application.cc').read_text()
    protocol = (ROOT / 'overlay/files/main/memoria/memoria_protocol.cc').read_text()
    started = function(protocol, 'bool MemoriaProtocol::HandleGenerationStarted(')
    assert 'on_local_flush_requested_(fence_.generation_id);' in started
    assert 'Schedule(' not in started
    downlink = function(protocol, 'bool MemoriaProtocol::HandleDownlink(')
    assert downlink.index('EmitLegacyTts("start")') < downlink.index('on_incoming_audio_(')
    flush = app.split('->SetLocalFlushCallback(', 1)[1].split('});', 1)[0]
    assert 'ResetDecoderForServerGeneration(generation_id)' in flush
    assert 'Schedule(' not in flush
    receive = app.split('protocol_->OnIncomingAudio(', 1)[1].split('#else', 1)[0]
    assert 'PushServerPacketToDecodeQueue' in receive and 'Schedule(' not in receive
    speaking = app.split('case kDeviceStateSpeaking:', 1)[1].split('break;', 1)[0]
    methods = '\n'.join(function(audio, signature) for signature in (
        'bool AudioService::ResetDecoderLocked(',
        'void AudioService::ResetDecoder(',
        'void AudioService::ResetDecoderForServerGeneration(',
        'AudioService::ServerPacketAdmit AudioService::PushServerPacketToDecodeQueue(',
    ))
    header = (fixture / 'main/audio/audio_service.h').read_text()
    declaration = re.search(r'void ResetDecoder\([^;]+;', header)[0]
    for board in (1, 0):
        source = r'''
#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <cstdio>
#include <memory>
#include <mutex>
#include <deque>
#include <functional>
#define MAX_DECODE_PACKETS_IN_QUEUE 120
namespace memoria {
struct SupplyEpisodeSummary {};
enum class SupplyCloseReason { kChannelFlush };
}
int MemoriaSupplyNowMs() { return 0; }
void MemoriaLogSupplySummary(memoria::SupplyEpisodeSummary) {}
void esp_opus_dec_reset(void*) {}
struct Meter {
 memoria::SupplyEpisodeSummary NoteDecoderReset(uint32_t, uint32_t, int) { return {}; }
 memoria::SupplyEpisodeSummary NoteGenerationAnnounced(uint32_t, uint32_t, int) { return {}; }
 memoria::SupplyEpisodeSummary Close(memoria::SupplyCloseReason, int) { return {}; }
};
struct AudioStreamPacket { uint32_t generation_id = 1; int seq = 0; };
struct AudioService {
 enum ServerPacketAdmit { kServerPacketAdmitted, kServerPacketInvalid,
 kServerPacketStopped, kServerPacketGenerationMismatch, kServerPacketQueueFull };
 std::mutex audio_queue_mutex_, decoder_mutex_;
 std::condition_variable audio_queue_cv_;
 std::atomic<bool> service_stopped_{false};
 std::atomic<uint32_t> server_decode_queue_drops_{0};
 uint32_t accepted_server_generation_ = 0, playback_generation_ = 0;
 bool playback_drained_notified_ = false, exact_output_pending_ = false;
 void* opus_decoder_ = nullptr;
 std::deque<std::unique_ptr<AudioStreamPacket>> audio_decode_queue_;
 std::deque<int> timestamp_queue_, audio_playback_queue_, audio_testing_queue_;
 Meter playback_supply_meter_;
 struct { std::function<void()> on_playback_drained; } callbacks_;
 bool MarkPlaybackDrainedLocked() { return false; }
 bool ResetDecoderLocked(uint32_t);
 RESET_DECL
 void ResetDecoderForServerGeneration(uint32_t);
 ServerPacketAdmit PushServerPacketToDecodeQueue(std::unique_ptr<AudioStreamPacket>);
 void EnableVoiceProcessing(bool) {}
 void EnableWakeWordDetection(bool) {}
 bool IsAfeWakeWord() { return false; }
};
'''.replace('RESET_DECL', declaration)
        source += methods
        source += r'''
namespace Lang { namespace Strings { const char* SPEAKING = "speaking"; } }
struct Display { void SetStatus(const char*) {} };
struct Application {
 AudioService audio_service_;
 Display screen;
 Display* display = &screen;
 int listening_mode_ = 0;
 static constexpr int kListeningModeRealtime = 1;
 void ConfigureStopKeywordForSpeaking() {}
 void Speaking() { SPEAKING_BODY }
};
int main() {
 Application app;
 auto& a = app.audio_service_;
 auto push = [&](uint32_t generation) {
  auto p = std::make_unique<AudioStreamPacket>(); p->generation_id = generation;
  return a.PushServerPacketToDecodeQueue(std::move(p));
 };
 for (int mode : {0, 1}) {
  app.listening_mode_ = mode;
  a.ResetDecoderForServerGeneration(7);
  if (push(7) != AudioService::kServerPacketAdmitted) return 1;
  const auto fence = a.playback_generation_;
  app.Speaking();
  const auto queued = a.audio_decode_queue_.size();
  // Host codec boundary consumes each admitted packet and records its seq.
  int outputs = 0, seq = -1;
  while (!a.audio_decode_queue_.empty()) {
   seq = a.audio_decode_queue_.front()->seq;
   a.audio_decode_queue_.pop_front(); ++outputs;
  }
  std::printf("board=%d mode=%d seq0_queued=%zu output_frames=%d output_seq=%d fence_delta=%u\n",
   CONFIG_BOARD_TYPE_MEMORIA_ESP_VOCAT, mode, queued, outputs, seq,
   a.playback_generation_ - fence);
#if CONFIG_BOARD_TYPE_MEMORIA_ESP_VOCAT
  if (queued != 1 || outputs != 1 || seq != 0 || a.playback_generation_ != fence) return 2;
#else
  if (queued || outputs || a.playback_generation_ != fence + 1) return 3;
#endif
 }
 // Explicit generation flush (including same generation), stop/cancel and local reset.
 for (uint32_t next : {7u, 8u, 0u}) {
  a.ResetDecoderForServerGeneration(7); push(7);
  a.audio_playback_queue_.push_back(0); a.exact_output_pending_ = true;
  const auto fence = a.playback_generation_;
  a.ResetDecoderForServerGeneration(next);
  if (!a.audio_decode_queue_.empty() || !a.audio_playback_queue_.empty() ||
      a.exact_output_pending_ || a.playback_generation_ != fence + 1 ||
      a.accepted_server_generation_ != next) return 4;
  if (next != 7 && push(7) != AudioService::kServerPacketGenerationMismatch) return 5;
 }
 a.audio_playback_queue_.push_back(42);
 app.Speaking();
 if (!a.audio_playback_queue_.empty()) return 6;
 a.ResetDecoderForServerGeneration(7); push(7);
 a.ResetDecoder();
 if (!a.audio_decode_queue_.empty()) return 7;
 std::puts("generation/same-generation/stop/cancel/local/default-reset controls passed");
}
'''.replace('SPEAKING_BODY', speaking)
        print(run_cpp(tmp_path, f'#define CONFIG_BOARD_TYPE_MEMORIA_ESP_VOCAT {board}\n' + source))


if __name__ == '__main__':
    with TemporaryDirectory(prefix='memoria-first-frame-') as directory:
        test_first_frame(Path(directory))
