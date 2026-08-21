# P1-2 实现本地停止词

## 目标

在 ESP32 固件中实现本地停止词检测，使用户可以通过说"停一下"、"等等"等关键词立即中断 AI 播放，无需等待服务端确认。

## 背景

根据整改方案，打断方式分为三类：
1. **物理按钮**：最快，< 150ms 本地响应（P0-3 已实现）
2. **本地停止词**：快速，< 300-500ms，本项任务
3. **语义打断**：较慢，需要 ASR + 服务端判断，< 700ms（P1-3）

本地停止词的优势：
- 不需要完整 ASR 和服务端判断
- 响应速度介于物理按钮和语义打断之间
- 用户体验自然（语音比按钮更自然）

## 技术选型

### 方案 A：ESP-SR WakeNet（推荐）

**优点**：
- Espressif 官方支持
- 已集成在 ESP-SR 库中
- 可与 AFE 管道无缝集成
- 支持自定义唤醒词
- 资源占用可控

**缺点**：
- 需要训练自定义模型（"停一下"、"等等"）
- 可能需要 Espressif 技术支持

### 方案 B：简单能量+ZCR 检测

**优点**：
- 实现简单
- 资源占用极低

**缺点**：
- 误触发率高
- 准确率低
- 不适合生产环境

**推荐**：使用方案 A（ESP-SR WakeNet）

## 实施方案

### 1. 停止词列表

定义需要支持的停止词：

```cpp
// 优先级 P0（必须支持）
"停"
"停一下"
"等等"
"暂停"

// 优先级 P1（推荐支持）
"别说了"
"不要说了"
"够了"
"安静"

// 优先级 P2（可选）
"闭嘴"
"停下来"
```

### 2. 固件实现

#### 步骤 2.1：集成 WakeNet

**新增文件**：`firmware/esp32/overlay/files/main/audio/stop_keyword.h`

```cpp
#pragma once

#include <functional>
#include "esp_wn_iface.h"
#include "esp_wn_models.h"

namespace memoria {

/**
 * Local stop keyword detector using ESP-SR WakeNet.
 * 
 * Detects stop phrases like "停一下", "等等" without server roundtrip.
 */
class StopKeywordDetector {
public:
    using StopCallback = std::function<void(const char* keyword)>;
    
    StopKeywordDetector();
    ~StopKeywordDetector();
    
    /**
     * Initialize detector with custom stop keywords model.
     */
    bool Initialize();
    
    /**
     * Process audio frame for stop keyword detection.
     * Should be called with clean audio AFTER AEC.
     * 
     * Returns true if stop keyword detected.
     */
    bool ProcessFrame(const int16_t* samples, size_t frame_count);
    
    /**
     * Set callback for stop keyword detection.
     */
    void SetStopCallback(StopCallback callback) { stop_callback_ = callback; }
    
    /**
     * Enable/disable stop keyword detection.
     * Should be disabled when not playing.
     */
    void SetEnabled(bool enabled) { enabled_ = enabled; }
    
    bool IsEnabled() const { return enabled_; }
    
    /**
     * Reset detector state.
     */
    void Reset();
    
private:
    esp_wn_iface_t* wakenet_;
    model_iface_data_t* model_data_;
    StopCallback stop_callback_;
    bool enabled_;
    
    // Keyword IDs
    static constexpr int kKeywordStop = 0;        // "停"
    static constexpr int kKeywordStopWait = 1;    // "停一下"
    static constexpr int kKeywordWait = 2;        // "等等"
    static constexpr int kKeywordPause = 3;       // "暂停"
};

}  // namespace memoria
```

**新增文件**：`firmware/esp32/overlay/files/main/audio/stop_keyword.cc`

