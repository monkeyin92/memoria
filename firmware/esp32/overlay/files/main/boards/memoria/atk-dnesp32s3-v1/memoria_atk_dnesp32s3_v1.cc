#include "wifi_board.h"
#include "codecs/es8388_audio_codec.h"
#include "display/lcd_display.h"
#include "application.h"
#include "button.h"
#include "config.h"
#include "i2c_device.h"
#include "led/single_led.h"
#include "memoria_bootstrap.h"
#include "settings.h"

#include <esp_log.h>
#include <esp_lcd_panel_vendor.h>
#include <driver/i2c_master.h>
#include <driver/spi_common.h>

#define TAG "memoria-atk-dnesp32s3-v1"

namespace {

// Board-level acoustic calibration authority. ES8388 exposes 3 dB PGA steps;
// keep this explicit so noise/VAD tuning cannot silently override sensitivity.
constexpr float kMicInputGainDb = 18.0f;

}  // namespace

class XL9555 : public I2cDevice {
public:
    XL9555(i2c_master_bus_handle_t i2c_bus, uint8_t addr) : I2cDevice(i2c_bus, addr) {
        WriteReg(0x06, 0x03);
        WriteReg(0x07, 0xF0);
    }

    void SetOutputState(uint8_t bit, uint8_t level) {
        uint16_t data;
        int index = bit;

        if (bit < 8) {
            data = ReadReg(0x02);
        } else {
            data = ReadReg(0x03);
            index -= 8;
        }

        data = (data & ~(1 << index)) | (level << index);

        if (bit < 8) {
            WriteReg(0x02, data);
        } else {
            WriteReg(0x03, data);
        }
    }
};

class MemoriaBacklight final : public Backlight {
public:
    explicit MemoriaBacklight(XL9555* expander) : expander_(expander) {}

protected:
    void SetBrightnessImpl(uint8_t brightness) override {
        // The board exposes only a binary XL9555 gate, not PWM. Preserve the
        // server's 0/non-zero safety boundary and do not claim fractional
        // luminance control on this hardware revision.
        expander_->SetOutputState(8, brightness > 0 ? 1 : 0);
    }

private:
    XL9555* expander_;
};

class MemoriaAtkDnesp32s3V1 : public WifiBoard {
private:
    i2c_master_bus_handle_t i2c_bus_;
    Button boot_button_;
    LcdDisplay* display_;
    XL9555* xl9555_;

    void InitializeI2c() {
        i2c_master_bus_config_t i2c_bus_cfg = {
            .i2c_port = I2C_NUM_0,
            .sda_io_num = AUDIO_CODEC_I2C_SDA_PIN,
            .scl_io_num = AUDIO_CODEC_I2C_SCL_PIN,
            .clk_source = I2C_CLK_SRC_DEFAULT,
            .glitch_ignore_cnt = 7,
            .intr_priority = 0,
            .trans_queue_depth = 0,
            .flags = {
                .enable_internal_pullup = 1,
            },
        };
        ESP_ERROR_CHECK(i2c_new_master_bus(&i2c_bus_cfg, &i2c_bus_));
        xl9555_ = new XL9555(i2c_bus_, 0x20);
    }

    void InitializeSpi() {
        spi_bus_config_t buscfg = {};
        buscfg.mosi_io_num = LCD_MOSI_PIN;
        buscfg.miso_io_num = GPIO_NUM_NC;
        buscfg.sclk_io_num = LCD_SCLK_PIN;
        buscfg.quadwp_io_num = GPIO_NUM_NC;
        buscfg.quadhd_io_num = GPIO_NUM_NC;
        buscfg.max_transfer_sz = DISPLAY_WIDTH * DISPLAY_HEIGHT * sizeof(uint16_t);
        ESP_ERROR_CHECK(spi_bus_initialize(SPI2_HOST, &buscfg, SPI_DMA_CH_AUTO));
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
                // Cancel the resumable Session before WifiBoard resets the
                // protocol object for provisioning. Both callbacks execute in
                // the main loop: ToggleChat is handled before scheduled work,
                // so EnterWifiConfigMode observes the resulting idle state.
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
    }

