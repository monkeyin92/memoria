# P1-1 实现 AEC Reference 对齐

## 目标

实现 ESP32 音频前端（AFE）的 AEC Reference 对齐，使设备能够在播放时采集到干净的近端语音，支持全双工对话。

## 背景

根据整改方案第 9 节，AEC（Acoustic Echo Cancellation，回声消除）是全双工对话的关键技术。当设备扬声器播放 AI 回复时，麦克风会同时采集到：
- **近端信号**（用户语音）- 需要保留
- **远端回声**（扬声器播放声音的回声）- 需要消除

AEC 需要一个**参考信号**（Reference）来识别和消除回声。参考信号必须与实际扬声器播放的信号精确对齐。

## 当前状态

根据整改方案 9.3 节，当前板型存在以下问题：

1. **Reference tap 位置缺失**：没有在正确位置提取播放参考信号
2. **采样率不一致**：上行 16 kHz，下行可能是 24 kHz
3. **时间对齐缺失**：Reference 与 Mic 没有统一的样本计数和延迟校准

## 实施方案

### 1. 确定 Reference Tap 位置

**目标位置**（整改方案 9.3）：

```text
服务器 Opus
→ Media Edge Decode
→ ESP32 下行 Opus Decode
→ PCM 16/24 kHz
→ 用户音量缩放（数字）
→ [**在这里复制给 AEC Reference**]  ← 关键位置
→ I2S DMA
→ ES8388 DAC
→ PA/扬声器
```

**关键约束**：
- AEC Reference 之后不要再做不可知的数字音量变化
- Codec/PA 模拟增益尽量固定
- 用户音量优先在 Reference tap 之前数字处理

### 2. 固件实现步骤

#### 步骤 2.1：统一采样率

**问题**：上行 16 kHz，下行可能 24 kHz

**解决方案**：
- 方案 A：统一为 16 kHz（简单，符合 ESP-SR AEC 要求）
- 方案 B：在 tap 点做重采样到 16 kHz

**推荐**：先固定 16 kHz 全链路

**修改位置**：
- `firmware/esp32/overlay/files/main/audio/audio_service.cc` - 播放队列配置
- `firmware/esp32/overlay/files/main/memoria/memoria_protocol.cc` - 下行解码配置

```cpp
// 确保下行播放配置为 16 kHz
constexpr int kPlaybackSampleRate = 16000;
constexpr int kPlaybackChannels = 1;
constexpr int kPlaybackBitsPerSample = 16;
```

#### 步骤 2.2：创建 Reference Buffer

**新增文件**：`firmware/esp32/overlay/files/main/audio/aec_reference.h`

```cpp
#pragma once

#include <cstdint>
#include <vector>

namespace memoria {

/**
 * AEC Reference buffer for capturing playback signal.
 * 
 * The reference signal must be tapped AFTER user volume scaling
 * but BEFORE I2S DMA transmission.
 */
class AecReference {
public:
    explicit AecReference(int sample_rate, int channels);
    
    /**
     * Feed playback samples to reference buffer.
     * Called from playback pipeline after volume scaling.
     */
    void FeedPlayback(const int16_t* samples, size_t frame_count);
    
    /**
     * Read reference samples for AEC processing.
     * Must be time-aligned with microphone samples.
     */
    bool ReadReference(int16_t* out_samples, size_t frame_count);
    
    /**
     * Flush reference buffer when playback stops or interrupts.
     */
    void Flush();
    
    /**
     * Get current reference sample position for alignment.
     */
    uint64_t GetSamplePosition() const { return sample_position_; }
    
private:
    int sample_rate_;
    int channels_;
    uint64_t sample_position_;
    std::vector<int16_t> buffer_;
    size_t write_pos_;
    size_t read_pos_;
};

}  // namespace memoria
```

**新增文件**：`firmware/esp32/overlay/files/main/audio/aec_reference.cc`

