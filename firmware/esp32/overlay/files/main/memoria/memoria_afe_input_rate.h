#pragma once

namespace memoria {

// Rate of the PCM buffer AudioInputTask hands to AfeAudioEngine::Feed.
// ReadAudioData opens esp_ae_rate_cvt from the codec rate to 16 kHz when they
// differ. If that open fails, Feed still receives the codec buffer.

inline int AfeInputHz(int codec_hz, bool resampler_open) {
    if (codec_hz == 16000 || resampler_open) {
        return 16000;
    }
    return codec_hz;
}

}  // namespace memoria