```cpp
#include "audio/stop_keyword.h"
#include "esp_log.h"
#include <cstring>

namespace memoria {

static const char* TAG = "StopKeyword";

// Keyword string mapping
static const char* kKeywordStrings[] = {
    "停",
    "停一下",
    "等等",
    "暂停",
};

StopKeywordDetector::StopKeywordDetector()
    : wakenet_(nullptr),
      model_data_(nullptr),
      stop_callback_(nullptr),
      enabled_(false) {
}

StopKeywordDetector::~StopKeywordDetector() {
    if (wakenet_ && model_data_) {
        wakenet_->destroy(model_data_);
    }
}

bool StopKeywordDetector::Initialize() {
    // TODO: 需要训练自定义停止词模型
    // 当前使用默认 WakeNet 模型作为示例
    
    wakenet_ = &WAKENET_MODEL;
    if (wakenet_ == nullptr) {
        ESP_LOGE(TAG, "WakeNet interface not available");
        return false;
    }
    
    model_coeff_getter_t* model_coeff = wakenet_->get_coeff();
    if (model_coeff == nullptr) {
        ESP_LOGE(TAG, "Failed to get model coefficients");
        return false;
    }
    
    model_data_ = wakenet_->create(model_coeff, DET_MODE_90);
    if (model_data_ == nullptr) {
        ESP_LOGE(TAG, "Failed to create WakeNet model");
        return false;
    }
    
    ESP_LOGI(TAG, "Stop keyword detector initialized");
    ESP_LOGI(TAG, "Supported keywords: 停, 停一下, 等等, 暂停");
    
    return true;
}

bool StopKeywordDetector::ProcessFrame(const int16_t* samples, size_t frame_count) {
    if (!enabled_ || wakenet_ == nullptr || model_data_ == nullptr) {
        return false;
    }
    
    if (samples == nullptr || frame_count == 0) {
        return false;
    }
    
    // Feed audio to WakeNet
    int chunk_size = wakenet_->get_samp_chunksize(model_data_);
    
    // Process in chunks
    for (size_t offset = 0; offset < frame_count; offset += chunk_size) {
        size_t chunk = std::min(chunk_size, static_cast<int>(frame_count - offset));
        
        int result = wakenet_->detect(model_data_, 
                                      const_cast<int16_t*>(samples + offset));
        
        if (result > 0) {
            // Keyword detected
            const char* keyword = nullptr;
            
            if (result >= kKeywordStop && result <= kKeywordPause) {
                keyword = kKeywordStrings[result];
            } else {
                keyword = "unknown";
            }
            
            ESP_LOGI(TAG, "Stop keyword detected: %s (id=%d)", keyword, result);
            
            if (stop_callback_) {
                stop_callback_(keyword);
            }
            
            return true;
        }
    }
    
    return false;
}

void StopKeywordDetector::Reset() {
    if (wakenet_ && model_data_) {
        wakenet_->reset(model_data_);
        ESP_LOGI(TAG, "Stop keyword detector reset");
    }
}

}  // namespace memoria
```

#### 步骤 2.2：集成到 AFE Pipeline

**修改文件**：`firmware/esp32/overlay/files/main/audio/afe_pipeline.h`

```cpp
#include "audio/stop_keyword.h"

class AfePipeline {
public:
    bool ProcessFrame(
        const int16_t* mic_samples,
        size_t frame_count,
        int16_t* out_samples,
        bool* vad_triggered,
        bool* stop_keyword_detected  // 新增
    );
    
    void SetStopKeywordEnabled(bool enabled);
    
private:
    std::unique_ptr<StopKeywordDetector> stop_detector_;
};
```

**修改文件**：`firmware/esp32/overlay/files/main/audio/afe_pipeline.cc`

```cpp
bool AfePipeline::Initialize(AecReference* aec_reference) {
    // ... existing AFE initialization ...
    
    // 初始化停止词检测器
    stop_detector_ = std::make_unique<StopKeywordDetector>();
    if (!stop_detector_->Initialize()) {
        ESP_LOGW(TAG, "Stop keyword detector initialization failed, continuing without it");
    }
    
    return true;
}

bool AfePipeline::ProcessFrame(
    const int16_t* mic_samples,
    size_t frame_count,
    int16_t* out_samples,
    bool* vad_triggered,
    bool* stop_keyword_detected
) {
    // ... existing AEC processing ...
    
    // 在 AEC 后的干净音频上检测停止词
    if (stop_keyword_detected && stop_detector_) {
        *stop_keyword_detected = stop_detector_->ProcessFrame(out_samples, fetch_size);
    }
    
    return true;
}

void AfePipeline::SetStopKeywordEnabled(bool enabled) {
    if (stop_detector_) {
        stop_detector_->SetEnabled(enabled);
    }
}
```

#### 步骤 2.3：集成到 Memoria Protocol

**修改文件**：`firmware/esp32/overlay/files/main/memoria/memoria_protocol.cc`

