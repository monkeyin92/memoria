#ifndef MEMORIA_LOCAL_DATA_ERASE_H_
#define MEMORIA_LOCAL_DATA_ERASE_H_

#include <cstdint>

namespace memoria {

// Erase child-related local data (Wi-Fi credentials, wake word, UI settings, power settings).
// Called ONLY when device transitions from bound state (activation_v != 0) to released state.
// Preserves:
//   - memoria_identity partition (device identity required for server connectivity)
//   - activation_ctr in memoria_runtime (monotonic counter prevents replay attacks)
//   - Any non-child-related keys
// Returns true if all erasures succeeded, false if any failed (but best-effort attempted).
bool EraseLocalChildData();

}  // namespace memoria

#endif  // MEMORIA_LOCAL_DATA_ERASE_H_
