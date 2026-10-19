#pragma once

#include <cstdint>
#include <map>
#include <set>
#include <string>
#include <vector>

namespace fake {

struct Nvs {
    std::map<std::string, std::map<std::string, int32_t>> store;
    std::set<std::string> missing_namespaces;
    std::set<std::string> broken_namespaces;
    std::set<std::string> failing_erases;   // "namespace/key"
    std::set<std::string> failing_commits;  // namespace
    std::map<uint32_t, std::string> open_handles;
    std::map<uint32_t, std::vector<std::string>> pending_erases;
    uint32_t next_handle = 0x1000;
    std::vector<std::string> trace;
};

inline Nvs& State() {
    static Nvs state;
    return state;
}

inline void Trace(const std::string& line) { State().trace.push_back(line); }

}  // namespace fake
