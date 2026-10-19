#pragma once

namespace memoria {

// Final product copy, 产品部 v1 定稿 2026-10-19.
// Source: /workspace/memoria-notes/需求/激活失败提示文案-v1.md
constexpr const char* kActivationNetworkErrorTitle = "网络没连上";
constexpr const char* kActivationNetworkErrorBody =
    "请检查家里的无线网，网好了会自动连上";
// Bottom line, untranslated diagnostics. The code is (esp_err name, esp_err
// number, HTTP status, TLS/transport code); no secret or URL is ever passed.
constexpr const char* kActivationNetworkErrorDetailFormat = "%s (%d)  HTTP %d  TLS 0x%x";

}  // namespace memoria
