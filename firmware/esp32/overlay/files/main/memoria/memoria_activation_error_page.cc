#include "memoria_activation_error_page.h"

#include <lvgl.h>

#include "display/display.h"
#include "memoria_activation_error_text.h"

// Only the diagnostics line uses this size. The CJK copy inherits the assets'
// common-charset text font, which is the 20 px face the QR caption already
// uses; no extra font is added by this page.
LV_FONT_DECLARE(font_noto_sans_basic_14_1);

namespace memoria {
namespace {

// The round panel is 360x360. Keeping every label inside 68% of the width and
// above the narrow bottom cap leaves both Chinese lines inside the circle.
constexpr int kLabelWidthPercent = 68;
constexpr int kTitleOffsetY = -80;
constexpr int kBodyOffsetY = -30;
constexpr int kDetailBottomOffset = -55;

lv_obj_t* page_ = nullptr;
lv_obj_t* detail_label_ = nullptr;

lv_obj_t* CreateLabel(lv_obj_t* parent,
                      const char* text,
                      const lv_font_t* font,
                      lv_color_t color,
                      lv_align_t align,
                      int offset_y) {
    lv_obj_t* label = lv_label_create(parent);
    if (label == nullptr) {
        return nullptr;
    }
    lv_label_set_text(label, text);
    lv_label_set_long_mode(label, LV_LABEL_LONG_WRAP);
    lv_obj_set_width(label, LV_HOR_RES * kLabelWidthPercent / 100);
    lv_obj_set_style_text_align(label, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_set_style_text_color(label, color, 0);
    if (font != nullptr) {
        lv_obj_set_style_text_font(label, font, 0);
    }
    lv_obj_align(label, align, 0, offset_y);
    return label;
}

}  // namespace

bool ShowActivationNetworkErrorPage(Display* display, const char* detail) {
    if (display == nullptr || detail == nullptr) {
        return false;
    }
    // The retry task owns this call. The display lock is what the existing
    // bootstrap QR path also uses to serialise LVGL with the UI task.
    DisplayLockGuard lock(display);
    if (page_ != nullptr && detail_label_ != nullptr) {
        lv_label_set_text(detail_label_, detail);
        return true;
    }
    lv_obj_t* page = lv_obj_create(lv_screen_active());
    if (page == nullptr) {
        return false;
    }
    lv_obj_set_size(page, LV_HOR_RES, LV_VER_RES);
    lv_obj_center(page);
    lv_obj_remove_flag(page, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_set_style_pad_all(page, 0, 0);
    lv_obj_set_style_border_width(page, 0, 0);
    lv_obj_set_style_radius(page, 0, 0);
    lv_obj_set_style_bg_color(page, lv_color_white(), 0);
    lv_obj_set_style_bg_opa(page, LV_OPA_COVER, 0);

    const lv_font_t* text_font =
        lv_obj_get_style_text_font(lv_screen_active(), LV_PART_MAIN);
    lv_obj_t* title = CreateLabel(page, kActivationNetworkErrorTitle, text_font,
                                  lv_color_black(), LV_ALIGN_CENTER, kTitleOffsetY);
    lv_obj_t* body = CreateLabel(page, kActivationNetworkErrorBody, text_font,
                                 lv_color_black(), LV_ALIGN_CENTER, kBodyOffsetY);
    detail_label_ = CreateLabel(page, detail, &font_noto_sans_basic_14_1,
                                lv_color_hex(0x555555), LV_ALIGN_BOTTOM_MID,
                                kDetailBottomOffset);
    if (title == nullptr || body == nullptr || detail_label_ == nullptr) {
        lv_obj_del(page);
        detail_label_ = nullptr;
        return false;
    }
    page_ = page;
    return true;
}

void HideActivationNetworkErrorPage(Display* display) {
    if (display == nullptr || page_ == nullptr) {
        return;
    }
    DisplayLockGuard lock(display);
    if (page_ != nullptr) {
        lv_obj_del(page_);
        page_ = nullptr;
        detail_label_ = nullptr;
    }
}

}  // namespace memoria
