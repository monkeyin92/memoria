#include "application.h"
#include "backlight.h"
#include "button.h"
#include "codecs/box_audio_codec.h"
#include "config.h"
#include "display/lcd_display.h"
#include "wifi_board.h"

#include <esp_log.h>
#include <esp_timer.h>
#include <cinttypes>
#include "esp_idf_version.h"

#define ESP_VOCAT_ENABLE_CAP_TOUCH_SENSOR (ESP_IDF_VERSION < ESP_IDF_VERSION_VAL(6, 0, 0))

#include <driver/i2c_master.h>
#include <esp_lcd_panel_io.h>
#include <esp_lcd_panel_ops.h>
#include <esp_lcd_st77916.h>
#include <cstdlib>
#include "bmi270_api.h"
#include "esp_lcd_touch_cst816s.h"
#include "i2c_bus.h"
#include "i2c_device.h"
#include "touch.h"
#include "memoria_bootstrap.h"
#include "memoria_face_display.h"
#include "settings.h"

#if ESP_VOCAT_ENABLE_CAP_TOUCH_SENSOR
extern "C" {
#include "touch_button_sensor.h"
#include "touch_slider_sensor.h"
}
#endif

#include <atomic>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>
#include <freertos/task.h>
#include "driver/temperature_sensor.h"

#define TAG "MemoriaEspVocat"

namespace Bmi270Motion {
static bmi270_handle_t bmi_handle_ = nullptr;

esp_err_t Initialize(i2c_bus_handle_t i2c_bus) {
    if (bmi_handle_) {
        return ESP_OK;
    }
    if (!i2c_bus) {
        return ESP_ERR_INVALID_ARG;
    }

    esp_err_t ret = bmi270_sensor_create(i2c_bus, &bmi_handle_, bmi270_config_file,
                                         BMI2_GYRO_CROSS_SENS_ENABLE | BMI2_CRT_RTOSK_ENABLE);
    if (ret != ESP_OK || !bmi_handle_) {
        ESP_LOGW(TAG, "BMI270 init failed: %s", esp_err_to_name(ret));
        return ret == ESP_OK ? ESP_FAIL : ret;
    }

    const uint8_t sens_list[] = {BMI2_ACCEL};
    int8_t rslt = bmi270_sensor_enable(sens_list, 1, bmi_handle_);
    if (rslt != BMI2_OK) {
        ESP_LOGW(TAG, "BMI270 accel enable failed: %d", rslt);
        return ESP_FAIL;
    }

    ESP_LOGI(TAG, "BMI270 initialized");
    return ESP_OK;
}

bool ReadAccelRaw(struct bmi2_sens_data& accel) {
    if (!bmi_handle_) {
        return false;
    }
    int8_t rslt = bmi2_get_sensor_data(&accel, bmi_handle_);
    return rslt == BMI2_OK;
}
}  // namespace Bmi270Motion

