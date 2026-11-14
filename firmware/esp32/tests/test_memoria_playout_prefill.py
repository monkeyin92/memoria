"""Run the applied output task against bounded transport jitter and cancellation."""
from __future__ import annotations

import os
import re
import runpy
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
HELPERS = runpy.run_path(str(ROOT / 'tests/test_memoria_media_realtime.py'))

STUB = r'''
#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <deque>
#include <functional>
#include <memory>
#include <mutex>
#include <optional>
#include <thread>
#include <vector>
#include "METER_HEADER"
static void MemoriaLogSupplyWait(const memoria::SupplyWaitRecord&) {}
static void MemoriaLogSupplySummary(const memoria::SupplyEpisodeSummary&) {}
static void esp_opus_dec_reset(void*) {}
using Clock = std::chrono::steady_clock;
static int64_t MemoriaSupplyNowMs() {
 return std::chrono::duration_cast<std::chrono::milliseconds>(Clock::now().time_since_epoch()).count();
}
#define ESP_LOGW(...) do {} while(0)
#define CONFIG_USE_SERVER_AEC 0
#define AUDIO_CODEC_DMA_DESC_NUM 6
#define AUDIO_POWER_CHECK_INTERVAL_MS 1000
static void esp_timer_stop(int) {}
static void esp_timer_start_periodic(int, int) {}
namespace memoria {
struct Tap {};
[[maybe_unused]] static Tap output_level_tap;
[[maybe_unused]] static void PublishPcm(Tap&, const int16_t*, size_t, int) {}
[[maybe_unused]] static void WritePlay(const int16_t*, size_t) {}
}
struct Task {
 uint32_t generation_id=1, timestamp=0, received_sequence=0;
 int frame_duration=20;
 uint64_t sample_end=0;
 std::vector<int16_t> pcm;
};
struct Codec {
 Clock::time_point origin, next_output_at;
 bool exact=false, stalled_counter=false;
 std::vector<int64_t> writes;
 std::vector<int> values;
 std::function<void()> on_write;
 bool output_enabled() const { return true; }
 void EnableOutput(bool) {}
 int output_sample_rate() const { return 24000; }
 uint64_t OutputCompletionCounter() const { return stalled_counter ? 0 : std::chrono::duration_cast<std::chrono::milliseconds>(Clock::now()-origin).count()/10; }
 bool HasExactOutputCompletion() const { return exact; }
 void OutputData(const std::vector<int16_t>& pcm) {
  const auto at=Clock::now();
  if(next_output_at.time_since_epoch().count()==0 || at-next_output_at>std::chrono::milliseconds(60)) next_output_at=at;
  writes.push_back(std::chrono::duration_cast<std::chrono::milliseconds>(at-origin).count());
  values.push_back(pcm.at(0));
  next_output_at+=std::chrono::milliseconds(20);
  std::this_thread::sleep_until(next_output_at);
  if(on_write) on_write();
 }
};
struct AudioService {
 std::mutex audio_queue_mutex_, decoder_mutex_;
 void* opus_decoder_=nullptr;
 std::deque<int> timestamp_queue_;
 std::deque<std::unique_ptr<Task>> audio_testing_queue_;
 bool decode_in_flight_=false, playback_drained_notified_=false;
 std::condition_variable audio_queue_cv_;
 std::deque<std::unique_ptr<Task>> audio_playback_queue_, audio_decode_queue_;
 std::atomic<bool> service_stopped_{false};
 uint32_t playback_generation_=1, accepted_server_generation_=1;
 std::optional<uint32_t> server_playout_prefill_epoch_;
 bool output_in_flight_=false, exact_output_pending_=false;
 uint64_t exact_output_target_=0, exact_output_sample_end_=0;
 uint32_t exact_output_generation_=0, exact_output_sequence_=0;
 Clock::time_point exact_output_started_, last_output_time_;
 int audio_power_timer_=0;
 struct { int playback_count=0; } debug_statistics_;
 struct {
  std::function<void(uint32_t,uint64_t,uint32_t,bool)> on_playback_output;
  std::function<void()> on_playback_drained;
 } callbacks_;
 memoria::PlaybackSupplyMeter playback_supply_meter_;
 Codec* codec_;
 bool IsPlaybackDrainedLocked() const;
 bool MarkPlaybackDrainedLocked();
 bool ResetDecoderLocked(uint32_t accepted_server_generation);
 void ResetDecoder(bool preserve_server_generation);
 bool WaitForServerPlayoutPrefillLocked(std::unique_lock<std::mutex>& lock);
 void AudioOutputTask();
 void Push(int value, uint32_t generation=1) {
  std::lock_guard<std::mutex> lock(audio_queue_mutex_);
  auto t=std::make_unique<Task>(); t->pcm.assign(480,static_cast<int16_t>(value));
  t->generation_id=generation; audio_decode_queue_.push_back(std::move(t)); audio_queue_cv_.notify_all();
 }
 void DecodeLoop() {
  std::unique_lock<std::mutex> lock(audio_queue_mutex_);
  while(!service_stopped_) {
   audio_queue_cv_.wait(lock,[&] {return service_stopped_ || (!audio_decode_queue_.empty() && audio_playback_queue_.size()<8);});
   if(service_stopped_) break;
   audio_playback_queue_.push_back(std::move(audio_decode_queue_.front()));
   audio_decode_queue_.pop_front(); playback_drained_notified_=false; audio_queue_cv_.notify_all();
  }
 }
 void Stop() {
  std::lock_guard<std::mutex> lock(audio_queue_mutex_);
  ++playback_generation_; audio_playback_queue_.clear(); audio_decode_queue_.clear();
  service_stopped_=true; audio_queue_cv_.notify_all();
 }
};
'''

