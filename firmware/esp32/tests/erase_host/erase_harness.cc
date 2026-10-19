#include <cstdio>
#include <iostream>
#include <sstream>
#include <string>

#include "fake_nvs.h"
#include "memoria_local_data_erase.h"
#include "nvs.h"

esp_err_t nvs_open(const char* name, nvs_open_mode_t mode, nvs_handle_t* out_handle) {
    auto& nvs = fake::State();
    const std::string ns = name;
    if (nvs.broken_namespaces.count(ns) != 0) {
        fake::Trace("open_fail " + ns);
        return ESP_FAIL;
    }
    if (nvs.missing_namespaces.count(ns) != 0) {
        fake::Trace("open_missing " + ns);
        return ESP_ERR_NVS_NOT_FOUND;
    }
    if (mode == NVS_READWRITE) {
        nvs.store[ns];
    }
    const nvs_handle_t handle = nvs.next_handle++;
    nvs.open_handles[handle] = ns;
    fake::Trace("open " + ns);
    *out_handle = handle;
    return ESP_OK;
}

esp_err_t nvs_erase_key(nvs_handle_t handle, const char* key) {
    auto& nvs = fake::State();
    auto open = nvs.open_handles.find(handle);
    if (open == nvs.open_handles.end()) {
        fake::Trace("bad_erase");
        return ESP_FAIL;
    }
    const std::string& ns = open->second;
    fake::Trace("erase_key " + ns + " " + key);
    if (nvs.failing_erases.count(ns + "/" + key) != 0) {
        return ESP_FAIL;
    }
    if (nvs.store[ns].count(key) == 0) {
        return ESP_ERR_NVS_NOT_FOUND;
    }
    nvs.pending_erases[handle].push_back(key);
    return ESP_OK;
}

esp_err_t nvs_commit(nvs_handle_t handle) {
    auto& nvs = fake::State();
    auto open = nvs.open_handles.find(handle);
    if (open == nvs.open_handles.end()) {
        fake::Trace("bad_commit");
        return ESP_FAIL;
    }
    const std::string& ns = open->second;
    if (nvs.failing_commits.count(ns) != 0) {
        fake::Trace("commit_fail " + ns);
        return ESP_FAIL;
    }
    for (const auto& key : nvs.pending_erases[handle]) {
        nvs.store[ns].erase(key);
    }
    nvs.pending_erases.erase(handle);
    fake::Trace("commit " + ns);
    return ESP_OK;
}

void nvs_close(nvs_handle_t handle) {
    auto& nvs = fake::State();
    auto open = nvs.open_handles.find(handle);
    if (open == nvs.open_handles.end()) {
        fake::Trace("bad_close");
        return;
    }
    fake::Trace("close " + open->second);
    nvs.pending_erases.erase(handle);
    nvs.open_handles.erase(open);
}

static void Report(bool ok) {
    auto& nvs = fake::State();
    for (const auto& line : nvs.trace) {
        printf("t %s\n", line.c_str());
    }
    nvs.trace.clear();
    for (const auto& [ns, keys] : nvs.store) {
        for (const auto& [key, value] : keys) {
            printf("s %s %s %d\n", ns.c_str(), key.c_str(), value);
        }
    }
    printf("r %s\n", ok ? "true" : "false");
    fflush(stdout);
}

int main() {
    auto& nvs = fake::State();
    std::string line;
    while (std::getline(std::cin, line)) {
        std::istringstream in(line);
        std::string command;
        in >> command;
        if (command.empty() || command[0] == '#') {
            continue;
        }
        std::string ns, key;
        if (command == "put") {
            int value = 0;
            in >> ns >> key >> value;
            nvs.store[ns][key] = value;
        } else if (command == "missing") {
            in >> ns;
            nvs.missing_namespaces.insert(ns);
        } else if (command == "broken") {
            in >> ns;
            nvs.broken_namespaces.insert(ns);
        } else if (command == "fail_erase") {
            in >> ns >> key;
            nvs.failing_erases.insert(ns + "/" + key);
        } else if (command == "fail_commit") {
            in >> ns;
            nvs.failing_commits.insert(ns);
        } else if (command == "heal") {
            nvs.missing_namespaces.clear();
            nvs.broken_namespaces.clear();
            nvs.failing_erases.clear();
            nvs.failing_commits.clear();
        } else if (command == "erase") {
            Report(memoria::EraseLocalChildData());
        } else {
            return 2;
        }
    }
    return 0;
}
