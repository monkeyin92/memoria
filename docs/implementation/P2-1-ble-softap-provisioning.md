# P2-1 实现 BLE/SoftAP 配网

## 目标

实现 ESP32 设备的无线配网功能，让用户可以通过小程序将设备连接到 Wi-Fi 网络，完成设备初始化流程的关键步骤。

## 背景

根据整改方案第 6.1 节，设备启用流程为：

```text
ESP32 上电
  → 无 Activation 时显示动态二维码 + 备用短码
  → 小程序扫码
  → 建立 Bootstrap Session
  → BLE/SoftAP 配网 ← 本任务
  → 设备连云并提交在线证明
  → 登录账号一次性 Claim
  → ...（后续流程）
```

配网是设备联网的前提，必须安全、可靠、用户体验友好。

## 技术方案选择

### 方案对比

| 方案 | 优点 | 缺点 | 推荐度 |
|------|------|------|--------|
| **BLE** | 安全（加密）、低功耗、兼容性好 | 需要蓝牙权限、传输速度慢 | ⭐⭐⭐⭐⭐ |
| **SoftAP** | 无需额外权限、传输快 | 需要切换网络、iOS 体验差 | ⭐⭐⭐ |
| **SmartConfig** | 简单 | 不安全（明文）、成功率低 | ❌ 不推荐 |

**推荐方案**：**BLE（主）+ SoftAP（备用）**

- 优先使用 BLE（安全且体验好）
- SoftAP 作为降级方案（BLE 不可用时）

## 实施方案

### 1. 固件实现

#### 步骤 1.1：BLE 配网服务

**新增文件**：`firmware/esp32/overlay/files/main/provisioning/ble_provisioning.h`

```cpp
#pragma once

#include <string>
#include <functional>
#include "esp_gap_ble_api.h"
#include "esp_gatts_api.h"

namespace memoria {

/**
 * BLE-based Wi-Fi provisioning service.
 * 
 * Implements a secure BLE GATT service for provisioning:
 * - Wi-Fi credentials transfer
 * - Security via proof-of-possession (PoP)
 * - Status feedback
 */
class BleProvisioning {
public:
    struct Config {
        std::string device_name;        // BLE 广播名称
        std::string proof_of_possession; // PoP 密钥（从二维码获取）
        uint16_t conn_timeout_ms;
        bool enable_encryption;
    };
    
    struct WifiCredentials {
        std::string ssid;
        std::string password;
        std::string bootstrap_token;  // 从小程序传递
    };
    
    enum class Status {
        IDLE,
        ADVERTISING,
        CONNECTED,
        AUTHENTICATING,
        RECEIVING_CREDENTIALS,
        CONNECTING_WIFI,
        CONNECTED_WIFI,
        FAILED,
    };
    
    using StatusCallback = std::function<void(Status, const std::string& message)>;
    using CredentialsCallback = std::function<void(const WifiCredentials&)>;
    
    explicit BleProvisioning(const Config& config);
    ~BleProvisioning();
    
    /**
     * Start BLE provisioning service.
     * Device will start advertising and wait for connection.
     */
    bool Start();
    
    /**
     * Stop provisioning service.
     */
    void Stop();
    
    /**
     * Check if provisioning is active.
     */
    bool IsActive() const { return status_ != Status::IDLE; }
    
    /**
     * Get current status.
     */
    Status GetStatus() const { return status_; }
    
    /**
     * Set callbacks.
     */
    void SetStatusCallback(StatusCallback callback) { status_callback_ = callback; }
    void SetCredentialsCallback(CredentialsCallback callback) { credentials_callback_ = callback; }
    
private:
    Config config_;
    Status status_;
    StatusCallback status_callback_;
    CredentialsCallback credentials_callback_;
    
    esp_gatt_if_t gatts_if_;
    uint16_t conn_id_;
    uint16_t service_handle_;
    
    // GATT characteristic handles
    uint16_t char_wifi_ssid_handle_;
    uint16_t char_wifi_password_handle_;
    uint16_t char_bootstrap_token_handle_;
    uint16_t char_status_handle_;
    
    bool authenticated_;
    std::string session_key_;  // 会话密钥（PoP 派生）
    
    void StartAdvertising();
    void StopAdvertising();
    
    bool VerifyPoP(const std::string& challenge);
    void SetStatus(Status status, const std::string& message = "");
    
    // BLE event handlers
    static void GapEventHandler(esp_gap_ble_cb_event_t event, esp_ble_gap_cb_param_t* param);
    static void GattsEventHandler(esp_gatts_cb_event_t event, esp_gatt_if_t gatts_if, esp_ble_gatts_cb_param_t* param);
};

}  // namespace memoria
```