```cpp
#include "audio/aec_reference.h"
#include <algorithm>
#include <cstring>
#include "esp_log.h"

namespace memoria {

static const char* TAG = "AecReference";

// Buffer size: 500ms @ 16kHz mono = 8000 samples
constexpr size_t kReferenceBufferSize = 8000;

AecReference::AecReference(int sample_rate, int channels)
    : sample_rate_(sample_rate),
      channels_(channels),
      sample_position_(0),
      buffer_(kReferenceBufferSize, 0),
      write_pos_(0),
      read_pos_(0) {
    ESP_LOGI(TAG, "AEC Reference initialized: %d Hz, %d ch", sample_rate, channels);
}

void AecReference::FeedPlayback(const int16_t* samples, size_t frame_count) {
    if (samples == nullptr || frame_count == 0) {
        return;
    }
    
    const size_t sample_count = frame_count * channels_;
    
    for (size_t i = 0; i < sample_count; ++i) {
        buffer_[write_pos_] = samples[i];
        write_pos_ = (write_pos_ + 1) % kReferenceBufferSize;
        
        // Handle overrun - advance read position
        if (write_pos_ == read_pos_) {
            read_pos_ = (read_pos_ + 1) % kReferenceBufferSize;
            ESP_LOGW(TAG, "Reference buffer overrun");
        }
    }
    
    sample_position_ += frame_count;
}

bool AecReference::ReadReference(int16_t* out_samples, size_t frame_count) {
    if (out_samples == nullptr || frame_count == 0) {
        return false;
    }
    
    const size_t sample_count = frame_count * channels_;
    size_t available = (write_pos_ >= read_pos_)
        ? (write_pos_ - read_pos_)
        : (kReferenceBufferSize - read_pos_ + write_pos_);
    
    if (available < sample_count) {
        // Not enough samples - zero-fill
        std::memset(out_samples, 0, sample_count * sizeof(int16_t));
        return false;
    }
    
    for (size_t i = 0; i < sample_count; ++i) {
        out_samples[i] = buffer_[read_pos_];
        read_pos_ = (read_pos_ + 1) % kReferenceBufferSize;
    }
    
    return true;
}

void AecReference::Flush() {
    write_pos_ = 0;
    read_pos_ = 0;
    std::fill(buffer_.begin(), buffer_.end(), 0);
    ESP_LOGI(TAG, "Reference buffer flushed");
}

}  // namespace memoria
```

#### 步骤 2.3：在播放路径插入 Reference Tap

**修改文件**：`firmware/esp32/overlay/files/main/audio/audio_service.cc`

```cpp
#include "audio/aec_reference.h"

class AudioService {
private:
    std::unique_ptr<AecReference> aec_reference_;
    
public:
    void Initialize() {
        // 初始化 AEC Reference
        aec_reference_ = std::make_unique<AecReference>(16000, 1);
    }
    
    void PlaybackTask() {
        while (true) {
            AudioFrame frame = GetNextPlaybackFrame();
            
            // 1. 应用用户音量
            ApplyVolume(frame.samples, frame.frame_count, user_volume_);
            
            // 2. **TAP POINT**: 复制给 AEC Reference
            if (aec_reference_) {
                aec_reference_->FeedPlayback(frame.samples, frame.frame_count);
            }
            
            // 3. 发送到 I2S DMA
            i2s_write(I2S_NUM_0, frame.samples, frame.size, &bytes_written, portMAX_DELAY);
        }
    }
    
    void OnPlaybackFlush() {
        // 清空播放队列时同步清空 Reference
        if (aec_reference_) {
            aec_reference_->Flush();
        }
    }
    
    AecReference* GetAecReference() { return aec_reference_.get(); }
};
```

#### 步骤 2.4：集成 ESP-SR AFE

**新增文件**：`firmware/esp32/overlay/files/main/audio/afe_pipeline.h`

```cpp
#pragma once

#include "esp_afe_sr_iface.h"
#include "esp_afe_sr_models.h"
#include "audio/aec_reference.h"

namespace memoria {

/**
 * ESP-SR Audio Front-End pipeline with AEC.
 */
class AfePipeline {
public:
    AfePipeline();
    ~AfePipeline();
    
    /**
     * Initialize AFE with AEC in full-duplex mode.
     */
    bool Initialize(AecReference* aec_reference);
    
    /**
     * Process one frame: Mic + Reference → AEC → NS → VAD → Clean output
     */
    bool ProcessFrame(
        const int16_t* mic_samples,
        size_t frame_count,
        int16_t* out_samples,
        bool* vad_triggered
    );
    
    /**
     * Reset AFE state (on reconnect, flush, etc.)
     */
    void Reset();
    
private:
    esp_afe_sr_iface_t* afe_;
    esp_afe_sr_data_t* afe_data_;
    AecReference* aec_reference_;
    
    int16_t reference_buffer_[512];  // Temporary buffer for reference
};

}  // namespace memoria
```

