#include "memoria_mascot_display.h"

#include "application.h"
#include "assets.h"
#include "assets/lang_config.h"
#include "backlight.h"
#include "display/lvgl_display/lvgl_theme.h"
#include "settings.h"
#include "system_info.h"

#include <esp_heap_caps.h>
#include <esp_log.h>
#include <esp_random.h>
#include <esp_timer.h>
#include <miniz.h>

#include <cstring>

#define TAG "MemoriaMascot"

namespace {

constexpr const char* kSettingsNamespace = "memoria_ui";
constexpr const char* kCompanionKey = "companion";
constexpr uint32_t kErrorHoldMs = 8000;
constexpr int kPillRadius = 18;

struct CompanionName {
    const char* id;
    const char* name;
};
// Display names from services/common/companions.py (Mini Program catalogue).
constexpr CompanionName kCompanions[] = {
    {"starlight", "星澜"}, {"taoxi", "桃喜"}, {"mianmian", "绵绵"},
    {"axu", "阿序"},       {"xuanmo", "玄墨"},
};

const char* CompanionDisplayName(const std::string& id) {
    for (const auto& companion : kCompanions) {
        if (id == companion.id) {
            return companion.name;
        }
    }
    return kCompanions[0].name;
}

void* PsramAlloc(std::size_t bytes) {
    void* ptr = heap_caps_malloc(bytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    return ptr != nullptr ? ptr : heap_caps_malloc(bytes, MALLOC_CAP_8BIT);
}

void PsramFree(void* ptr) { heap_caps_free(ptr); }

bool RomInflate(const uint8_t* in, std::size_t in_size, uint8_t* out, std::size_t out_size) {
    auto* decompressor = static_cast<tinfl_decompressor*>(
        heap_caps_malloc(sizeof(tinfl_decompressor), MALLOC_CAP_8BIT));
    if (decompressor == nullptr) {
        return false;
    }
    tinfl_init(decompressor);
    std::size_t in_bytes = in_size;
    std::size_t out_bytes = out_size;
    const tinfl_status status = tinfl_decompress(
        decompressor, in, &in_bytes, out, out, &out_bytes,
        TINFL_FLAG_PARSE_ZLIB_HEADER | TINFL_FLAG_USING_NON_WRAPPING_OUTPUT_BUF);
    heap_caps_free(decompressor);
    return status == TINFL_STATUS_DONE && out_bytes == out_size;
}

lv_color_t Color(uint32_t rgb) { return lv_color_hex(rgb); }

bool Equals(const char* a, const char* b) { return a != nullptr && b != nullptr && std::strcmp(a, b) == 0; }

void StylePill(lv_obj_t* obj, uint32_t ink) {
    lv_obj_set_style_bg_color(obj, lv_color_white(), 0);
    lv_obj_set_style_bg_opa(obj, LV_OPA_70, 0);
    lv_obj_set_style_radius(obj, kPillRadius, 0);
    lv_obj_set_style_border_width(obj, 0, 0);
    lv_obj_set_style_text_color(obj, Color(ink), 0);
}

}  // namespace

bool MemoriaMascotDisplay::IsKnownCompanion(const char* companion_id) {
    if (companion_id == nullptr) {
        return false;
    }
    for (const auto& companion : kCompanions) {
        if (std::strcmp(companion_id, companion.id) == 0) {
            return true;
        }
    }
    return false;
}

uint32_t MemoriaMascotDisplay::NowMs() { return static_cast<uint32_t>(esp_timer_get_time() / 1000); }

MemoriaMascotDisplay::MemoriaMascotDisplay(esp_lcd_panel_io_handle_t panel_io,
                                           esp_lcd_panel_handle_t panel, int width, int height,
                                           int offset_x, int offset_y, bool mirror_x,
                                           bool mirror_y, bool swap_xy)
    : SpiLcdDisplay(panel_io, panel, width, height, offset_x, offset_y, mirror_x, mirror_y,
                    swap_xy) {
    input_mutex_ = xSemaphoreCreateMutex();
    if (width_ != memoria::MascotScene::kSize || height_ != memoria::MascotScene::kSize) {
        ESP_LOGE(TAG, "mascot scene needs a %dx%d panel", memoria::MascotScene::kSize,
                 memoria::MascotScene::kSize);
        return;
    }
    const std::size_t bytes = static_cast<std::size_t>(width_) * height_ * sizeof(uint16_t);
    framebuffer_ = static_cast<uint16_t*>(PsramAlloc(bytes));
    if (framebuffer_ == nullptr) {
        ESP_LOGE(TAG, "framebuffer allocation failed (%u bytes)", static_cast<unsigned>(bytes));
        return;
    }
    std::memset(framebuffer_, 0, bytes);
    // LvglAllocatedImage owns the framebuffer from here on.
    frame_image_ = std::make_unique<LvglAllocatedImage>(framebuffer_, bytes, width_, height_,
                                                        width_ * static_cast<int>(sizeof(uint16_t)),
                                                        LV_COLOR_FORMAT_RGB565);
    scene_ = std::make_unique<memoria::MascotScene>(framebuffer_, PsramAlloc, PsramFree);
    if (!scene_->Init()) {
        ESP_LOGE(TAG, "scene allocation failed");
        scene_.reset();
        return;
    }
    scene_->Seed(esp_random());

    Settings settings(kSettingsNamespace, false);
    const std::string saved = settings.GetString(kCompanionKey, "starlight");
    companion_id_ = IsKnownCompanion(saved.c_str()) ? saved : std::string("starlight");

    void* data = nullptr;
    std::size_t size = 0;
    if (Assets::GetInstance().GetAssetData("brand_mark.mma", data, size) &&
        memoria::LoadBrandMark(static_cast<const uint8_t*>(data), size, PsramAlloc, PsramFree,
                               RomInflate, &brand_)) {
        scene_->SetBrand(&brand_);
    } else {
        ESP_LOGW(TAG, "brand mark unavailable");
    }
    if (LoadCompanion(companion_id_, &pack_)) {
        scene_->SetPack(pack_.get(), NowMs());
    }
}

MemoriaMascotDisplay::~MemoriaMascotDisplay() {
    running_.store(false);
    // The task exits within one frame; give it time before tearing down.
    for (int i = 0; i < 20 && task_ != nullptr; ++i) {
        vTaskDelay(pdMS_TO_TICKS(kFrameMs));
    }
    if (Lock(1000)) {
        if (container_ != nullptr) {
            lv_obj_set_style_bg_image_src(container_, nullptr, 0);
        }
        Unlock();
    }
    scene_.reset();
    pack_.reset();
    if (brand_.alpha != nullptr) {
        PsramFree(brand_.alpha);
        brand_.alpha = nullptr;
    }
    frame_image_.reset();  // frees the framebuffer
    framebuffer_ = nullptr;
    if (input_mutex_ != nullptr) {
        vSemaphoreDelete(input_mutex_);
    }
}

bool MemoriaMascotDisplay::LoadCompanion(const std::string& id,
                                         std::unique_ptr<memoria::MascotPack>* out) {
    const std::string name = "mascot_" + id + ".mmp";
    void* data = nullptr;
    std::size_t size = 0;
    if (!Assets::GetInstance().GetAssetData(name, data, size)) {
        ESP_LOGE(TAG, "asset %s missing", name.c_str());
        return false;
    }
    auto pack = std::make_unique<memoria::MascotPack>(PsramAlloc, PsramFree, RomInflate);
    const int64_t started = esp_timer_get_time();
    if (!pack->Load(static_cast<const uint8_t*>(data), size)) {
        ESP_LOGE(TAG, "asset %s invalid", name.c_str());
        return false;
    }
    ESP_LOGI(TAG, "companion=%s decoded in %lld ms", id.c_str(),
             static_cast<long long>((esp_timer_get_time() - started) / 1000));
    *out = std::move(pack);
    return true;
}

void MemoriaMascotDisplay::SetupUI() {
    SpiLcdDisplay::SetupUI();
    if (bottom_bar_ != nullptr && Lock(1000)) {
        bottom_bar_height_ = lv_obj_get_style_height(bottom_bar_, LV_PART_MAIN);
        Unlock();
    }
    // The backdrop is light, so text follows the light theme and then the
    // companion's ink colour.
    auto* light_theme = LvglThemeManager::GetInstance().GetTheme("light");
    if (light_theme != nullptr && current_theme_ != light_theme) {
        SetTheme(light_theme);  // our override re-applies the chrome
    } else if (Lock(1000)) {
        ApplyChrome();
        Unlock();
    }
    if (scene_ == nullptr || frame_image_ == nullptr) {
        return;
    }
    scene_->StartIntro(NowMs());
    running_.store(true);
    // Below the audio tasks (AFE 3, Opus 5, output 6, input 8) on LVGL's core.
    if (xTaskCreatePinnedToCore(&MemoriaMascotDisplay::AnimationTask, "mascot_anim", 6144, this, 2,
                                &task_, 1) != pdPASS) {
        task_ = nullptr;
        running_.store(false);
        ESP_LOGE(TAG, "animation task unavailable");
    }
}

void MemoriaMascotDisplay::ApplyChrome() {
    if (container_ == nullptr) {
        return;
    }
    const uint32_t ink = scene_ != nullptr ? scene_->theme().ink : 0x22344D;
    lv_obj_set_style_bg_color(container_, lv_color_black(), 0);
    if (frame_image_ != nullptr) {
        lv_obj_set_style_bg_image_src(container_, frame_image_->image_dsc(), 0);
        lv_obj_set_style_bg_image_opa(container_, LV_OPA_COVER, 0);
    }
    if (emoji_box_ != nullptr) {
        lv_obj_add_flag(emoji_box_, LV_OBJ_FLAG_HIDDEN);
    }
    // The square status icons sit outside the round panel; the scene speaks
    // for the device state instead.
    if (top_bar_ != nullptr) {
        lv_obj_add_flag(top_bar_, LV_OBJ_FLAG_HIDDEN);
    }
    if (caption_layout_) {
        // Caption band under the shrunken mascot: plain ink text, the status
        // or network notice first, then up to two lines of detail.
        constexpr int kLineY = memoria::MascotScene::kCaptionTextY;
        if (status_bar_ != nullptr) {
            lv_obj_set_style_bg_opa(status_bar_, LV_OPA_TRANSP, 0);
            lv_obj_align(status_bar_, LV_ALIGN_TOP_MID, 0, kLineY - 4);
        }
        if (status_label_ != nullptr) {
            lv_obj_set_width(status_label_, 250);
            lv_obj_set_style_text_color(status_label_, Color(ink), 0);
            lv_obj_set_style_text_opa(status_label_, LV_OPA_COVER, 0);
        }
        if (notification_label_ != nullptr) {
            lv_obj_set_width(notification_label_, 250);
            lv_obj_set_style_bg_opa(notification_label_, LV_OPA_TRANSP, 0);
            lv_obj_set_style_pad_all(notification_label_, 0, 0);
            lv_obj_set_style_text_color(notification_label_, Color(ink), 0);
        }
        if (bottom_bar_ != nullptr) {
            lv_obj_set_size(bottom_bar_, 244, LV_SIZE_CONTENT);
            lv_obj_set_style_bg_opa(bottom_bar_, LV_OPA_TRANSP, 0);
            lv_obj_set_style_pad_all(bottom_bar_, 0, 0);
            lv_obj_align(bottom_bar_, LV_ALIGN_TOP_MID, 0, kLineY + 32);
        }
        if (chat_message_label_ != nullptr) {
            lv_obj_set_width(chat_message_label_, 236);
            lv_label_set_long_mode(chat_message_label_, LV_LABEL_LONG_WRAP);
            lv_obj_align(chat_message_label_, LV_ALIGN_TOP_MID, 0, 0);
            lv_obj_set_style_text_color(chat_message_label_, Color(ink), 0);
            lv_obj_set_style_text_opa(chat_message_label_, LV_OPA_80, 0);
        }
    } else {
        if (status_bar_ != nullptr) {
            lv_obj_set_style_bg_opa(status_bar_, LV_OPA_TRANSP, 0);
            lv_obj_align(status_bar_, LV_ALIGN_TOP_MID, 0, 26);
        }
        if (status_label_ != nullptr) {
            lv_obj_set_width(status_label_, 200);
            lv_obj_set_style_text_color(status_label_, Color(ink), 0);
            lv_obj_set_style_text_opa(status_label_, LV_OPA_80, 0);
        }
        if (notification_label_ != nullptr) {
            lv_obj_set_width(notification_label_, 208);
            StylePill(notification_label_, ink);
            lv_obj_set_style_pad_ver(notification_label_, 4, 0);
            lv_obj_set_style_pad_hor(notification_label_, 12, 0);
        }
        if (bottom_bar_ != nullptr) {
            if (bottom_bar_height_ > 0) {
                lv_obj_set_height(bottom_bar_, bottom_bar_height_);
            }
            lv_obj_set_width(bottom_bar_, 256);
            StylePill(bottom_bar_, ink);
            lv_obj_set_style_pad_all(bottom_bar_, 0, 0);
            lv_obj_align(bottom_bar_, LV_ALIGN_BOTTOM_MID, 0, -28);
        }
        if (chat_message_label_ != nullptr) {
            lv_obj_set_width(chat_message_label_, 228);
            lv_label_set_long_mode(chat_message_label_, LV_LABEL_LONG_SCROLL_CIRCULAR);
            lv_obj_align(chat_message_label_, LV_ALIGN_CENTER, 0, 0);
            lv_obj_set_style_text_color(chat_message_label_, Color(ink), 0);
            lv_obj_set_style_text_opa(chat_message_label_, LV_OPA_COVER, 0);
        }
    }
    if (low_battery_popup_ != nullptr) {
        lv_obj_set_size(low_battery_popup_, 200, LV_SIZE_CONTENT);
        lv_obj_set_style_pad_ver(low_battery_popup_, 6, 0);
        lv_obj_set_style_radius(low_battery_popup_, kPillRadius, 0);
        lv_obj_align(low_battery_popup_, LV_ALIGN_TOP_MID, 0, 58);
    }
    StyleQrCard();
    ApplyChromeOpacity(chrome_opa_);
    lv_obj_invalidate(lv_screen_active());
}

void MemoriaMascotDisplay::ApplyChromeOpacity(uint8_t opa) {
    chrome_opa_ = opa;
    if (qr_overlay_ != nullptr) {
        lv_obj_set_style_opa(qr_overlay_, opa, 0);
    }
    // The QR card is a screen of its own: status and subtitle text would sit
    // on the code and its caption. On the caption band the text fades in only
    // once the mascot has made room, and out before the mascot grows back.
    uint8_t text_opa = opa;
    if (qr_visible_.load()) {
        text_opa = 0;
    } else if (caption_layout_) {
        text_opa = static_cast<uint8_t>((static_cast<uint32_t>(opa) * caption_mix_) / 255);
    }
    text_opa_applied_ = text_opa;
    lv_obj_t* layers[] = {status_bar_, bottom_bar_};
    for (lv_obj_t* layer : layers) {
        if (layer != nullptr) {
            lv_obj_set_style_opa(layer, text_opa, 0);
        }
    }
}

void MemoriaMascotDisplay::StyleQrCard() {
    if (qr_overlay_ == nullptr || qr_code_ == nullptr) {
        return;
    }
    const uint32_t ink = scene_ != nullptr ? scene_->theme().ink : 0x22344D;
    // Let the scene show through; the QR sits on a white card in the middle.
    lv_obj_set_style_bg_opa(qr_overlay_, LV_OPA_TRANSP, 0);
    lv_obj_remove_flag(qr_overlay_, LV_OBJ_FLAG_SCROLLABLE);
    if (qr_card_ == nullptr) {
        qr_card_ = lv_obj_create(qr_overlay_);
        lv_obj_remove_flag(qr_card_, LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_move_background(qr_card_);
    }
    lv_obj_set_size(qr_card_, 238, 238);
    lv_obj_set_style_bg_color(qr_card_, lv_color_white(), 0);
    lv_obj_set_style_bg_opa(qr_card_, LV_OPA_COVER, 0);
    lv_obj_set_style_radius(qr_card_, 28, 0);
    lv_obj_set_style_border_width(qr_card_, 1, 0);
    lv_obj_set_style_border_color(qr_card_, Color(ink), 0);
    lv_obj_set_style_border_opa(qr_card_, LV_OPA_10, 0);
    lv_obj_set_style_pad_all(qr_card_, 0, 0);
    lv_obj_align(qr_card_, LV_ALIGN_CENTER, 0, -6);
    lv_obj_align(qr_code_, LV_ALIGN_CENTER, 0, -6);
    if (qr_title_ == nullptr) {
        qr_title_ = lv_label_create(qr_overlay_);
    }
    std::string title = std::string("你好，我是") + CompanionDisplayName(companion_id_);
    lv_label_set_text(qr_title_, title.c_str());
    lv_obj_set_style_text_color(qr_title_, Color(ink), 0);
    lv_obj_align(qr_title_, LV_ALIGN_TOP_MID, 0, 30);
    if (qr_caption_ != nullptr) {
        lv_obj_set_style_text_color(qr_caption_, Color(ink), 0);
        lv_obj_set_style_text_opa(qr_caption_, LV_OPA_80, 0);
        lv_obj_align(qr_caption_, LV_ALIGN_BOTTOM_MID, 0, -34);
    }
}

void MemoriaMascotDisplay::SetTheme(Theme* theme) {
    LcdDisplay::SetTheme(theme);
    if (!Lock(1000)) {
        return;
    }
    ApplyChrome();
    Unlock();
}

bool MemoriaMascotDisplay::ShowQrCode(const std::string& payload, const char* caption) {
    if (qr_card_ != nullptr || qr_title_ != nullptr) {
        // The base deletes the old overlay together with our children.
        qr_card_ = nullptr;
        qr_title_ = nullptr;
    }
    if (!LcdDisplay::ShowQrCode(payload, caption)) {
        return false;
    }
    qr_visible_.store(true);
    if (Lock(1000)) {
        StyleQrCard();
        ApplyChromeOpacity(chrome_opa_);
        Unlock();
    }
    return true;
}

void MemoriaMascotDisplay::ClearQrCode() {
    LcdDisplay::ClearQrCode();
    qr_card_ = nullptr;
    qr_title_ = nullptr;
    qr_visible_.store(false);
    if (Lock(1000)) {
        ApplyChromeOpacity(chrome_opa_);
        Unlock();
    }
}

bool MemoriaMascotDisplay::WantsCaption(uint32_t now_ms) {
    if (qr_visible_.load() || scene_->intro_active(now_ms)) {
        return false;
    }
    // Getting online: the network notices, the hotspot hint and activation
    // text need a place of their own. Conversation states keep the full-size
    // companion and show no status text.
    switch (Application::GetInstance().GetDeviceState()) {
        case kDeviceStateStarting:
        case kDeviceStateWifiConfiguring:
        case kDeviceStateActivating:
            return true;
        default:
            return false;
    }
}

void MemoriaMascotDisplay::SetEmotion(const char* emotion) {
    if (emotion == nullptr || emotion[0] == '\0') {
        return;
    }
    if (scene_ == nullptr) {
        LcdDisplay::SetEmotion(emotion);
        return;
    }
    // Upstream alerts pass icon names; the error ones make the companion dizzy.
    if (Equals(emotion, "cancel") || Equals(emotion, "cloud_off") || Equals(emotion, "warning")) {
        error_until_ms_.store(NowMs() + kErrorHoldMs);
        ESP_LOGI(TAG, "emotion=%s -> error", emotion);
        return;
    }
    memoria::SceneMood mood;
    if (!memoria::SceneMoodFromName(emotion, &mood)) {
        return;  // gear, link, download, ...: the phase already tells the story
    }
    ESP_LOGI(TAG, "emotion=%s", emotion);
    if (xSemaphoreTake(input_mutex_, portMAX_DELAY) == pdTRUE) {
        pending_mood_ = mood;
        mood_pending_ = true;
        xSemaphoreGive(input_mutex_);
    }
}

void MemoriaMascotDisplay::SetStatus(const char* status) {
    if (status == nullptr) {
        return;
    }
    // Conversation states are shown by the companion and the ring, not text.
    if (Equals(status, Lang::Strings::STANDBY) || Equals(status, Lang::Strings::LISTENING) ||
        Equals(status, Lang::Strings::SPEAKING) || Equals(status, Lang::Strings::INITIALIZING)) {
        LvglDisplay::SetStatus("");
        return;
    }
    if (Equals(status, Lang::Strings::ERROR)) {
        error_until_ms_.store(NowMs() + kErrorHoldMs);
    }
    if (Equals(status, Lang::Strings::LOADING_PROTOCOL)) {
        LvglDisplay::SetStatus("正在连接");
        return;
    }
    if (Equals(status, Lang::Strings::CONNECTING)) {
        // A conversation (re)opening: the comet ring already says so, and a
        // line at the top would sit on the full-size companion's head.
        LvglDisplay::SetStatus("");
        return;
    }
    LvglDisplay::SetStatus(status);
}

void MemoriaMascotDisplay::ShowNotification(const char* notification, int duration_ms) {
    if (notification == nullptr) {
        return;
    }
    // The boot animation carries the brand, and version numbers are for logs.
    if (Equals(notification, "Memoria") ||
        std::strncmp(notification, Lang::Strings::VERSION, std::strlen(Lang::Strings::VERSION)) == 0) {
        return;
    }
    LvglDisplay::ShowNotification(notification, duration_ms);
}

void MemoriaMascotDisplay::ShowNotification(const std::string& notification, int duration_ms) {
    ShowNotification(notification.c_str(), duration_ms);
}

void MemoriaMascotDisplay::SetChatMessage(const char* role, const char* content) {
    if (role != nullptr && content != nullptr && std::strcmp(role, "user") == 0 && content[0] != '\0') {
        user_spoke_.store(true);
    }
    // The user agent line printed at boot is a diagnostic, not a subtitle.
    if (role != nullptr && content != nullptr && std::strcmp(role, "system") == 0 &&
        SystemInfo::GetUserAgent() == content) {
        LcdDisplay::SetChatMessage(role, "");
        return;
    }
    // Upstream's one-line hotspot hint ("手机连接热点 X，浏览器访问 http://Y")
    // becomes two short lines for the caption band.
    const std::string hotspot_prefix = Lang::Strings::CONNECT_TO_HOTSPOT;
    const std::string browser_infix = Lang::Strings::ACCESS_VIA_BROWSER;
    if (role != nullptr && content != nullptr && std::strcmp(role, "system") == 0 &&
        std::strncmp(content, hotspot_prefix.c_str(), hotspot_prefix.size()) == 0) {
        const std::string hint(content);
        const std::size_t infix = hint.find(browser_infix, hotspot_prefix.size());
        if (infix != std::string::npos) {
            std::string url = hint.substr(infix + browser_infix.size());
            if (url.rfind("http://", 0) == 0) {
                url.erase(0, 7);
            }
            const std::string lines = "连接热点 " +
                                      hint.substr(hotspot_prefix.size(), infix - hotspot_prefix.size()) +
                                      "\n浏览器打开 " + url;
            LcdDisplay::SetChatMessage(role, lines.c_str());
            return;
        }
    }
    LcdDisplay::SetChatMessage(role, content);
}

void MemoriaMascotDisplay::SetCompanion(const char* companion_id) {
    if (!IsKnownCompanion(companion_id)) {
        ESP_LOGW(TAG, "unknown companion ignored");
        return;
    }
    if (xSemaphoreTake(input_mutex_, portMAX_DELAY) == pdTRUE) {
        pending_companion_ = companion_id;
        xSemaphoreGive(input_mutex_);
    }
}

void MemoriaMascotDisplay::Pat() {
    if (xSemaphoreTake(input_mutex_, portMAX_DELAY) == pdTRUE) {
        pat_pending_ = true;
        xSemaphoreGive(input_mutex_);
    }
}

void MemoriaMascotDisplay::Shake() {
    if (xSemaphoreTake(input_mutex_, portMAX_DELAY) == pdTRUE) {
        shake_pending_ = true;
        xSemaphoreGive(input_mutex_);
    }
}

memoria::ScenePhase MemoriaMascotDisplay::CurrentPhase(uint32_t now_ms) {
    using memoria::ScenePhase;
    if (qr_visible_.load()) {
        return ScenePhase::kSetup;
    }
    const DeviceState state = Application::GetInstance().GetDeviceState();
    if (state != kDeviceStateListening) {
        user_spoke_.store(false);
    }
    switch (state) {
        case kDeviceStateWifiConfiguring:
            return ScenePhase::kWifiConfig;
        case kDeviceStateIdle:
            return static_cast<int32_t>(error_until_ms_.load() - now_ms) > 0 ? ScenePhase::kError
                                                                              : ScenePhase::kIdle;
        case kDeviceStateListening:
            return user_spoke_.load() ? ScenePhase::kThinking : ScenePhase::kListening;
        case kDeviceStateSpeaking:
            return ScenePhase::kSpeaking;
        case kDeviceStateAudioTesting:
            return ScenePhase::kListening;
        case kDeviceStateFatalError:
            return ScenePhase::kError;
        default:
            return ScenePhase::kConnecting;
    }
}

void MemoriaMascotDisplay::AnimationTask(void* arg) {
    auto* self = static_cast<MemoriaMascotDisplay*>(arg);
    self->AnimationLoop();
    self->task_ = nullptr;
    vTaskDelete(nullptr);
}

void MemoriaMascotDisplay::AnimationLoop() {
    bool first_frame = true;
    TickType_t wake = xTaskGetTickCount();
    // Render cost receipts for on-device acceptance, one line a minute.
    int64_t stats_since_us = esp_timer_get_time();
    uint32_t stats_frames = 0;
    uint32_t stats_drawn = 0;
    int64_t stats_render_us = 0;
    int64_t stats_render_max_us = 0;
    uint64_t stats_pixels = 0;
    while (running_.load()) {
        // Collect inputs from other tasks.
        std::string companion;
        bool mood_pending = false;
        memoria::SceneMood mood = memoria::SceneMood::kNeutral;
        bool pat = false;
        bool shake = false;
        if (xSemaphoreTake(input_mutex_, pdMS_TO_TICKS(5)) == pdTRUE) {
            companion.swap(pending_companion_);
            mood_pending = mood_pending_;
            mood = pending_mood_;
            mood_pending_ = false;
            pat = pat_pending_;
            shake = shake_pending_;
            pat_pending_ = false;
            shake_pending_ = false;
            xSemaphoreGive(input_mutex_);
        }

        // A companion switch decodes outside the display lock (only this task
        // touches pack_), then swaps under it: companion_id_ is also read by
        // StyleQrCard from other tasks.
        std::unique_ptr<memoria::MascotPack> previous;
        std::unique_ptr<memoria::MascotPack> next;
        if (!companion.empty() && companion != companion_id_ && LoadCompanion(companion, &next)) {
            Settings settings(kSettingsNamespace, true);
            settings.SetString(kCompanionKey, companion);
            ESP_LOGI(TAG, "companion switched to %s", companion.c_str());
        } else {
            next.reset();
        }

        if (Lock(100)) {
            const uint32_t frame_now = NowMs();
            if (next != nullptr) {
                previous = std::move(pack_);
                pack_ = std::move(next);
                companion_id_ = companion;
                scene_->SetPack(pack_.get(), frame_now);
                ApplyChrome();  // ink colour follows the companion
            }
            scene_->SetPhase(CurrentPhase(frame_now), frame_now);
            const bool want_caption = WantsCaption(frame_now);
            scene_->SetCaptioned(want_caption, frame_now);
            caption_mix_ = scene_->caption_mix(frame_now);
            if (want_caption && !caption_layout_) {
                caption_layout_ = true;  // text moves now, fades in with the mix
                ApplyChrome();
            } else if (!want_caption && caption_layout_ && caption_mix_ == 0) {
                caption_layout_ = false;
                ApplyChrome();
                // "已连接 <Wi-Fi>" is held for 30 s; it has done its job once
                // the device is online and must not land on the companion.
                if (notification_label_ != nullptr && status_label_ != nullptr) {
                    lv_obj_add_flag(notification_label_, LV_OBJ_FLAG_HIDDEN);
                    lv_obj_remove_flag(status_label_, LV_OBJ_FLAG_HIDDEN);
                }
            }
            if (mood_pending) {
                scene_->SetMood(mood, frame_now);
            }
            if (pat) {
                scene_->Pat(frame_now);
            }
            if (shake) {
                scene_->Shake(frame_now);
            }
            memoria::SceneRect dirty[memoria::MascotScene::kMaxDirty];
            const int64_t render_started = esp_timer_get_time();
            const int count = scene_->Render(frame_now, dirty, memoria::MascotScene::kMaxDirty);
            const int64_t render_us = esp_timer_get_time() - render_started;
            ++stats_frames;
            if (count > 0) {
                ++stats_drawn;
                stats_render_us += render_us;
                stats_render_max_us = render_us > stats_render_max_us ? render_us : stats_render_max_us;
                for (int i = 0; i < count; ++i) {
                    stats_pixels += static_cast<uint64_t>(dirty[i].x1 - dirty[i].x0) *
                                    (dirty[i].y1 - dirty[i].y0);
                }
            }
            for (int i = 0; i < count && container_ != nullptr; ++i) {
                lv_area_t area = {static_cast<int32_t>(dirty[i].x0), static_cast<int32_t>(dirty[i].y0),
                                  static_cast<int32_t>(dirty[i].x1 - 1),
                                  static_cast<int32_t>(dirty[i].y1 - 1)};
                lv_obj_invalidate_area(container_, &area);
            }
            const uint8_t opa = scene_->chrome_opa(frame_now);
            const uint8_t text_opa =
                caption_layout_ ? static_cast<uint8_t>((static_cast<uint32_t>(opa) * caption_mix_) / 255)
                                : opa;
            if (opa != chrome_opa_ || (!qr_visible_.load() && text_opa != text_opa_applied_)) {
                ApplyChromeOpacity(opa);
            }
            Unlock();
        }
        previous.reset();  // the scene no longer references the old pack
        if (next != nullptr && xSemaphoreTake(input_mutex_, portMAX_DELAY) == pdTRUE) {
            // The display was busy; try the switch again next frame rather than
            // wait for the next display_version change.
            if (pending_companion_.empty()) {
                pending_companion_ = companion;
            }
            xSemaphoreGive(input_mutex_);
        }

        if (first_frame) {
            first_frame = false;
            // Let LVGL flush the first (black) frame before the light comes on.
            vTaskDelay(pdMS_TO_TICKS(60));
            if (on_first_frame_) {
                on_first_frame_();
            }
        }

        // Doze: dim the panel while the companion sleeps.
        if (backlight_ != nullptr && scene_->sleeping() != dimmed_) {
            dimmed_ = scene_->sleeping();
            if (dimmed_) {
                backlight_->SetBrightness(backlight_->brightness() / 3 + 4);
            } else {
                backlight_->RestoreBrightness();
            }
        }

        const int64_t stats_now = esp_timer_get_time();
        if (stats_now - stats_since_us >= 60 * 1000 * 1000) {
            ESP_LOGI(TAG, "anim frames=%lu drawn=%lu render_avg=%lldus render_max=%lldus px_per_drawn=%llu",
                     static_cast<unsigned long>(stats_frames), static_cast<unsigned long>(stats_drawn),
                     static_cast<long long>(stats_drawn ? stats_render_us / stats_drawn : 0),
                     static_cast<long long>(stats_render_max_us),
                     static_cast<unsigned long long>(stats_drawn ? stats_pixels / stats_drawn : 0));
            stats_since_us = stats_now;
            stats_frames = stats_drawn = 0;
            stats_render_us = stats_render_max_us = 0;
            stats_pixels = 0;
        }
        const TickType_t interval = pdMS_TO_TICKS(scene_->FrameIntervalMs(NowMs()));
        if (xTaskGetTickCount() - wake > interval) {
            wake = xTaskGetTickCount();  // after a companion decode: no catch-up burst
        }
        vTaskDelayUntil(&wake, interval);
    }
}