```cpp
void MemoriaProtocol::CaptureTask() {
    int16_t mic_raw[512];
    int16_t mic_clean[512];
    bool vad_triggered = false;
    bool stop_keyword_detected = false;
    
    while (true) {
        // 1. 读取麦克风
        i2s_read(I2S_NUM_1, mic_raw, sizeof(mic_raw), &bytes_read, portMAX_DELAY);
        
        // 2. AFE 处理（包含停止词检测）
        if (afe_pipeline_->ProcessFrame(
                mic_raw, 512, mic_clean, 
                &vad_triggered, 
                &stop_keyword_detected)) {
            
            // 3. 停止词触发本地 flush
            if (stop_keyword_detected && playback_active_) {
                ESP_LOGI(TAG, "Stop keyword detected, triggering local flush");
                NotifyLocalFlush();
                
                // 发送停止事件到服务器
                SendStopKeywordEvent(fence_);
                
                continue;  // 跳过本帧上传
            }
            
            // 4. 正常编码和上传
            EncodeAndSendAudio(mic_clean, 512);
        }
    }
}

void MemoriaProtocol::OnPlaybackStarted() {
    // 播放开始时启用停止词检测
    if (afe_pipeline_) {
        afe_pipeline_->SetStopKeywordEnabled(true);
    }
}

void MemoriaProtocol::OnPlaybackEnded() {
    // 播放结束时禁用停止词检测（节省 CPU）
    if (afe_pipeline_) {
        afe_pipeline_->SetStopKeywordEnabled(false);
    }
}

void MemoriaProtocol::SendStopKeywordEvent(const GenerationFence& fence) {
    // 构造 stop.keyword 事件
    cJSON* event = cJSON_CreateObject();
    cJSON_AddStringToObject(event, "type", "stop.keyword");
    cJSON_AddNumberToObject(event, "version", 2);
    cJSON_AddNumberToObject(event, "stream_epoch", stream_epoch_);
    cJSON_AddNumberToObject(event, "control_sequence", NextControlSequence());
    cJSON_AddNumberToObject(event, "device_monotonic_ms", GetMonotonicMs());
    
    // 添加 fence
    cJSON* fence_obj = cJSON_CreateObject();
    cJSON_AddNumberToObject(fence_obj, "turn_id", fence.turn_id);
    cJSON_AddNumberToObject(fence_obj, "generation_id", fence.generation_id);
    cJSON_AddNumberToObject(fence_obj, "tool_epoch", fence.tool_epoch);
    cJSON_AddNumberToObject(fence_obj, "session_epoch", fence.session_epoch);
    cJSON_AddItemToObject(event, "expected_fence", fence_obj);
    
    SendControlMessage(event);
    cJSON_Delete(event);
}
```

### 3. 服务端处理

#### Media Edge 处理

**修改文件**：`services/media_edge/device_ws_uplink.go`

```go
case "stop.keyword":
    return c.handleStopKeyword(envelope, runtime)

func (c *DeviceWSUplink) handleStopKeyword(
    envelope *DeviceEnvelope,
    runtime *DeviceRuntime,
) error {
    expectedFence := envelope.ExpectedFence
    if expectedFence == nil {
        return errors.New("missing expected_fence")
    }
    
    // 检查是否允许停止词打断
    if !runtime.AllowedBargeIn.Contains("local_keyword") {
        c.metrics.IncStopSourceForbidden("local_keyword")
        return errors.New("local_keyword barge-in not allowed")
    }
    
    // 取消 Generation
    eventID := envelope.DeviceMonotonicMs
    return runtime.CancelGeneration(
        eventID,
        "device.stop.keyword",
        expectedFence,
        envelope.DeviceMonotonicMs,
    )
}
```

#### Voice Core 处理

停止词的处理与按钮停止相同，复用现有的 `CancelGeneration` 逻辑。

**修改文件**：`services/agent/src/voice_core/grpc_bridge.py`

```python
if envelope.type == "stop.keyword":
    if not self.bridge.accept_client_event(envelope):
        await self._error(connection, "stale_client_event", "stop keyword rejected")
        return
    
    logger.info(
        "Stop keyword detected: session=%s fence=%s",
        connection.session.identity.session_id,
        fence,
    )
    
    if self.on_client_event is not None:
        await self.on_client_event(
            connection.session,
            envelope,
            int(event.monotonic_ms),
        )
    
    await self.emit_generation(
        connection.session.identity.session_id,
        connection.session.fence,
        action=media_pb2.GENERATION_ACTION_CANCEL,
        reason="stop_keyword",
    )
```

### 4. 配置与控制

#### Runtime Profile 配置

