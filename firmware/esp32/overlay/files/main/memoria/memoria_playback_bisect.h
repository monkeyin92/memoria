#pragma once

// Product images leave both paths on (Kconfig default y). A bench or keep-awake
// build can set either symbol to n so a heap-poisoned flash drops one path.
// ESP-IDF leaves a bool undefined when it is n, which is the same as 0 here.

#if __has_include("sdkconfig.h")
#include "sdkconfig.h"
#endif
#ifndef CONFIG_MEMORIA_PLAYBACK_STOP
#define CONFIG_MEMORIA_PLAYBACK_STOP 0
#endif
#ifndef CONFIG_MEMORIA_STANDBY_AFTER_PLAYBACK
#define CONFIG_MEMORIA_STANDBY_AFTER_PLAYBACK 0
#endif

namespace memoria {

constexpr bool kPlaybackStopEnabled = CONFIG_MEMORIA_PLAYBACK_STOP != 0;
constexpr bool kStandbyAfterPlaybackEnabled = CONFIG_MEMORIA_STANDBY_AFTER_PLAYBACK != 0;

}  // namespace memoria