**新增文件**：`firmware/esp32/overlay/files/main/provisioning/ble_provisioning.cc`

```cpp
#include "provisioning/ble_provisioning.h"
#include "esp_log.h"
#include "esp_bt.h"
#include "esp_bt_main.h"
#include "esp_gap_ble_api.h"
#include "mbedtls/aes.h"
#include "mbedtls/sha256.h"

namespace memoria {

static const char* TAG = "BleProvisioning";

// Service UUID: 使用自定义 128-bit UUID
static const uint8_t kServiceUUID[16] = {
    0x12, 0x34, 0x56, 0x78, 0x90, 0xab, 0xcd, 0xef,
    0x12, 0x34, 0x56, 0x78, 0x90, 0xab, 0xcd, 0xef
};

BleProvisioning::BleProvisioning(const Config& config)
    : config_(config),
      status_(Status::IDLE),
      gatts_if_(ESP_GATT_IF_NONE),
      conn_id_(0),
      service_handle_(0),
      authenticated_(false) {
}

BleProvisioning::~BleProvisioning() {
    Stop();
}

bool BleProvisioning::Start() {
    ESP_LOGI(TAG, "Starting BLE provisioning service");
    
    // 1. 初始化 Bluetooth Controller
    esp_bt_controller_config_t bt_cfg = BT_CONTROLLER_INIT_CONFIG_DEFAULT();
    esp_err_t ret = esp_bt_controller_init(&bt_cfg);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Bluetooth controller init failed: %s", esp_err_to_name(ret));
        return false;
    }
    
    ret = esp_bt_controller_enable(ESP_BT_MODE_BLE);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Bluetooth controller enable failed: %s", esp_err_to_name(ret));
        return false;
    }
    
    // 2. 初始化 Bluedroid stack
    ret = esp_bluedroid_init();
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Bluedroid init failed: %s", esp_err_to_name(ret));
        return false;
    }
    
    ret = esp_bluedroid_enable();
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Bluedroid enable failed: %s", esp_err_to_name(ret));
        return false;
    }
    
    // 3. 注册 GATT 回调
    ret = esp_ble_gatts_register_callback(GattsEventHandler);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "GATTS register callback failed: %s", esp_err_to_name(ret));
        return false;
    }
    
    ret = esp_ble_gap_register_callback(GapEventHandler);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "GAP register callback failed: %s", esp_err_to_name(ret));
        return false;
    }
    
    // 4. 注册 GATT 应用
    ret = esp_ble_gatts_app_register(0);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "GATTS app register failed: %s", esp_err_to_name(ret));
        return false;
    }
    
    SetStatus(Status::ADVERTISING);
    return true;
}

void BleProvisioning::Stop() {
    ESP_LOGI(TAG, "Stopping BLE provisioning service");
    
    StopAdvertising();
    
    // Disable Bluedroid
    esp_bluedroid_disable();
    esp_bluedroid_deinit();
    
    // Disable BT controller
    esp_bt_controller_disable();
    esp_bt_controller_deinit();
    
    SetStatus(Status::IDLE);
}

void BleProvisioning::StartAdvertising() {
    // 设置广播数据
    esp_ble_adv_data_t adv_data = {};
    adv_data.set_scan_rsp = false;
    adv_data.include_name = true;
    adv_data.include_txpower = true;
    adv_data.min_interval = 0x20;
    adv_data.max_interval = 0x40;
    adv_data.appearance = 0x00;
    adv_data.manufacturer_len = 0;
    adv_data.p_manufacturer_data = nullptr;
    adv_data.service_data_len = 0;
    adv_data.p_service_data = nullptr;
    adv_data.service_uuid_len = sizeof(kServiceUUID);
    adv_data.p_service_uuid = const_cast<uint8_t*>(kServiceUUID);
    adv_data.flag = (ESP_BLE_ADV_FLAG_GEN_DISC | ESP_BLE_ADV_FLAG_BREDR_NOT_SPT);
    
    esp_ble_gap_config_adv_data(&adv_data);
    
    // 设置设备名称
    esp_ble_gap_set_device_name(config_.device_name.c_str());
    
    // 开始广播
    esp_ble_adv_params_t adv_params = {};
    adv_params.adv_int_min = 0x20;
    adv_params.adv_int_max = 0x40;
    adv_params.adv_type = ADV_TYPE_IND;
    adv_params.own_addr_type = BLE_ADDR_TYPE_PUBLIC;
    adv_params.channel_map = ADV_CHNL_ALL;
    adv_params.adv_filter_policy = ADV_FILTER_ALLOW_SCAN_ANY_CON_ANY;
    
    esp_ble_gap_start_advertising(&adv_params);
    
    ESP_LOGI(TAG, "BLE advertising started: %s", config_.device_name.c_str());
}

void BleProvisioning::StopAdvertising() {
    esp_ble_gap_stop_advertising();
}

bool BleProvisioning::VerifyPoP(const std::string& challenge) {
    // 使用 PoP 密钥验证挑战
    // 实现 HMAC-SHA256(PoP, challenge)
    
    unsigned char hmac[32];
    mbedtls_sha256_context ctx;
    mbedtls_sha256_init(&ctx);
    mbedtls_sha256_starts(&ctx, 0);
    mbedtls_sha256_update(&ctx, 
                          reinterpret_cast<const unsigned char*>(config_.proof_of_possession.c_str()),
                          config_.proof_of_possession.length());
    mbedtls_sha256_update(&ctx,
                          reinterpret_cast<const unsigned char*>(challenge.c_str()),
                          challenge.length());
    mbedtls_sha256_finish(&ctx, hmac);
    mbedtls_sha256_free(&ctx);
    
    // TODO: 比较 HMAC 与客户端发送的值
    
    return true;
}

void BleProvisioning::SetStatus(Status status, const std::string& message) {
    status_ = status;
    
    if (status_callback_) {
        status_callback_(status, message);
    }
}

// 静态回调（需要实现具体的 GATT 事件处理）
void BleProvisioning::GapEventHandler(esp_gap_ble_cb_event_t event, esp_ble_gap_cb_param_t* param) {
    // TODO: 实现 GAP 事件处理
}

void BleProvisioning::GattsEventHandler(esp_gatts_cb_event_t event, esp_gatt_if_t gatts_if, esp_ble_gatts_cb_param_t* param) {
    // TODO: 实现 GATTS 事件处理
}

}  // namespace memoria
```