PROBE = r'''
int main() {
 Codec codec; AudioService service; service.codec_=&codec; codec.origin=Clock::now();
 codec.on_write=[&] { if(codec.values.size()==64) service.Stop(); };
 std::thread decoder([&] {service.DecodeLoop();});
 std::thread consumer([&] {service.AudioOutputTask();});
 for(int i=0;i<64;++i) {
  const int when=(i>=20 && i<=33) ? 660 : i*20;
  std::this_thread::sleep_until(codec.origin+std::chrono::milliseconds(when));
  service.Push(i);
 }
 consumer.join(); decoder.join();
 int64_t max_gap=0;
 for(size_t i=1;i<codec.writes.size();++i) max_gap=std::max(max_gap,codec.writes[i]-codec.writes[i-1]);
 printf("board=%d frames=%zu first_ms=%lld max_gap_ms=%lld\n",CONFIG_BOARD_TYPE_MEMORIA_ESP_VOCAT,
  codec.values.size(),static_cast<long long>(codec.writes.at(0)),static_cast<long long>(max_gap));
 if(codec.values.size()!=64) return 1;
 for(int i=0;i<64;++i) if(codec.values.at(i)!=i) return 2;
#if CONFIG_BOARD_TYPE_MEMORIA_ESP_VOCAT
 if(codec.writes.at(0)<250 || codec.writes.at(0)>1000 || max_gap>=100) return 3;
#else
 if(codec.writes.at(0)>=200 || max_gap<200) return 4;
#endif
 Codec local_codec; AudioService local; local.codec_=&local_codec; local_codec.origin=Clock::now();
 local_codec.on_write=[&] {local.Stop();}; local.Push(99,0);
 std::thread local_decoder([&] {local.DecodeLoop();});
 local.AudioOutputTask(); local_decoder.join();
 if(local_codec.writes.at(0)>=200 || local_codec.values.at(0)!=99) return 5;
 Codec cancel_codec; AudioService cancelled; cancelled.codec_=&cancel_codec;
 cancel_codec.origin=Clock::now(); cancelled.Push(42);
 std::thread cancel_decoder([&] {cancelled.DecodeLoop();});
 std::thread pending([&] {cancelled.AudioOutputTask();});
 std::this_thread::sleep_for(std::chrono::milliseconds(50));
 const auto stop_at=Clock::now(); cancelled.Stop(); pending.join(); cancel_decoder.join();
 auto stop_ms=std::chrono::duration_cast<std::chrono::milliseconds>(Clock::now()-stop_at).count();
#if CONFIG_BOARD_TYPE_MEMORIA_ESP_VOCAT
 if(!cancel_codec.values.empty() || stop_ms>=150) return 6;
#endif
 printf("local_first_ms=%lld cancel_outputs=%zu stop_ms=%lld\n",
  static_cast<long long>(local_codec.writes.at(0)),cancel_codec.values.size(),static_cast<long long>(stop_ms));

#if CONFIG_BOARD_TYPE_MEMORIA_ESP_VOCAT
 // Same server id with a new local epoch must cancel the old first frame.
 for(uint32_t next_generation: {1u,2u}) {
  Codec c; AudioService s; s.codec_=&c; c.origin=Clock::now(); s.Push(42);
  c.on_write=[&] {s.Stop();};
  std::thread decode([&] {s.DecodeLoop();}), output([&] {s.AudioOutputTask();});
  std::this_thread::sleep_for(std::chrono::milliseconds(50));
  { std::lock_guard<std::mutex> guard(s.audio_queue_mutex_); s.ResetDecoderLocked(next_generation); }
  s.Push(43,next_generation); output.join(); decode.join();
  if(c.values!=std::vector<int>{43} || c.writes.at(0)<340) return 7;
 }
 // Preserve Speaking reset and spurious notifications cannot restart the deadline.
 Codec short_c; AudioService short_s; short_s.codec_=&short_c; short_c.origin=Clock::now(); short_s.Push(71);
 short_c.on_write=[&] {short_s.Stop();};
 std::thread short_decode([&] {short_s.DecodeLoop();}), short_output([&] {short_s.AudioOutputTask();});
 std::this_thread::sleep_for(std::chrono::milliseconds(50)); short_s.ResetDecoder(true);
 std::thread noise([&] {while(!short_s.service_stopped_) {
  short_s.audio_queue_cv_.notify_all(); std::this_thread::sleep_for(std::chrono::milliseconds(2));
 }});
 short_output.join(); short_decode.join(); noise.join();
 if(short_c.values!=std::vector<int>{71} || short_c.writes.at(0)<300 || short_c.writes.at(0)>650) return 8;
 // PCM capacity stays eight: 8+8 valid queued frames may release early;
 // mixed local packets and an in-flight decoder are not extra ready frames.
 for(int mode=0;mode<3;++mode) {
  Codec c; AudioService s; s.codec_=&c; c.origin=Clock::now();
  auto add=[&](auto& queue,int generation) {
   auto t=std::make_unique<Task>();t->generation_id=generation;
   t->pcm.assign(480,1);t->frame_duration=generation ? 20 : 60;queue.push_back(std::move(t));
  };
  for(int i=0;i<8;++i) add(s.audio_playback_queue_,(mode==1 && i>0) ? 0 : 1);
  for(int i=0;i<(mode==2 ? 7 : 8);++i) add(s.audio_decode_queue_,mode==1 ? 0 : 1);
  s.decode_in_flight_=mode==2;
  c.on_write=[&] {s.Stop();};s.AudioOutputTask();
  if((mode==0 && c.writes.at(0)>=150) || (mode!=0 && c.writes.at(0)<300)) return 9;
 }
 // A 15-frame finished stream cannot wait forever for frame sixteen.
 Codec fifteen_c; AudioService fifteen_s; fifteen_s.codec_=&fifteen_c;fifteen_c.origin=Clock::now();
 for(int i=0;i<15;++i) fifteen_s.Push(i);
 fifteen_c.on_write=[&] {if(fifteen_c.values.size()==15) fifteen_s.Stop();};
 std::thread fifteen_decode([&] {fifteen_s.DecodeLoop();});fifteen_s.AudioOutputTask();fifteen_decode.join();
 if(fifteen_c.values.size()!=15 || fifteen_c.writes.at(0)<300) return 10;
 for(int i=0;i<15;++i) if(fifteen_c.values.at(i)!=i) return 11;
 // The real output loop keeps submission -> exact completion -> drained order.
 for(bool stall: {false,true}) {
  Codec c; AudioService s;s.codec_=&c;c.origin=Clock::now();c.exact=true;c.stalled_counter=stall;
  std::vector<int> receipts;
  s.callbacks_.on_playback_output=[&](uint32_t,uint64_t,uint32_t,bool committed) {receipts.push_back(committed ? 1 : 2);};
  s.callbacks_.on_playback_drained=[&] {receipts.push_back(3);s.Stop();};
  s.Push(1);std::thread decode([&] {s.DecodeLoop();});s.AudioOutputTask();decode.join();
  const auto expected=stall ? std::vector<int>{1,3} : std::vector<int>{1,2,3};
  if(receipts!=expected || c.values.size()!=1) return 12;
 }
 puts("epoch reset/same-id flush/preserve/short/mixed/inflight/EOS/completion controls passed");
#endif
}
'''