temperature_sensor_handle_t temp_sensor = NULL;
static const st77916_lcd_init_cmd_t vendor_specific_init_yysj[] = {
    {0xF0, (uint8_t[]){0x28}, 1, 0},
    {0xF2, (uint8_t[]){0x28}, 1, 0},
    {0x73, (uint8_t[]){0xF0}, 1, 0},
    {0x7C, (uint8_t[]){0xD1}, 1, 0},
    {0x83, (uint8_t[]){0xE0}, 1, 0},
    {0x84, (uint8_t[]){0x61}, 1, 0},
    {0xF2, (uint8_t[]){0x82}, 1, 0},
    {0xF0, (uint8_t[]){0x00}, 1, 0},
    {0xF0, (uint8_t[]){0x01}, 1, 0},
    {0xF1, (uint8_t[]){0x01}, 1, 0},
    {0xB0, (uint8_t[]){0x56}, 1, 0},
    {0xB1, (uint8_t[]){0x4D}, 1, 0},
    {0xB2, (uint8_t[]){0x24}, 1, 0},
    {0xB4, (uint8_t[]){0x87}, 1, 0},
    {0xB5, (uint8_t[]){0x44}, 1, 0},
    {0xB6, (uint8_t[]){0x8B}, 1, 0},
    {0xB7, (uint8_t[]){0x40}, 1, 0},
    {0xB8, (uint8_t[]){0x86}, 1, 0},
    {0xBA, (uint8_t[]){0x00}, 1, 0},
    {0xBB, (uint8_t[]){0x08}, 1, 0},
    {0xBC, (uint8_t[]){0x08}, 1, 0},
    {0xBD, (uint8_t[]){0x00}, 1, 0},
    {0xC0, (uint8_t[]){0x80}, 1, 0},
    {0xC1, (uint8_t[]){0x10}, 1, 0},
    {0xC2, (uint8_t[]){0x37}, 1, 0},
    {0xC3, (uint8_t[]){0x80}, 1, 0},
    {0xC4, (uint8_t[]){0x10}, 1, 0},
    {0xC5, (uint8_t[]){0x37}, 1, 0},
    {0xC6, (uint8_t[]){0xA9}, 1, 0},
    {0xC7, (uint8_t[]){0x41}, 1, 0},
    {0xC8, (uint8_t[]){0x01}, 1, 0},
    {0xC9, (uint8_t[]){0xA9}, 1, 0},
    {0xCA, (uint8_t[]){0x41}, 1, 0},
    {0xCB, (uint8_t[]){0x01}, 1, 0},
    {0xD0, (uint8_t[]){0x91}, 1, 0},
    {0xD1, (uint8_t[]){0x68}, 1, 0},
    {0xD2, (uint8_t[]){0x68}, 1, 0},
    {0xF5, (uint8_t[]){0x00, 0xA5}, 2, 0},
    {0xDD, (uint8_t[]){0x4F}, 1, 0},
    {0xDE, (uint8_t[]){0x4F}, 1, 0},
    {0xF1, (uint8_t[]){0x10}, 1, 0},
    {0xF0, (uint8_t[]){0x00}, 1, 0},
    {0xF0, (uint8_t[]){0x02}, 1, 0},
    {0xE0,
     (uint8_t[]){0xF0, 0x0A, 0x10, 0x09, 0x09, 0x36, 0x35, 0x33, 0x4A, 0x29, 0x15, 0x15, 0x2E,
                 0x34},
     14, 0},
    {0xE1,
     (uint8_t[]){0xF0, 0x0A, 0x0F, 0x08, 0x08, 0x05, 0x34, 0x33, 0x4A, 0x39, 0x15, 0x15, 0x2D,
                 0x33},
     14, 0},
    {0xF0, (uint8_t[]){0x10}, 1, 0},
    {0xF3, (uint8_t[]){0x10}, 1, 0},
    {0xE0, (uint8_t[]){0x07}, 1, 0},
    {0xE1, (uint8_t[]){0x00}, 1, 0},
    {0xE2, (uint8_t[]){0x00}, 1, 0},
    {0xE3, (uint8_t[]){0x00}, 1, 0},
    {0xE4, (uint8_t[]){0xE0}, 1, 0},
    {0xE5, (uint8_t[]){0x06}, 1, 0},
    {0xE6, (uint8_t[]){0x21}, 1, 0},
    {0xE7, (uint8_t[]){0x01}, 1, 0},
    {0xE8, (uint8_t[]){0x05}, 1, 0},
    {0xE9, (uint8_t[]){0x02}, 1, 0},
    {0xEA, (uint8_t[]){0xDA}, 1, 0},
    {0xEB, (uint8_t[]){0x00}, 1, 0},
    {0xEC, (uint8_t[]){0x00}, 1, 0},
    {0xED, (uint8_t[]){0x0F}, 1, 0},
    {0xEE, (uint8_t[]){0x00}, 1, 0},
    {0xEF, (uint8_t[]){0x00}, 1, 0},
    {0xF8, (uint8_t[]){0x00}, 1, 0},
    {0xF9, (uint8_t[]){0x00}, 1, 0},
    {0xFA, (uint8_t[]){0x00}, 1, 0},
    {0xFB, (uint8_t[]){0x00}, 1, 0},
    {0xFC, (uint8_t[]){0x00}, 1, 0},
    {0xFD, (uint8_t[]){0x00}, 1, 0},
    {0xFE, (uint8_t[]){0x00}, 1, 0},
    {0xFF, (uint8_t[]){0x00}, 1, 0},
    {0x60, (uint8_t[]){0x40}, 1, 0},
    {0x61, (uint8_t[]){0x04}, 1, 0},
    {0x62, (uint8_t[]){0x00}, 1, 0},
    {0x63, (uint8_t[]){0x42}, 1, 0},
    {0x64, (uint8_t[]){0xD9}, 1, 0},
    {0x65, (uint8_t[]){0x00}, 1, 0},
    {0x66, (uint8_t[]){0x00}, 1, 0},
    {0x67, (uint8_t[]){0x00}, 1, 0},
    {0x68, (uint8_t[]){0x00}, 1, 0},
    {0x69, (uint8_t[]){0x00}, 1, 0},
    {0x6A, (uint8_t[]){0x00}, 1, 0},
    {0x6B, (uint8_t[]){0x00}, 1, 0},
    {0x70, (uint8_t[]){0x40}, 1, 0},
    {0x71, (uint8_t[]){0x03}, 1, 0},
    {0x72, (uint8_t[]){0x00}, 1, 0},
    {0x73, (uint8_t[]){0x42}, 1, 0},
    {0x74, (uint8_t[]){0xD8}, 1, 0},
    {0x75, (uint8_t[]){0x00}, 1, 0},
    {0x76, (uint8_t[]){0x00}, 1, 0},
    {0x77, (uint8_t[]){0x00}, 1, 0},
    {0x78, (uint8_t[]){0x00}, 1, 0},
    {0x79, (uint8_t[]){0x00}, 1, 0},
    {0x7A, (uint8_t[]){0x00}, 1, 0},
    {0x7B, (uint8_t[]){0x00}, 1, 0},
    {0x80, (uint8_t[]){0x48}, 1, 0},
    {0x81, (uint8_t[]){0x00}, 1, 0},
    {0x82, (uint8_t[]){0x06}, 1, 0},
    {0x83, (uint8_t[]){0x02}, 1, 0},
    {0x84, (uint8_t[]){0xD6}, 1, 0},
    {0x85, (uint8_t[]){0x04}, 1, 0},
    {0x86, (uint8_t[]){0x00}, 1, 0},
    {0x87, (uint8_t[]){0x00}, 1, 0},
    {0x88, (uint8_t[]){0x48}, 1, 0},
    {0x89, (uint8_t[]){0x00}, 1, 0},
    {0x8A, (uint8_t[]){0x08}, 1, 0},
    {0x8B, (uint8_t[]){0x02}, 1, 0},
    {0x8C, (uint8_t[]){0xD8}, 1, 0},
    {0x8D, (uint8_t[]){0x04}, 1, 0},
    {0x8E, (uint8_t[]){0x00}, 1, 0},
    {0x8F, (uint8_t[]){0x00}, 1, 0},
    {0x90, (uint8_t[]){0x48}, 1, 0},
    {0x91, (uint8_t[]){0x00}, 1, 0},
    {0x92, (uint8_t[]){0x0A}, 1, 0},
    {0x93, (uint8_t[]){0x02}, 1, 0},
    {0x94, (uint8_t[]){0xDA}, 1, 0},
    {0x95, (uint8_t[]){0x04}, 1, 0},
    {0x96, (uint8_t[]){0x00}, 1, 0},
    {0x97, (uint8_t[]){0x00}, 1, 0},
    {0x98, (uint8_t[]){0x48}, 1, 0},
    {0x99, (uint8_t[]){0x00}, 1, 0},
    {0x9A, (uint8_t[]){0x0C}, 1, 0},
    {0x9B, (uint8_t[]){0x02}, 1, 0},
    {0x9C, (uint8_t[]){0xDC}, 1, 0},
    {0x9D, (uint8_t[]){0x04}, 1, 0},
    {0x9E, (uint8_t[]){0x00}, 1, 0},
    {0x9F, (uint8_t[]){0x00}, 1, 0},
    {0xA0, (uint8_t[]){0x48}, 1, 0},
    {0xA1, (uint8_t[]){0x00}, 1, 0},
    {0xA2, (uint8_t[]){0x05}, 1, 0},
    {0xA3, (uint8_t[]){0x02}, 1, 0},
    {0xA4, (uint8_t[]){0xD5}, 1, 0},
    {0xA5, (uint8_t[]){0x04}, 1, 0},
    {0xA6, (uint8_t[]){0x00}, 1, 0},
    {0xA7, (uint8_t[]){0x00}, 1, 0},
    {0xA8, (uint8_t[]){0x48}, 1, 0},
    {0xA9, (uint8_t[]){0x00}, 1, 0},
    {0xAA, (uint8_t[]){0x07}, 1, 0},
    {0xAB, (uint8_t[]){0x02}, 1, 0},
    {0xAC, (uint8_t[]){0xD7}, 1, 0},
    {0xAD, (uint8_t[]){0x04}, 1, 0},
    {0xAE, (uint8_t[]){0x00}, 1, 0},
    {0xAF, (uint8_t[]){0x00}, 1, 0},
    {0xB0, (uint8_t[]){0x48}, 1, 0},
    {0xB1, (uint8_t[]){0x00}, 1, 0},
    {0xB2, (uint8_t[]){0x09}, 1, 0},
    {0xB3, (uint8_t[]){0x02}, 1, 0},
    {0xB4, (uint8_t[]){0xD9}, 1, 0},
    {0xB5, (uint8_t[]){0x04}, 1, 0},
    {0xB6, (uint8_t[]){0x00}, 1, 0},
    {0xB7, (uint8_t[]){0x00}, 1, 0},
    {0xB8, (uint8_t[]){0x48}, 1, 0},
    {0xB9, (uint8_t[]){0x00}, 1, 0},
    {0xBA, (uint8_t[]){0x0B}, 1, 0},
    {0xBB, (uint8_t[]){0x02}, 1, 0},
    {0xBC, (uint8_t[]){0xDB}, 1, 0},
    {0xBD, (uint8_t[]){0x04}, 1, 0},
    {0xBE, (uint8_t[]){0x00}, 1, 0},
    {0xBF, (uint8_t[]){0x00}, 1, 0},
    {0xC0, (uint8_t[]){0x10}, 1, 0},
    {0xC1, (uint8_t[]){0x47}, 1, 0},
    {0xC2, (uint8_t[]){0x56}, 1, 0},
    {0xC3, (uint8_t[]){0x65}, 1, 0},
    {0xC4, (uint8_t[]){0x74}, 1, 0},
    {0xC5, (uint8_t[]){0x88}, 1, 0},
    {0xC6, (uint8_t[]){0x99}, 1, 0},
    {0xC7, (uint8_t[]){0x01}, 1, 0},
    {0xC8, (uint8_t[]){0xBB}, 1, 0},
    {0xC9, (uint8_t[]){0xAA}, 1, 0},
    {0xD0, (uint8_t[]){0x10}, 1, 0},
    {0xD1, (uint8_t[]){0x47}, 1, 0},
    {0xD2, (uint8_t[]){0x56}, 1, 0},
    {0xD3, (uint8_t[]){0x65}, 1, 0},
    {0xD4, (uint8_t[]){0x74}, 1, 0},
    {0xD5, (uint8_t[]){0x88}, 1, 0},
    {0xD6, (uint8_t[]){0x99}, 1, 0},
    {0xD7, (uint8_t[]){0x01}, 1, 0},
    {0xD8, (uint8_t[]){0xBB}, 1, 0},
    {0xD9, (uint8_t[]){0xAA}, 1, 0},
    {0xF3, (uint8_t[]){0x01}, 1, 0},
    {0xF0, (uint8_t[]){0x00}, 1, 0},
    {0x21, (uint8_t[]){}, 0, 0},
    {0x11, (uint8_t[]){}, 0, 0},
    {0x00, (uint8_t[]){}, 0, 120},
};
float tsens_value;
static gpio_num_t AUDIO_I2S_GPIO_DIN = AUDIO_I2S_GPIO_DIN_1;
static gpio_num_t AUDIO_CODEC_PA_PIN = AUDIO_CODEC_PA_PIN_1;
static gpio_num_t QSPI_PIN_NUM_LCD_RST = QSPI_PIN_NUM_LCD_RST_1;
static gpio_num_t TOUCH_PAD2 = TOUCH_PAD2_1;
static gpio_num_t UART1_TX = UART1_TX_1;
static gpio_num_t UART1_RX = UART1_RX_1;