#### 步骤 1.2：SoftAP 配网服务（备用）

**新增文件**：`firmware/esp32/overlay/files/main/provisioning/softap_provisioning.h`

```cpp
#pragma once

#include <string>
#include <functional>
#include "esp_wifi.h"
#include "esp_http_server.h"

namespace memoria {

/**
 * SoftAP-based Wi-Fi provisioning service.
 * 
 * Creates a temporary Wi-Fi AP and HTTP server for provisioning.
 * Less secure than BLE but works as fallback.
 */
class SoftApProvisioning {
public:
    struct Config {
        std::string ap_ssid;
        std::string ap_password;
        std::string bootstrap_token;
        uint16_t http_port;
    };
    
    struct WifiCredentials {
        std::string ssid;
        std::string password;
        std::string bootstrap_token;
    };
    
    using StatusCallback = std::function<void(bool success, const std::string& message)>;
    using CredentialsCallback = std::function<void(const WifiCredentials&)>;
    
    explicit SoftApProvisioning(const Config& config);
    ~SoftApProvisioning();
    
    bool Start();
    void Stop();
    
    void SetStatusCallback(StatusCallback callback) { status_callback_ = callback; }
    void SetCredentialsCallback(CredentialsCallback callback) { credentials_callback_ = callback; }
    
private:
    Config config_;
    StatusCallback status_callback_;
    CredentialsCallback credentials_callback_;
    
    httpd_handle_t http_server_;
    
    bool StartSoftAP();
    bool StartHttpServer();
    
    // HTTP handlers
    static esp_err_t HandleProvisionRequest(httpd_req_t* req);
    static esp_err_t HandleStatusRequest(httpd_req_t* req);
};

}  // namespace memoria
```