**新增文件**：`firmware/esp32/overlay/files/main/audio/afe_pipeline.cc`

```cpp
#include "audio/afe_pipeline.h"
#include "esp_log.h"

namespace memoria {

static const char* TAG = "AfePipeline";

AfePipeline::AfePipeline()
    : afe_(nullptr),
      afe_data_(nullptr),
      aec_reference_(nullptr) {
}

AfePipeline::~AfePipeline() {
    if (afe_ && afe_data_) {
        afe_->destroy(afe_data_);
    }
}

bool AfePipeline::Initialize(AecReference* aec_reference) {
    aec_reference_ = aec_reference;
    
    // 获取 ESP-SR AFE 接口
    afe_ = &ESP_AFE_SR_HANDLE;
    
    // 配置 AFE
    afe_config_t afe_config = {
        .aec_init = true,
        .se_init = true,
        .vad_init = true,
        .wakenet_init = false,  // 唤醒词按需启用
        .vad_mode = VAD_MODE_3,
        .wakenet_model = nullptr,
        .afe_mode = SR_MODE_LOW_COST,  // FD Low Cost
        .afe_perferred_core = 0,
        .afe_perferred_priority = 5,
        .afe_ringbuf_size = 50,
        .memory_alloc_mode = AFE_MEMORY_ALLOC_MORE_PSRAM,
        .agc_mode = AFE_MN_PEAK_AGC_MODE_2,
        .pcm_config = {
            .total_ch_num = 2,  // 1 Mic + 1 Reference
            .mic_num = 1,
            .ref_num = 1,
            .sample_rate = 16000,
        },
        .debug_init = false,
        .debug_hook = {{AFE_DEBUG_HOOK_MASE_TASK_IN, nullptr}, {AFE_DEBUG_HOOK_FETCH_TASK_IN, nullptr}},
    };
    
    // 创建 AFE 实例
    afe_data_ = afe_->create_from_config(&afe_config);
    if (afe_data_ == nullptr) {
        ESP_LOGE(TAG, "Failed to create AFE instance");
        return false;
    }
    
    ESP_LOGI(TAG, "AFE initialized with AEC in FD mode");
    return true;
}

bool AfePipeline::ProcessFrame(
    const int16_t* mic_samples,
    size_t frame_count,
    int16_t* out_samples,
    bool* vad_triggered
) {
    if (afe_ == nullptr || afe_data_ == nullptr || aec_reference_ == nullptr) {
        return false;
    }
    
    // 1. 获取时间对齐的 Reference 样本
    if (!aec_reference_->ReadReference(reference_buffer_, frame_count)) {
        // Reference 不足，使用静音
        std::memset(reference_buffer_, 0, frame_count * sizeof(int16_t));
    }
    
    // 2. 喂给 AFE：Mic + Reference
    int afe_chunk_size = afe_->get_feed_chunksize(afe_data_);
    
    // 交错排列：[M0, R0, M1, R1, ...]
    int16_t interleaved[afe_chunk_size * 2];
    for (int i = 0; i < afe_chunk_size; ++i) {
        interleaved[i * 2] = mic_samples[i];
        interleaved[i * 2 + 1] = reference_buffer_[i];
    }
    
    afe_->feed(afe_data_, interleaved);
    
    // 3. 获取 AEC 处理后的输出
    int fetch_size = afe_->get_fetch_chunksize(afe_data_);
    int16_t* fetched = afe_->fetch(afe_data_);
    
    if (fetched) {
        std::memcpy(out_samples, fetched, fetch_size * sizeof(int16_t));
        
        // 4. 检查 VAD
        if (vad_triggered) {
            *vad_triggered = afe_->get_vad_state(afe_data_);
        }
        
        return true;
    }
    
    return false;
}

void AfePipeline::Reset() {
    if (afe_ && afe_data_) {
        afe_->reset(afe_data_);
        ESP_LOGI(TAG, "AFE reset");
    }
}

}  // namespace memoria
```

### 3. 集成到 Memoria Protocol

**修改文件**：`firmware/esp32/overlay/files/main/memoria/memoria_protocol.cc`

在采集任务中集成 AFE：