class MemoriaEspVocat;

class Charge : public I2cDevice {
public:
    static constexpr uint8_t kRegVoltage = 0x08;
    static constexpr uint8_t kRegBatteryStatus = 0x0A;
    static constexpr uint8_t kRegCurrent = 0x0C;
    static constexpr uint8_t kRegStateOfCharge = 0x2C;
    static constexpr uint8_t kRegAverageCurrent = 0x14;
    static constexpr int kChargingCurrentMa = 30;
    static constexpr uint16_t kBatteryStatusDsg = BIT0;

    Charge(i2c_master_bus_handle_t i2c_bus, uint8_t addr) : I2cDevice(i2c_bus, addr) {
        read_buffer_ = new uint8_t[8];
    }
    ~Charge() { delete[] read_buffer_; }

    int16_t ReadWord(uint8_t reg) {
        uint8_t data[2] = {0};
        ReadRegs(reg, data, 2);
        return static_cast<int16_t>(static_cast<uint16_t>(data[0]) |
                                    (static_cast<uint16_t>(data[1]) << 8));
    }

    int GetBatteryLevel() {
        int level = ReadWord(kRegStateOfCharge);
        if (level < 0) {
            return 0;
        }
        if (level > 100) {
            return 100;
        }
        return level;
    }

    bool IsCharging() {
        const uint16_t status = static_cast<uint16_t>(ReadWord(kRegBatteryStatus));
        if ((status & kBatteryStatusDsg) == 0) {
            return true;
        }

        const int16_t avg_current_ma = ReadWord(kRegAverageCurrent);
        const int16_t current_ma = ReadWord(kRegCurrent);
        return avg_current_ma > kChargingCurrentMa || current_ma > kChargingCurrentMa;
    }