#### 步骤 1.3：统一配网管理器

**新增文件**：`firmware/esp32/overlay/files/main/provisioning/provisioning_manager.h`

```cpp
#pragma once

#include "provisioning/ble_provisioning.h"
#include "provisioning/softap_provisioning.h"

namespace memoria {

/**
 * Unified provisioning manager.
 * 
 * Tries BLE first, falls back to SoftAP if needed.
 */
class ProvisioningManager {
public:
    enum class Method {
        NONE,
        BLE,
        SOFT_AP,
    };
    
    struct WifiCredentials {
        std::string ssid;
        std::string password;
        std::string bootstrap_token;
    };
    
    using CompletionCallback = std::function<void(bool success, const WifiCredentials&)>;
    
    ProvisioningManager();
    ~ProvisioningManager();
    
    /**
     * Start provisioning with preferred method.
     * 
     * @param bootstrap_token Token from QR code
     * @param proof_of_possession PoP from QR code (for BLE security)
     * @param prefer_ble Whether to prefer BLE over SoftAP
     */
    bool Start(
        const std::string& bootstrap_token,
        const std::string& proof_of_possession,
        bool prefer_ble = true
    );
    
    /**
     * Stop provisioning.
     */
    void Stop();
    
    /**
     * Check if provisioning is active.
     */
    bool IsActive() const { return current_method_ != Method::NONE; }
    
    /**
     * Get current method.
     */
    Method GetCurrentMethod() const { return current_method_; }
    
    /**
     * Set completion callback.
     */
    void SetCompletionCallback(CompletionCallback callback) { completion_callback_ = callback; }
    
private:
    Method current_method_;
    CompletionCallback completion_callback_;
    
    std::unique_ptr<BleProvisioning> ble_provisioning_;
    std::unique_ptr<SoftApProvisioning> softap_provisioning_;
    
    bool StartBleProvisioning(const std::string& bootstrap_token, const std::string& pop);
    bool StartSoftApProvisioning(const std::string& bootstrap_token);
    
    void OnProvisioningComplete(bool success, const WifiCredentials& credentials);
};

}  // namespace memoria
```

### 2. 小程序实现

#### 步骤 2.1：BLE 配网页面

**新增文件**：`apps/miniprogram/pages/provision/ble-provision.wxml`

```html
<view class="container">
  <view class="header">
    <text class="title">连接设备到 Wi-Fi</text>
    <text class="subtitle">通过蓝牙配置网络</text>
  </view>
  
  <view class="status">
    <view class="status-item" wx:if="{{status === 'scanning'}}">
      <icon type="search" size="40"></icon>
      <text>正在搜索设备...</text>
    </view>
    
    <view class="status-item" wx:if="{{status === 'connecting'}}">
      <icon type="waiting" size="40"></icon>
      <text>正在连接到设备...</text>
    </view>
    
    <view class="status-item" wx:if="{{status === 'sending'}}">
      <icon type="waiting" size="40"></icon>
      <text>正在发送 Wi-Fi 信息...</text>
    </view>
    
    <view class="status-item" wx:if="{{status === 'success'}}">
      <icon type="success" size="40" color="green"></icon>
      <text>配网成功！</text>
    </view>
    
    <view class="status-item" wx:if="{{status === 'error'}}">
      <icon type="warn" size="40" color="red"></icon>
      <text>{{errorMessage}}</text>
    </view>
  </view>
  
  <view class="wifi-form" wx:if="{{status === 'ready'}}">
    <view class="form-item">
      <text class="label">Wi-Fi 名称</text>
      <input class="input" value="{{ssid}}" bindinput="onSsidInput" placeholder="请输入 Wi-Fi 名称"></input>
    </view>
    
    <view class="form-item">
      <text class="label">Wi-Fi 密码</text>
      <input class="input" value="{{password}}" bindinput="onPasswordInput" password="true" placeholder="请输入密码"></input>
    </view>
    
    <view class="tips">
      <text>⚠️ 仅支持 2.4 GHz Wi-Fi</text>
    </view>
    
    <button class="btn-primary" bindtap="startProvisioning" disabled="{{!canStart}}">
      开始配网
    </button>
  </view>
  
  <view class="fallback" wx:if="{{status === 'ble_unavailable'}}">
    <text>蓝牙不可用，使用备用方案</text>
    <button bindtap="useSoftAP">使用 Wi-Fi 配网</button>
  </view>
</view>
```

