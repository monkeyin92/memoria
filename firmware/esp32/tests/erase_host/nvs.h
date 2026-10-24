#pragma once

#include <cstdint>

#include "esp_err.h"

// The only NVS calls the erase module may use. Anything else (nvs_erase_all,
// nvs_set_*) is deliberately undeclared so the module stops compiling.
typedef uint32_t nvs_handle_t;
typedef enum { NVS_READONLY, NVS_READWRITE } nvs_open_mode_t;

esp_err_t nvs_open(const char* name, nvs_open_mode_t mode, nvs_handle_t* out_handle);
esp_err_t nvs_erase_key(nvs_handle_t handle, const char* key);
esp_err_t nvs_commit(nvs_handle_t handle);
void nvs_close(nvs_handle_t handle);