    bool IsDischarging() {
        return (static_cast<uint16_t>(ReadWord(kRegBatteryStatus)) & kBatteryStatusDsg) != 0;
    }

    void Update() {
        const int16_t voltage = ReadWord(kRegVoltage);
        const int16_t current = ReadWord(kRegCurrent);
        ESP_ERROR_CHECK(temperature_sensor_get_celsius(temp_sensor, &tsens_value));
        (void)voltage;
        (void)current;
    }

private:
    uint8_t* read_buffer_ = nullptr;
};

class Cst816s : public I2cDevice {
public:
    struct TouchPoint_t {
        int num = 0;
        int x = -1;
        int y = -1;
    };

    enum TouchEvent { TOUCH_NONE, TOUCH_PRESS, TOUCH_RELEASE, TOUCH_HOLD };

    Cst816s(i2c_master_bus_handle_t i2c_bus, uint8_t addr) : I2cDevice(i2c_bus, addr) {
        read_buffer_ = new uint8_t[6];
        was_touched_ = false;
        press_count_ = 0;

        touch_isr_mux_ = xSemaphoreCreateBinary();
        if (touch_isr_mux_ == NULL) {
            ESP_LOGE(TAG, "Failed to create touch semaphore");
        }
    }

    ~Cst816s() {
        delete[] read_buffer_;

        if (touch_isr_mux_ != NULL) {
            vSemaphoreDelete(touch_isr_mux_);
            touch_isr_mux_ = NULL;
        }
    }

    void UpdateTouchPoint() {
        ReadRegs(0x02, read_buffer_, 6);
        tp_.num = read_buffer_[0] & 0x0F;
        tp_.x = ((read_buffer_[1] & 0x0F) << 8) | read_buffer_[2];
        tp_.y = ((read_buffer_[3] & 0x0F) << 8) | read_buffer_[4];
    }

    const TouchPoint_t& GetTouchPoint() { return tp_; }

    TouchEvent CheckTouchEvent() {
        bool is_touched = (tp_.num > 0);
        TouchEvent event = TOUCH_NONE;

        if (is_touched && !was_touched_) {
            press_count_++;
            event = TOUCH_PRESS;
            ESP_LOGI(TAG, "TOUCH PRESS - count: %d, x: %d, y: %d", press_count_, tp_.x, tp_.y);
        } else if (!is_touched && was_touched_) {
            event = TOUCH_RELEASE;
            ESP_LOGI(TAG, "TOUCH RELEASE - total presses: %d", press_count_);
        } else if (is_touched && was_touched_) {
            event = TOUCH_HOLD;
            ESP_LOGD(TAG, "TOUCH HOLD - x: %d, y: %d", tp_.x, tp_.y);
        }

        was_touched_ = is_touched;
        return event;
    }

    int GetPressCount() const { return press_count_; }

    void ResetPressCount() { press_count_ = 0; }

    SemaphoreHandle_t GetTouchSemaphore() { return touch_isr_mux_; }

    bool WaitForTouchEvent(TickType_t timeout = portMAX_DELAY) {
        if (touch_isr_mux_ != NULL) {
            return xSemaphoreTake(touch_isr_mux_, timeout) == pdTRUE;
        }
        return false;
    }

    void NotifyTouchEvent() {
        if (touch_isr_mux_ != NULL) {
            BaseType_t xHigherPriorityTaskWoken = pdFALSE;
            xSemaphoreGiveFromISR(touch_isr_mux_, &xHigherPriorityTaskWoken);
            portYIELD_FROM_ISR(xHigherPriorityTaskWoken);
        }
    }

private:
    uint8_t* read_buffer_ = nullptr;
    TouchPoint_t tp_;

    bool was_touched_;
    int press_count_;

    SemaphoreHandle_t touch_isr_mux_;
};

class MemoriaEspVocat : public WifiBoard {
private:
    // The ES7210 microphone array gain is configured to 36 dB (+6 dB from 30 dB)
    // to ensure normal 30-60 cm speech maintains healthy RMS above the gateway
    // noise floor and improves far-field wake word detection.
    // Strict local AFE VAD keeps room noise below the gateway noise-gate floor.
    static constexpr float kMicInputGainDb = 36.0f;

    i2c_master_bus_handle_t i2c_bus_;
    i2c_bus_handle_t shared_i2c_bus_handle_ = nullptr;
    Cst816s* cst816s_;
    Charge* charge_;
    Button boot_button_;
    LcdDisplay* display_ = nullptr;
    PwmBacklight* backlight_ = nullptr;
    esp_timer_handle_t touchpad_timer_;
    esp_timer_handle_t pat_restore_timer_ = nullptr;
    TaskHandle_t charge_task_handle_ = nullptr;
    TaskHandle_t touch_task_handle_ = nullptr;
    TaskHandle_t imu_task_handle_ = nullptr;
#if ESP_VOCAT_ENABLE_CAP_TOUCH_SENSOR
    TaskHandle_t touch_slider_task_handle_ = nullptr;
    touch_slider_handle_t touch_slider_handle_ = nullptr;
    touch_button_handle_t touch_button_handle_ = nullptr;
#endif
    bool bmi270_ready_ = false;
    bool was_charging_ = false;
    std::atomic<int64_t> imu_mute_until_ms_{0};

    static void battery_task(void* arg) {
        auto* self = static_cast<MemoriaEspVocat*>(arg);
        while (true) {
            if (self != nullptr && self->charge_ != nullptr) {
                self->charge_->Update();
                bool is_charging = self->charge_->IsCharging();
                if (is_charging != self->was_charging_) {
                    self->was_charging_ = is_charging;
                    ESP_LOGI(TAG, "Charging state changed: %s", is_charging ? "CHARGING" : "DISCHARGING");
                }
            }
            vTaskDelay(pdMS_TO_TICKS(1000));
        }
    }

    // BMI270 has no haptic motor. A body tap is a short accel impulse, not
    // conversation start, screen tap rumble, or a sustained shake.
    static constexpr int kPatDeltaThreshold = 6000;
    static constexpr int kPatQuietMax = 2500;
    static constexpr int kPatPulseMaxSamples = 2;
    static constexpr int kShakePersistMin = 3;
    static constexpr int64_t kPatCooldownMs = 2500;
    static constexpr int64_t kTouchImuMuteMs = 400;
    static constexpr uint64_t kPatFaceHoldUs = 1500 * 1000;