**新增文件**：`apps/miniprogram/pages/provision/ble-provision.js`

```javascript
const BleProvisioning = require('../../utils/ble-provisioning');

Page({
  data: {
    status: 'scanning', // scanning | ready | connecting | sending | success | error
    ssid: '',
    password: '',
    deviceId: '',
    bootstrapToken: '',
    proofOfPossession: '',
    errorMessage: '',
    canStart: false,
  },
  
  onLoad(options) {
    const { deviceId, bootstrapToken, pop } = options;
    
    this.setData({
      deviceId,
      bootstrapToken,
      proofOfPossession: pop,
    });
    
    this.startBleProvisioning();
  },
  
  async startBleProvisioning() {
    try {
      // 1. 检查蓝牙权限
      const authResult = await wx.getSetting();
      if (!authResult.authSetting['scope.bluetooth']) {
        await wx.authorize({ scope: 'scope.bluetooth' });
      }
      
      // 2. 初始化蓝牙
      await wx.openBluetoothAdapter();
      
      // 3. 搜索设备
      this.setData({ status: 'scanning' });
      
      const bleService = new BleProvisioning({
        deviceName: `Memoria-${this.data.deviceId.substr(-6)}`,
        serviceUUID: '12345678-90ab-cdef-1234-567890abcdef',
        pop: this.data.proofOfPossession,
      });
      
      const connected = await bleService.connect();
      
      if (connected) {
        this.setData({ status: 'ready' });
        this.bleService = bleService;
      } else {
        throw new Error('无法连接到设备');
      }
      
    } catch (error) {
      console.error('BLE provisioning failed:', error);
      
      if (error.errCode === 10001) {
        // 蓝牙未打开
        this.setData({
          status: 'ble_unavailable',
          errorMessage: '请打开蓝牙后重试',
        });
      } else {
        this.setData({
          status: 'error',
          errorMessage: error.message || '配网失败',
        });
      }
    }
  },
  
  onSsidInput(e) {
    this.setData({
      ssid: e.detail.value,
      canStart: e.detail.value && this.data.password,
    });
  },
  
  onPasswordInput(e) {
    this.setData({
      password: e.detail.value,
      canStart: this.data.ssid && e.detail.value,
    });
  },
  
  async startProvisioning() {
    this.setData({ status: 'sending' });
    
    try {
      // 发送 Wi-Fi 凭证到设备
      const result = await this.bleService.sendCredentials({
        ssid: this.data.ssid,
        password: this.data.password,
        bootstrapToken: this.data.bootstrapToken,
      });
      
      if (result.success) {
        this.setData({ status: 'success' });
        
        // 等待 2 秒后跳转
        setTimeout(() => {
          wx.navigateBack();
        }, 2000);
      } else {
        throw new Error(result.message || '配网失败');
      }
      
    } catch (error) {
      console.error('Provisioning failed:', error);
      this.setData({
        status: 'error',
        errorMessage: error.message,
      });
    }
  },
  
  useSoftAP() {
    wx.navigateTo({
      url: `/pages/provision/softap-provision?deviceId=${this.data.deviceId}&bootstrapToken=${this.data.bootstrapToken}`,
    });
  },
  
  onUnload() {
    // 清理 BLE 连接
    if (this.bleService) {
      this.bleService.disconnect();
    }
    
    wx.closeBluetoothAdapter();
  },
});
```