```cpp
#include "audio/afe_pipeline.h"

class MemoriaProtocol {
private:
    std::unique_ptr<AfePipeline> afe_pipeline_;
    
public:
    void Initialize() {
        // 初始化 AFE
        afe_pipeline_ = std::make_unique<AfePipeline>();
        
        // 传递 AudioService 的 Reference
        AecReference* reference = audio_service_->GetAecReference();
        if (!afe_pipeline_->Initialize(reference)) {
            ESP_LOGE(TAG, "Failed to initialize AFE pipeline");
        }
    }
    
    void CaptureTask() {
        int16_t mic_raw[512];
        int16_t mic_clean[512];
        bool vad_triggered = false;
        
        while (true) {
            // 1. 读取原始麦克风数据
            i2s_read(I2S_NUM_1, mic_raw, sizeof(mic_raw), &bytes_read, portMAX_DELAY);
            
            // 2. AFE 处理：Mic + Reference → AEC + NS + VAD
            if (afe_pipeline_->ProcessFrame(mic_raw, 512, mic_clean, &vad_triggered)) {
                // 3. 使用干净的音频进行编码和上传
                EncodeAndSendAudio(mic_clean, 512);
                
                // 4. VAD 状态用于打断判断
                if (vad_triggered) {
                    OnVadTriggered();
                }
            }
        }
    }
    
    void OnLocalFlush() {
        // 播放打断时重置 AFE 状态
        if (afe_pipeline_) {
            afe_pipeline_->Reset();
        }
    }
};
```

### 4. 时间对齐验证

**目标**：确保 Reference 与 Mic 在时间上精确对齐

**验证方法**：

1. **播放已知信号**（如 1 kHz 正弦波）
2. **同时采集 Mic 和 Reference**
3. **计算互相关**找到延迟
4. **调整 Reference buffer 延迟**补偿

**诊断工具**：

```cpp
// 新增文件：firmware/esp32/overlay/files/main/audio/aec_calibration.h

class AecCalibration {
public:
    /**
     * Measure delay between playback and reference.
     * Returns delay in samples.
     */
    static int MeasureReferenceDelay(
        AudioService* audio_service,
        AfePipeline* afe_pipeline
    );
    
    /**
     * Play test tone and capture for alignment verification.
     */
    static bool RunAlignmentTest(
        int expected_delay_samples,
        float* measured_correlation
    );
};
```

## 验收标准

### 功能验收

✅ Reference buffer 成功从播放路径 tap 信号  
✅ AFE 初始化成功并配置为 FD Low Cost 模式  
✅ Mic 和 Reference 时间对齐误差 < 10 samples (@16kHz = 0.625ms)  
✅ AEC 输出无明显削弱（近端 only 场景）  
✅ 播放 flush 时 Reference 同步清空  

### 性能验收

✅ Far-end only：扬声器播放时 VAD 不误触发（< 5%）  
✅ Near-end only：用户说话时识别率无下降  
✅ Double-talk：播放时用户插话能被正确采集  
✅ CPU 占用：AFE + Opus + LVGL 总占用 < 80%  
✅ 内存占用：堆/PSRAM 稳定，无泄漏  
✅ 看门狗：连续运行 1 小时无 watchdog 触发  

### 声学验收矩阵

按整改方案 9.5 节要求：

| 场景 | 验收标准 |
|---|---|
| Far-end only @ 50% volume | VAD 误触发率 < 5% |
| Near-end only @ 1m | ASR 字错率无明显上升 |
| Double-talk @ 1m, 50% vol | 近端语音可识别，ASR 字错率 < 20% |
| 音量范围 | 20%、50%、80% 均通过上述测试 |
| 距离 | 0.5m、1m、2m 测试 |

## 下一步

完成 P1-1 后，继续：

- **P1-2**：实现本地停止词检测
- **P1-3**：实现语义打断分类（区分附和、停止词、新问题）
- **P1-4**：AEC 声学矩阵完整验收

## 参考

- 整改方案第 9 节：全双工声学前端
- ESP-SR AEC 文档：https://docs.espressif.com/projects/esp-sr/en/latest/esp32s3/acoustic_echo_cancellation/README.html
- ESP-SR AFE 文档：https://docs.espressif.com/projects/esp-sr/en/latest/esp32s3/audio_front_end/README.html