```json
{
  "device_id": "dev_xxx",
  "audio_settings": {
    "stop_keywords_enabled": true,
    "stop_keywords": ["停", "停一下", "等等", "暂停"],
    "stop_keyword_sensitivity": "medium"  // low | medium | high
  },
  "interruption_policy": {
    "allowed_barge_in": ["button", "local_keyword", "semantic"],
    "stop_keyword_mode": "enabled"  // disabled | enabled | debug
  }
}
```

#### 设备设置 API

小程序可以配置停止词行为：

```text
PATCH /v1/devices/{device_id}/settings

{
  "stop_keywords_enabled": true,
  "stop_keyword_sensitivity": "medium"
}
```

## 训练自定义停止词模型

### 步骤

1. **收集语音样本**
   - 不同说话人（成人、儿童、男女）
   - 不同距离（0.5m, 1m, 2m）
   - 不同音量
   - 不同环境噪声

2. **标注数据**
   - 正样本：停止词片段
   - 负样本：日常对话、环境噪声

3. **联系 Espressif 技术支持**
   - 提交训练数据
   - 获取自定义模型
   - 或使用 ESP-SR 训练工具自行训练

4. **集成模型**
   ```cpp
   extern const esp_wn_iface_t esp_sr_wakenet_memoria_stop_v1;
   #define WAKENET_MODEL esp_sr_wakenet_memoria_stop_v1
   ```

### 临时方案

在自定义模型训练完成前，可以：
1. 使用通用唤醒词模型测试流程
2. 或暂时禁用停止词功能
3. 仅依赖按钮和语义打断

## 性能优化

### CPU 优化

```cpp
// 停止词检测只在播放时启用
void MemoriaProtocol::SetPlaybackActive(bool active) {
    playback_active_ = active;
    
    if (afe_pipeline_) {
        afe_pipeline_->SetStopKeywordEnabled(active);
    }
}
```

### 误触发抑制

```cpp
// 增加确认机制：短时间内多次检测才触发
class StopKeywordDetector {
private:
    static constexpr int kConfirmationWindow = 500;  // ms
    static constexpr int kConfirmationThreshold = 2;
    
    int64_t last_detection_time_;
    int detection_count_;
    
public:
    bool ProcessFrameWithConfirmation(const int16_t* samples, size_t count) {
        bool detected = ProcessFrame(samples, count);
        
        if (detected) {
            int64_t now = esp_timer_get_time() / 1000;
            
            if (now - last_detection_time_ < kConfirmationWindow) {
                detection_count_++;
                
                if (detection_count_ >= kConfirmationThreshold) {
                    detection_count_ = 0;
                    return true;  // 确认触发
                }
            } else {
                detection_count_ = 1;
                last_detection_time_ = now;
            }
        }
        
        return false;
    }
};
```

## 验收标准

### 功能验收

✅ 停止词检测器成功初始化  
✅ 播放时检测到"停一下"触发本地 flush  
✅ 设备发送 `stop.keyword` 事件到服务端  
✅ 服务端取消 Generation  
✅ 不播放时停止词检测禁用（节省 CPU）  
✅ 非播放状态停止词不触发  

### 性能验收

| 指标 | 目标 |
|---|---|
| 停止词触发延迟 P95 | ≤ 500 ms |
| 停止词触发到本地静音 | ≤ 150 ms |
| 停止词准确率（正确场景） | ≥ 90% |
| 误触发率（日常对话） | < 5% |
| CPU 增量（检测开启时） | < 10% |

### 真机验收矩阵

| 场景 | 验收标准 |
|---|---|
| 播放时说"停一下" @ 1m | 检测率 ≥ 90% |
| 播放时说"等等" @ 1m | 检测率 ≥ 90% |
| 不同音量（20%/50%/80%） | 检测率 ≥ 85% |
| 不同距离（0.5m/1m/2m） | 检测率 ≥ 80% |
| 日常对话无停止词 | 误触发率 < 5% |
| 噪声环境（电视/音乐） | 误触发率 < 10% |

## 下一步

完成 P1-2 后，继续：

- **P1-3**：实现语义打断分类
- **P1-4**：AEC 声学矩阵完整验收

## 参考

- 整改方案第 9 节：全双工声学前端
- P0-3：按钮打断路径
- ESP-SR WakeNet 文档：https://docs.espressif.com/projects/esp-sr/en/latest/esp32s3/wake_word_engine/README.html