    static void PatRestoreCallback(void* arg) {
        auto* self = static_cast<MemoriaEspVocat*>(arg);
        if (self == nullptr) {
            return;
        }
        if (Application::GetInstance().GetDeviceState() != kDeviceStateIdle) {
            return;
        }
        auto* display = self->GetDisplay();
        if (display != nullptr) {
            display->SetEmotion("neutral");
        }
    }

    void EnsurePatRestoreTimer() {
        if (pat_restore_timer_ != nullptr) {
            return;
        }
        const esp_timer_create_args_t args = {
            .callback = &MemoriaEspVocat::PatRestoreCallback,
            .arg = this,
            .dispatch_method = ESP_TIMER_TASK,
            .name = "vocat_pat_restore",
            .skip_unhandled_events = true,
        };
        if (esp_timer_create(&args, &pat_restore_timer_) != ESP_OK) {
            pat_restore_timer_ = nullptr;
            ESP_LOGW(TAG, "pat restore timer unavailable");
        }
    }

    void OnDevicePat(int score) {
        auto& app = Application::GetInstance();
        const auto state = app.GetDeviceState();
        ESP_LOGI(TAG, "Device pat detected (score: %d) state=%d", score,
                 static_cast<int>(state));
        if (state != kDeviceStateIdle) {
            return;
        }
        auto* display = GetDisplay();
        if (display == nullptr) {
            return;
        }
        EnsurePatRestoreTimer();
        display->SetEmotion("surprised");
        if (pat_restore_timer_ != nullptr) {
            esp_timer_stop(pat_restore_timer_);
            esp_timer_start_once(pat_restore_timer_, kPatFaceHoldUs);
        }
    }

    static void imu_event_task(void* arg) {
        auto* self = static_cast<MemoriaEspVocat*>(arg);
        if (self == nullptr) {
            vTaskDelete(NULL);
            return;
        }

        int64_t last_pat_ms = 0;
        struct bmi2_sens_data prev = {};
        bool has_prev = false;
        int high_streak = 0;
        int peak_score = 0;

        while (true) {
            struct bmi2_sens_data cur = {};
            if (!Bmi270Motion::ReadAccelRaw(cur)) {
                has_prev = false;
                high_streak = 0;
                peak_score = 0;
                vTaskDelay(pdMS_TO_TICKS(20));
                continue;
            }
            if (has_prev) {
                int dx = abs(static_cast<int>(cur.acc.x) - static_cast<int>(prev.acc.x));
                int dy = abs(static_cast<int>(cur.acc.y) - static_cast<int>(prev.acc.y));
                int dz = abs(static_cast<int>(cur.acc.z) - static_cast<int>(prev.acc.z));
                int pat_score = dx + dy + dz;
                int64_t now_ms = esp_timer_get_time() / 1000;
                const bool muted =
                    now_ms < self->imu_mute_until_ms_.load(std::memory_order_relaxed);
                if (muted) {
                    high_streak = 0;
                    peak_score = 0;
                } else if (pat_score > kPatDeltaThreshold) {
                    high_streak++;
                    if (pat_score > peak_score) {
                        peak_score = pat_score;
                    }
                    if (high_streak == kShakePersistMin) {
                        ESP_LOGI(TAG, "Device shake ignored (score: %d)", peak_score);
                    }
                } else {
                    if (high_streak >= 1 && high_streak <= kPatPulseMaxSamples &&
                        pat_score < kPatQuietMax &&
                        (now_ms - last_pat_ms) > kPatCooldownMs) {
                        last_pat_ms = now_ms;
                        self->OnDevicePat(peak_score);
                    }
                    high_streak = 0;
                    peak_score = 0;
                }
            }
            prev = cur;
            has_prev = true;
            vTaskDelay(pdMS_TO_TICKS(20));
        }
    }

    void MuteImuForTouch() {
        imu_mute_until_ms_.store((esp_timer_get_time() / 1000) + kTouchImuMuteMs,
                                 std::memory_order_relaxed);
    }

    void HandleScreenTouchRelease() {
        MuteImuForTouch();
        auto& app = Application::GetInstance();
        const auto state = app.GetDeviceState();
        if (state == kDeviceStateStarting) {
            EnterWifiConfigMode();
            return;
        }
        if (state == kDeviceStateSpeaking) {
            app.AbortSpeaking(kAbortReasonNone);
            return;
        }
        if (state == kDeviceStateRecovering) {
            app.ToggleChatState();
            return;
        }
        if (state == kDeviceStateListening) {
            app.StopListening();
            return;
        }
        if (state == kDeviceStateIdle || state == kDeviceStateConnecting) {
            ESP_LOGI(TAG, "idle screen tap ignored; wake word or BOOT starts chat");
            return;
        }
        ESP_LOGI(TAG, "screen tap ignored in state=%d", static_cast<int>(state));
    }

    void InitializeI2c() {
        i2c_config_t i2c_cfg = {
            .mode = I2C_MODE_MASTER,
            .sda_io_num = AUDIO_CODEC_I2C_SDA_PIN,
            .scl_io_num = AUDIO_CODEC_I2C_SCL_PIN,
            .sda_pullup_en = true,
            .scl_pullup_en = true,
            .master =
                {
                    .clk_speed = 400000,
                },
            .clk_flags = 0,
        };
        shared_i2c_bus_handle_ = i2c_bus_create(I2C_NUM_0, &i2c_cfg);
        if (!shared_i2c_bus_handle_) {
            ESP_LOGE(TAG, "Failed to create shared I2C bus");
            ESP_ERROR_CHECK(ESP_FAIL);
        }
#if ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5, 3, 0) && !CONFIG_I2C_BUS_BACKWARD_CONFIG
        i2c_bus_ = i2c_bus_get_internal_bus_handle(shared_i2c_bus_handle_);
#else
#error "ESP-VoCat board requires i2c_bus_get_internal_bus_handle() support"
#endif
        if (!i2c_bus_) {
            ESP_LOGE(TAG, "Failed to get I2C master handle");
            ESP_ERROR_CHECK(ESP_FAIL);
        }

        temperature_sensor_config_t temp_sensor_config = TEMPERATURE_SENSOR_CONFIG_DEFAULT(10, 50);
        ESP_ERROR_CHECK(temperature_sensor_install(&temp_sensor_config, &temp_sensor));
        ESP_ERROR_CHECK(temperature_sensor_enable(temp_sensor));
    }