    void InitializeSt7789Display() {
        esp_lcd_panel_io_handle_t panel_io = nullptr;
        esp_lcd_panel_handle_t panel = nullptr;
        ESP_LOGD(TAG, "Install panel IO");

        esp_lcd_panel_io_spi_config_t io_config = {};
        io_config.cs_gpio_num = LCD_CS_PIN;
        io_config.dc_gpio_num = LCD_DC_PIN;
        io_config.spi_mode = 0;
        io_config.pclk_hz = 20 * 1000 * 1000;
        io_config.trans_queue_depth = 7;
        io_config.lcd_cmd_bits = 8;
        io_config.lcd_param_bits = 8;
        ESP_ERROR_CHECK(esp_lcd_new_panel_io_spi(SPI2_HOST, &io_config, &panel_io));

        ESP_LOGD(TAG, "Install LCD driver");
        esp_lcd_panel_dev_config_t panel_config = {};
        panel_config.reset_gpio_num = GPIO_NUM_NC;
        panel_config.rgb_ele_order = LCD_RGB_ELEMENT_ORDER_RGB;
        panel_config.bits_per_pixel = 16;
        panel_config.data_endian = LCD_RGB_DATA_ENDIAN_BIG;
        ESP_ERROR_CHECK(esp_lcd_new_panel_st7789(panel_io, &panel_config, &panel));

        ESP_ERROR_CHECK(esp_lcd_panel_reset(panel));
        xl9555_->SetOutputState(8, 1);
        xl9555_->SetOutputState(2, 0);

        ESP_ERROR_CHECK(esp_lcd_panel_init(panel));
        ESP_ERROR_CHECK(esp_lcd_panel_invert_color(panel, DISPLAY_BACKLIGHT_OUTPUT_INVERT));
        ESP_ERROR_CHECK(esp_lcd_panel_swap_xy(panel, DISPLAY_SWAP_XY));
        ESP_ERROR_CHECK(esp_lcd_panel_mirror(panel, DISPLAY_MIRROR_X, DISPLAY_MIRROR_Y));
        display_ = new SpiLcdDisplay(panel_io, panel, DISPLAY_WIDTH, DISPLAY_HEIGHT,
                                     DISPLAY_OFFSET_X, DISPLAY_OFFSET_Y, DISPLAY_MIRROR_X,
                                     DISPLAY_MIRROR_Y, DISPLAY_SWAP_XY);
    }

public:
    MemoriaAtkDnesp32s3V1()
        : boot_button_(BOOT_BUTTON_GPIO, false, MEMORIA_BUTTON_LONG_PRESS_MS, 0) {
        InitializeI2c();
        InitializeSpi();
        InitializeSt7789Display();
        InitializeButtons();
    }

    virtual Led* GetLed() override {
        static SingleLed led(BUILTIN_LED_GPIO);
        return &led;
    }

    virtual AudioCodec* GetAudioCodec() override {
        static Es8388AudioCodec audio_codec(
            i2c_bus_, I2C_NUM_0, AUDIO_INPUT_SAMPLE_RATE, AUDIO_OUTPUT_SAMPLE_RATE,
            AUDIO_I2S_GPIO_MCLK, AUDIO_I2S_GPIO_BCLK, AUDIO_I2S_GPIO_WS,
            AUDIO_I2S_GPIO_DOUT, AUDIO_I2S_GPIO_DIN, GPIO_NUM_NC,
            AUDIO_CODEC_ES8388_ADDR);
        // Upstream's 24 dB kept the old ASR VAD open on the board's broadband
        // noise floor. The first correction to 12 dB over-attenuated ordinary
        // speaking distance after DTLN. Keep the strict local AFE VAD and use
        // the ES8388's midpoint 18 dB PGA step: 6 dB more speech headroom than
        // 12 dB while retaining 6 dB noise reduction from upstream.
        static bool input_gain_configured = false;
        if (!input_gain_configured) {
            audio_codec.SetInputGain(kMicInputGainDb);
            input_gain_configured = true;
        }
        return &audio_codec;
    }

    virtual Display* GetDisplay() override {
        return display_;
    }

    virtual Backlight* GetBacklight() override {
        static MemoriaBacklight backlight(xl9555_);
        return &backlight;
    }
};

DECLARE_BOARD(MemoriaAtkDnesp32s3V1);