def test_server_playout_prefill(tmp_path: Path) -> None:
    upstream = Path(os.environ.get('MEMORIA_ESP32_UPSTREAM_DIR', ROOT / '.cache/upstream'))
    locked = re.search(r'^UPSTREAM_REF=(.+)$', (ROOT / 'upstream.lock').read_text(), re.MULTILINE)
    assert locked is not None
    actual = subprocess.check_output(['git', '-C', str(upstream), 'rev-parse', 'HEAD'], text=True).strip()
    assert actual == locked.group(1)
    cc, _ = HELPERS['load_audio_source'](tmp_path)
    output = HELPERS['function'](cc, 'void AudioService::AudioOutputTask()')
    for signature in ('bool AudioService::ResetDecoderLocked(', 'void AudioService::ResetDecoder(',
                      'bool AudioService::IsPlaybackDrainedLocked()', 'bool AudioService::MarkPlaybackDrainedLocked()'):
        output = HELPERS['function'](cc, signature) + '\n' + output
    signature = 'bool AudioService::WaitForServerPlayoutPrefillLocked('
    prefill = HELPERS['function'](cc, signature) if signature in cc else ''
    meter = ROOT / 'overlay/files/main/audio/memoria_playback_supply_meter.h'
    stub = STUB.replace('METER_HEADER', str(meter))
    for board in (1, 0):
        source = f'#define CONFIG_BOARD_TYPE_MEMORIA_ESP_VOCAT {board}\n' + stub + prefill + output + PROBE
        print(HELPERS['run_cpp'](tmp_path, source))


if __name__ == '__main__':
    with TemporaryDirectory() as folder:
        test_server_playout_prefill(Path(folder))