    uint8_t DetectPcbVersion() {
        gpio_config_t gpio_conf = {.pin_bit_mask = (1ULL << CORDEC_POWER_CTRL),
                                   .mode = GPIO_MODE_OUTPUT,
                                   .pull_up_en = GPIO_PULLUP_DISABLE,
                                   .pull_down_en = GPIO_PULLDOWN_DISABLE,
                                   .intr_type = GPIO_INTR_DISABLE};
        ESP_ERROR_CHECK(gpio_config(&gpio_conf));
        ESP_ERROR_CHECK(gpio_set_level(CORDEC_POWER_CTRL, 0));
        vTaskDelay(pdMS_TO_TICKS(50));

        bool codec_alive = (i2c_master_probe(i2c_bus_, 0x18, 100) == ESP_OK);
        uint8_t pcb_version = 0;
        if (codec_alive) {
            ESP_LOGI(TAG, "PCB version V1.0");
            pcb_version = 0;
        } else {
            ESP_ERROR_CHECK(gpio_set_level(CORDEC_POWER_CTRL, 1));
            vTaskDelay(pdMS_TO_TICKS(50));
            codec_alive = (i2c_master_probe(i2c_bus_, 0x18, 100) == ESP_OK);
            if (codec_alive) {
                ESP_LOGI(TAG, "PCB version V1.2");
                pcb_version = 1;
                AUDIO_I2S_GPIO_DIN = AUDIO_I2S_GPIO_DIN_2;
                AUDIO_CODEC_PA_PIN = AUDIO_CODEC_PA_PIN_2;
                QSPI_PIN_NUM_LCD_RST = QSPI_PIN_NUM_LCD_RST_2;
                TOUCH_PAD2 = TOUCH_PAD2_2;
                UART1_TX = UART1_TX_2;
                UART1_RX = UART1_RX_2;
            } else {
                ESP_LOGE(TAG, "PCB version detection error");
            }
        }
        return pcb_version;
    }

    static void touch_isr_callback(void* arg) {
        Cst816s* touchpad = static_cast<Cst816s*>(arg);
        if (touchpad != nullptr) {
            touchpad->NotifyTouchEvent();
        }
    }

    static void touch_event_task(void* arg) {
        Cst816s* touchpad = static_cast<Cst816s*>(arg);
        if (touchpad == nullptr) {
            ESP_LOGE(TAG, "Invalid touchpad pointer in touch_event_task");
            vTaskDelete(NULL);
            return;
        }

        while (true) {
            if (touchpad->WaitForTouchEvent()) {
                touchpad->UpdateTouchPoint();
                auto touch_event = touchpad->CheckTouchEvent();
                auto& board = static_cast<MemoriaEspVocat&>(Board::GetInstance());
                if (touch_event == Cst816s::TOUCH_PRESS ||
                    touch_event == Cst816s::TOUCH_HOLD) {
                    board.MuteImuForTouch();
                }
                if (touch_event == Cst816s::TOUCH_RELEASE) {
                    board.HandleScreenTouchRelease();
                }
            }
        }
    }

    void InitializeCharge() {
        charge_ = new Charge(i2c_bus_, 0x55);
        was_charging_ = charge_->IsCharging();
        xTaskCreatePinnedToCore(battery_task, "batteryTask", 3 * 1024, this, 6,
                                &charge_task_handle_, 0);
    }

    void InitializeCst816sTouchPad() {
        cst816s_ = new Cst816s(i2c_bus_, 0x15);

        xTaskCreatePinnedToCore(touch_event_task, "touch_task", 4 * 1024, cst816s_, 5,
                                &touch_task_handle_, 1);

        const gpio_config_t int_gpio_config = {.pin_bit_mask = (1ULL << TP_PIN_NUM_INT),
                                               .mode = GPIO_MODE_INPUT,
                                               .intr_type = GPIO_INTR_ANYEDGE};
        gpio_config(&int_gpio_config);
        gpio_install_isr_service(0);
        gpio_intr_enable(TP_PIN_NUM_INT);
        gpio_isr_handler_add(TP_PIN_NUM_INT, MemoriaEspVocat::touch_isr_callback, cst816s_);
    }

    void InitializeBmi270() {
        esp_err_t imu_ret = Bmi270Motion::Initialize(shared_i2c_bus_handle_);
        if (imu_ret == ESP_OK) {
            bmi270_ready_ = true;
            xTaskCreatePinnedToCore(imu_event_task, "imu_task", 4 * 1024, this, 4,
                                    &imu_task_handle_, 1);
        } else {
            ESP_LOGW(TAG, "BMI270 unavailable, pat motion disabled");
        }
    }

#if ESP_VOCAT_ENABLE_CAP_TOUCH_SENSOR
    static uint32_t TouchChannelFromPadGpio(gpio_num_t gpio) {
        if (gpio == GPIO_NUM_NC) {
            return 0;
        }
        if (gpio >= GPIO_NUM_1 && gpio <= GPIO_NUM_14) {
            return static_cast<uint32_t>(gpio);
        }
        return 0;
    }

    static void touch_cap_poll_task(void* arg) {
        auto* self = static_cast<MemoriaEspVocat*>(arg);
        while (true) {
            if (self != nullptr) {
                if (self->touch_slider_handle_ != nullptr) {
                    touch_slider_sensor_handle_events(self->touch_slider_handle_);
                } else if (self->touch_button_handle_ != nullptr) {
                    touch_button_sensor_handle_events(self->touch_button_handle_);
                }
            }
            vTaskDelay(pdMS_TO_TICKS(20));
        }
    }