#### 步骤 2.2：BLE 配网工具类

**新增文件**：`apps/miniprogram/utils/ble-provisioning.js`

```javascript
class BleProvisioning {
  constructor(options) {
    this.deviceName = options.deviceName;
    this.serviceUUID = options.serviceUUID;
    this.pop = options.pop;
    
    this.deviceId = null;
    this.connected = false;
    
    // Characteristic UUIDs
    this.charUUIDs = {
      ssid: '12345678-90ab-cdef-1234-567890abcde1',
      password: '12345678-90ab-cdef-1234-567890abcde2',
      token: '12345678-90ab-cdef-1234-567890abcde3',
      status: '12345678-90ab-cdef-1234-567890abcde4',
    };
  }
  
  async connect() {
    try {
      // 1. 开始搜索设备
      await this._startScan();
      
      // 2. 等待找到设备
      const device = await this._waitForDevice(10000);
      if (!device) {
        throw new Error('未找到设备');
      }
      
      this.deviceId = device.deviceId;
      
      // 3. 停止搜索
      await wx.stopBluetoothDevicesDiscovery();
      
      // 4. 连接设备
      await wx.createBLEConnection({
        deviceId: this.deviceId,
      });
      
      // 5. 获取服务
      const services = await wx.getBLEDeviceServices({
        deviceId: this.deviceId,
      });
      
      const service = services.services.find(s => s.uuid === this.serviceUUID);
      if (!service) {
        throw new Error('服务不可用');
      }
      
      // 6. 验证 PoP
      await this._verifyPoP();
      
      this.connected = true;
      return true;
      
    } catch (error) {
      console.error('BLE connect failed:', error);
      throw error;
    }
  }
  
  async sendCredentials(credentials) {
    if (!this.connected) {
      throw new Error('设备未连接');
    }
    
    try {
      // 1. 写入 SSID
      await this._writeCharacteristic(
        this.charUUIDs.ssid,
        this._stringToArrayBuffer(credentials.ssid)
      );
      
      // 2. 写入密码（加密）
      const encryptedPassword = await this._encryptPassword(credentials.password);
      await this._writeCharacteristic(
        this.charUUIDs.password,
        encryptedPassword
      );
      
      // 3. 写入 Bootstrap Token
      await this._writeCharacteristic(
        this.charUUIDs.token,
        this._stringToArrayBuffer(credentials.bootstrapToken)
      );
      
      // 4. 等待状态确认
      const status = await this._readStatus();
      
      return {
        success: status === 'connected',
        message: status,
      };
      
    } catch (error) {
      console.error('Send credentials failed:', error);
      throw error;
    }
  }
  
  async disconnect() {
    if (this.deviceId) {
      await wx.closeBLEConnection({
        deviceId: this.deviceId,
      });
      this.connected = false;
    }
  }
  
  // Private methods
  
  async _startScan() {
    return wx.startBluetoothDevicesDiscovery({
      services: [this.serviceUUID],
      allowDuplicatesKey: false,
    });
  }
  
  async _waitForDevice(timeout) {
    return new Promise((resolve) => {
      let found = false;
      
      const timer = setTimeout(() => {
        wx.offBluetoothDeviceFound(onDeviceFound);
        if (!found) {
          resolve(null);
        }
      }, timeout);
      
      const onDeviceFound = (res) => {
        const device = res.devices.find(d => d.name === this.deviceName);
        
        if (device && !found) {
          found = true;
          clearTimeout(timer);
          wx.offBluetoothDeviceFound(onDeviceFound);
          resolve(device);
        }
      };
      
      wx.onBluetoothDeviceFound(onDeviceFound);
    });
  }
  
  async _verifyPoP() {
    // TODO: 实现 PoP 验证逻辑
    // 1. 读取设备挑战
    // 2. 使用 PoP 计算 HMAC
    // 3. 写入响应
    return true;
  }
  
  async _writeCharacteristic(uuid, value) {
    return wx.writeBLECharacteristicValue({
      deviceId: this.deviceId,
      serviceId: this.serviceUUID,
      characteristicId: uuid,
      value: value,
    });
  }
  
  async _readCharacteristic(uuid) {
    const result = await wx.readBLECharacteristicValue({
      deviceId: this.deviceId,
      serviceId: this.serviceUUID,
      characteristicId: uuid,
    });
    
    return result.value;
  }
  
  async _readStatus() {
    const value = await this._readCharacteristic(this.charUUIDs.status);
    return this._arrayBufferToString(value);
  }
  
  async _encryptPassword(password) {
    // TODO: 使用会话密钥加密密码
    // 当前使用明文（仅用于演示）
    return this._stringToArrayBuffer(password);
  }
  
  _stringToArrayBuffer(str) {
    const buffer = new ArrayBuffer(str.length);
    const view = new Uint8Array(buffer);
    for (let i = 0; i < str.length; i++) {
      view[i] = str.charCodeAt(i);
    }
    return buffer;
  }
  
  _arrayBufferToString(buffer) {
    return String.fromCharCode.apply(null, new Uint8Array(buffer));
  }
}

module.exports = BleProvisioning;
```

