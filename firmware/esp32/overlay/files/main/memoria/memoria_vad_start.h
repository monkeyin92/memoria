#pragma once

namespace memoria {

enum class VadEdgeAction {
    kIgnore,
    kSuppressStart,
    kReport,
};

inline float VadStartRmsFloorFromMilli(int milli) {
    return static_cast<float>(milli) / 1000.0f;
}

inline VadEdgeAction ClassifyVadEdge(bool epoch_open, bool speaking, float near_end_rms,
                                     float floor) {
    if (speaking == epoch_open) {
        return VadEdgeAction::kIgnore;
    }
    if (speaking && near_end_rms < floor) {
        return VadEdgeAction::kSuppressStart;
    }
    return VadEdgeAction::kReport;
}

}  // namespace memoria