    void InitializeCapacitiveTouchPads() {
        if (TOUCH_PAD1 == GPIO_NUM_NC) {
            ESP_LOGW(TAG, "Capacitive touch disabled: TOUCH_PAD1 NC");
            return;
        }

        const uint32_t ch1 = TouchChannelFromPadGpio(TOUCH_PAD1);
        if (ch1 == 0) {
            ESP_LOGW(TAG, "TOUCH_PAD1 GPIO %d is not a touch channel (expect GPIO1..GPIO14)",
                     (int)TOUCH_PAD1);
            return;
        }

        if (TOUCH_PAD2 != GPIO_NUM_NC) {
            const uint32_t ch2 = TouchChannelFromPadGpio(TOUCH_PAD2);
            if (ch2 == 0) {
                ESP_LOGW(TAG, "TOUCH_PAD2 GPIO %d is not a touch channel", (int)TOUCH_PAD2);
                return;
            }

            static uint32_t slider_ch[2];
            static float slider_thr[2];
            slider_ch[0] = ch1;
            slider_ch[1] = ch2;
            slider_thr[0] = 0.004f;
            slider_thr[1] = 0.006f;

            touch_slider_config_t sld_cfg = {
                .channel_num = 2,
                .channel_list = slider_ch,
                .channel_threshold = slider_thr,
                .channel_gold_value = nullptr,
                .debounce_times = 1,
                .filter_reset_times = 5,
                .position_range = 10000,
                .calculate_window = 2,
                .swipe_threshold = 28.f,
                .swipe_hysterisis = 22.f,
                .swipe_alpha = 0.9f,
                .skip_lowlevel_init = false,
            };
            esp_err_t err = touch_slider_sensor_create(&sld_cfg, &touch_slider_handle_, nullptr, this);
            if (err != ESP_OK) {
                ESP_LOGW(TAG, "touch_slider_sensor_create failed: %s", esp_err_to_name(err));
                touch_slider_handle_ = nullptr;
                return;
            }
            xTaskCreatePinnedToCore(touch_cap_poll_task, "touch_cap", 3072, this, 3,
                                    &touch_slider_task_handle_, 1);
            ESP_LOGI(TAG, "Touch slider (PCB v1.2+): PAD1 GPIO%d ch%u, PAD2 GPIO%d ch%u",
                     (int)TOUCH_PAD1, (unsigned)slider_ch[0], (int)TOUCH_PAD2,
                     (unsigned)slider_ch[1]);
            return;
        }

        static uint32_t btn_ch[1];
        static float btn_thr[1];
        btn_ch[0] = ch1;
        btn_thr[0] = 0.004f;

        touch_button_config_t btn_cfg = {
            .channel_num = 1,
            .channel_list = btn_ch,
            .channel_threshold = btn_thr,
            .channel_gold_value = nullptr,
            .debounce_times = 2,
            .skip_lowlevel_init = false,
        };
        esp_err_t err = touch_button_sensor_create(&btn_cfg, &touch_button_handle_, nullptr, this);
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "touch_button_sensor_create failed: %s", esp_err_to_name(err));
            touch_button_handle_ = nullptr;
            return;
        }
        xTaskCreatePinnedToCore(touch_cap_poll_task, "touch_cap", 3072, this, 3,
                                &touch_slider_task_handle_, 1);
        ESP_LOGI(TAG, "Touch button (PCB v1.0): TOUCH_PAD1 GPIO%d ch%u", (int)TOUCH_PAD1,
                 (unsigned)btn_ch[0]);
    }
#endif

    void InitializeSpi() {
        const spi_bus_config_t bus_config = TAIJIPI_ST77916_PANEL_BUS_QSPI_CONFIG(
            QSPI_PIN_NUM_LCD_PCLK, QSPI_PIN_NUM_LCD_DATA0, QSPI_PIN_NUM_LCD_DATA1,
            QSPI_PIN_NUM_LCD_DATA2, QSPI_PIN_NUM_LCD_DATA3, QSPI_LCD_H_RES * 80 * sizeof(uint16_t));
        ESP_ERROR_CHECK(spi_bus_initialize(QSPI_LCD_HOST, &bus_config, SPI_DMA_CH_AUTO));
    }

    void InitializeSt77916Display(uint8_t pcb_version) {
        esp_lcd_panel_io_handle_t panel_io = nullptr;
        esp_lcd_panel_handle_t panel = nullptr;

        esp_lcd_panel_io_spi_config_t io_config = {};
        io_config.cs_gpio_num = QSPI_PIN_NUM_LCD_CS;
        io_config.dc_gpio_num = GPIO_NUM_NC;
        io_config.spi_mode = 0;
        io_config.pclk_hz = 40 * 1000 * 1000;
        io_config.trans_queue_depth = 10;
        io_config.lcd_cmd_bits = 32;
        io_config.lcd_param_bits = 8;
        io_config.flags.quad_mode = true;
        ESP_ERROR_CHECK(esp_lcd_new_panel_io_spi((esp_lcd_spi_bus_handle_t)QSPI_LCD_HOST,
                                                 &io_config, &panel_io));
        st77916_vendor_config_t vendor_config = {
            .init_cmds = vendor_specific_init_yysj,
            .init_cmds_size = sizeof(vendor_specific_init_yysj) / sizeof(st77916_lcd_init_cmd_t),
            .flags =
                {
                    .use_qspi_interface = 1,
                },
        };
        esp_lcd_panel_dev_config_t panel_config = {};
        panel_config.reset_gpio_num = QSPI_PIN_NUM_LCD_RST;
        panel_config.rgb_ele_order = LCD_RGB_ELEMENT_ORDER_RGB;
        panel_config.bits_per_pixel = QSPI_LCD_BIT_PER_PIXEL;
        panel_config.flags.reset_active_high = pcb_version;
        panel_config.vendor_config = &vendor_config;
        ESP_ERROR_CHECK(esp_lcd_new_panel_st77916(panel_io, &panel_config, &panel));

        esp_lcd_panel_reset(panel);
        esp_lcd_panel_init(panel);
        esp_lcd_panel_disp_on_off(panel, true);
        esp_lcd_panel_swap_xy(panel, DISPLAY_SWAP_XY);
        esp_lcd_panel_mirror(panel, DISPLAY_MIRROR_X, DISPLAY_MIRROR_Y);

        display_ = new MemoriaFaceDisplay(panel_io, panel, DISPLAY_WIDTH, DISPLAY_HEIGHT,
                                          DISPLAY_OFFSET_X, DISPLAY_OFFSET_Y, DISPLAY_MIRROR_X,
                                          DISPLAY_MIRROR_Y, DISPLAY_SWAP_XY);
        backlight_ = new PwmBacklight(DISPLAY_BACKLIGHT_PIN, DISPLAY_BACKLIGHT_OUTPUT_INVERT);
        backlight_->RestoreBrightness();
    }

    void InitializeButtons() {
        boot_button_.OnClick([this]() {
            auto& app = Application::GetInstance();
            const auto state = app.GetDeviceState();

            if (state == kDeviceStateStarting) {
                EnterWifiConfigMode();
            } else if (state == kDeviceStateSpeaking) {
                app.AbortSpeaking(kAbortReasonNone);
            } else if (state == kDeviceStateRecovering) {
                // The physical stop remains authoritative while the transport
                // is down; ToggleChatState terminally cancels recovery.
                app.ToggleChatState();
            } else if (state == kDeviceStateListening) {
                app.StopListening();
            } else if (state == kDeviceStateWifiConfiguring) {
                GetDisplay()->ShowNotification("WiFi 配网中", 1500);
            } else {
                app.ToggleChatState();
            }
        });

        boot_button_.OnLongPress([this]() {
            const auto state = Application::GetInstance().GetDeviceState();
            if (state == kDeviceStateWifiConfiguring) {
                GetDisplay()->ShowNotification("WiFi 配网中", 1500);
                return;
            }
            if (state == kDeviceStateRecovering) {
                auto& app = Application::GetInstance();
                app.ToggleChatState();
                app.Schedule([this]() { EnterWifiConfigMode(); });
                return;
            }
            if (state == kDeviceStateStarting || state == kDeviceStateIdle) {
                Settings runtime("memoria_runtime", true);
                if (state == kDeviceStateIdle || runtime.GetInt("activation_v", 0) == 0) {
                    Application::GetInstance().Schedule([this]() {
                        if (memoria::MemoriaBootstrap::GetInstance().Start(display_) == ESP_OK) {
                            return;
                        }
                        GetDisplay()->ShowNotification("正在进入配网", 1500);
                        EnterWifiConfigMode();
                    });
                    return;
                }
            }
            GetDisplay()->ShowNotification("正在进入配网", 1500);
            EnterWifiConfigMode();
        });

        gpio_config_t power_gpio_config = {
            .pin_bit_mask = (BIT64(POWER_CTRL)),
            .mode = GPIO_MODE_OUTPUT,
            .pull_up_en = GPIO_PULLUP_DISABLE,
            .pull_down_en = GPIO_PULLDOWN_DISABLE,
            .intr_type = GPIO_INTR_DISABLE,
        };
        ESP_ERROR_CHECK(gpio_config(&power_gpio_config));
        gpio_set_level(POWER_CTRL, 0);
    }