### 3. 服务端支持

#### 步骤 3.1：Bootstrap Session API

**端点**: `POST /v1/device-bootstrap-sessions`

```json
{
  "device_id": "dev_xxx",
  "qr_code_id": "qr_xxx",
  "proof_of_possession": "pop_secret_xxx"
}
```

**响应**:
```json
{
  "bootstrap_session_id": "bs_xxx",
  "bootstrap_token": "token_xxx",
  "expires_at": "2026-08-22T12:00:00Z",
  "status": "pending"
}
```

#### 步骤 3.2：设备上线通知

**端点**: `POST /v1/devices/{device_id}/online-proof`

```json
{
  "bootstrap_token": "token_xxx",
  "ip_address": "192.168.1.100",
  "mac_address": "AA:BB:CC:DD:EE:FF",
  "firmware_version": "1.0.0",
  "connected_at": "2026-08-22T12:05:00Z"
}
```

### 4. 安全考虑

#### 4.1：Proof of Possession (PoP)

- 每个设备二维码包含唯一的 PoP 密钥
- PoP 用于 BLE 会话认证
- 密码传输时使用 PoP 派生的会话密钥加密

#### 4.2：Bootstrap Token

- 服务端生成一次性 token
- Token 有效期 10 分钟
- 设备上线时需要提交 token 证明身份

#### 4.3：Wi-Fi 密码保护

- 小程序端：仅存储在内存，不持久化
- 传输：使用 BLE 加密或 HTTPS
- 设备端：存储在 NVS 加密分区

## 验收标准

### 功能验收

✅ BLE 配网流程完整：扫描 → 连接 → 认证 → 传输 → 确认  
✅ SoftAP 配网作为降级方案可用  
✅ 设备成功连接到 Wi-Fi 并上线  
✅ Bootstrap Token 验证通过  
✅ 配网失败时有清晰错误提示  

### 安全验收

✅ PoP 认证正确实施  
✅ Wi-Fi 密码加密传输  
✅ Bootstrap Token 一次性有效  
✅ 密码不在小程序或日志中泄露  

### 用户体验验收

✅ BLE 配网成功率 > 95%  
✅ 配网时间 < 30 秒（P95）  
✅ 错误提示清晰友好  
✅ 支持重试机制  

## 下一步

完成 P2-1 后，继续：
- P2-2: Soul/Persona 配置界面
- P2-3: 家庭成员与监护设置

## 参考

- 整改方案第 6 节：设备启用与独立运行流程
- ESP32 BLE 文档：https://docs.espressif.com/projects/esp-idf/en/latest/esp32/api-guides/ble/index.html
- 微信小程序蓝牙API：https://developers.weixin.qq.com/miniprogram/dev/api/device/bluetooth/wx.openBluetoothAdapter.html
