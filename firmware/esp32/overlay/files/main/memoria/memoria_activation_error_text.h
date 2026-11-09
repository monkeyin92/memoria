#pragma once

namespace memoria {

// Final product copy, 产品部 v1 定稿 2026-10-19.
// Source: /workspace/memoria-notes/需求/激活失败提示文案-v1.md
constexpr const char* kActivationNetworkErrorTitle = "网络没连上";
constexpr const char* kActivationNetworkErrorBody =
    "请检查家里的无线网，恢复后会自动重试";
constexpr const char* kActivationNetworkErrorDetailFormat = "支持码 %s";

}  // namespace memoria