public:
    ~MemoriaEspVocat() {
        if (pat_restore_timer_ != nullptr) {
            esp_timer_stop(pat_restore_timer_);
            esp_timer_delete(pat_restore_timer_);
            pat_restore_timer_ = nullptr;
        }
        if (charge_task_handle_ != nullptr) {
            vTaskDelete(charge_task_handle_);
        }
        if (touch_task_handle_ != nullptr) {
            vTaskDelete(touch_task_handle_);
        }
        if (imu_task_handle_ != nullptr) {
            vTaskDelete(imu_task_handle_);
        }
#if ESP_VOCAT_ENABLE_CAP_TOUCH_SENSOR
        if (touch_slider_task_handle_ != nullptr) {
            vTaskDelete(touch_slider_task_handle_);
            touch_slider_task_handle_ = nullptr;
        }
        if (touch_slider_handle_ != nullptr) {
            touch_slider_sensor_delete(touch_slider_handle_);
            touch_slider_handle_ = nullptr;
        }
        if (touch_button_handle_ != nullptr) {
            touch_button_sensor_delete(touch_button_handle_);
            touch_button_handle_ = nullptr;
        }
#endif

        delete charge_;
        delete cst816s_;
        delete display_;

        gpio_isr_handler_remove(TP_PIN_NUM_INT);

        if (temp_sensor != NULL) {
            temperature_sensor_disable(temp_sensor);
            temperature_sensor_uninstall(temp_sensor);
            temp_sensor = NULL;
        }
    }

    MemoriaEspVocat() : boot_button_(BOOT_BUTTON_GPIO) {
        InitializeI2c();
        uint8_t pcb_version = DetectPcbVersion();
        InitializeCharge();
        InitializeCst816sTouchPad();
        InitializeBmi270();

        InitializeSpi();
        InitializeSt77916Display(pcb_version);
        InitializeButtons();
#if ESP_VOCAT_ENABLE_CAP_TOUCH_SENSOR
        InitializeCapacitiveTouchPads();
#endif
    }

    virtual AudioCodec* GetAudioCodec() override {
        static BoxAudioCodec audio_codec(
            i2c_bus_, AUDIO_INPUT_SAMPLE_RATE, AUDIO_OUTPUT_SAMPLE_RATE, AUDIO_I2S_GPIO_MCLK,
            AUDIO_I2S_GPIO_BCLK, AUDIO_I2S_GPIO_WS, AUDIO_I2S_GPIO_DOUT, AUDIO_I2S_GPIO_DIN,
            AUDIO_CODEC_PA_PIN, AUDIO_CODEC_ES8311_ADDR, AUDIO_CODEC_ES7210_ADDR,
            AUDIO_INPUT_REFERENCE, kMicInputGainDb);
        return &audio_codec;
    }

    virtual Display* GetDisplay() override { return display_; }

    Cst816s* GetTouchpad() { return cst816s_; }

    virtual Backlight* GetBacklight() override { return backlight_; }

    virtual bool GetBatteryLevel(int& level, bool& charging, bool& discharging) override {
        if (charge_ == nullptr) {
            return false;
        }
        level = charge_->GetBatteryLevel();
        charging = charge_->IsCharging();
        discharging = charge_->IsDischarging();
        return true;
    }
};

DECLARE_BOARD(MemoriaEspVocat);
