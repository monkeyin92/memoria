#pragma once

namespace memoria {

// Protocol-side code learns which companion the account picked (display
// profile poll); the board's display renders it. This hook keeps the two
// decoupled: the board registers a sink once, the protocol publishes ids.
using CompanionSink = void (*)(const char* companion_id);

void SetCompanionSink(CompanionSink sink);
void PublishCompanion(const char* companion_id);

}  // namespace memoria
