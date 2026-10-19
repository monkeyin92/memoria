#ifndef MEMORIA_LOCAL_DATA_ERASE_H_
#define MEMORIA_LOCAL_DATA_ERASE_H_

#include <cstdint>

namespace memoria {

// Erase child-related local data (Wi-Fi credentials, wake word, UI settings, power settings).
// Call when device transitions from bound (activation_v != 0) to released (manifest 409).
// Preserved without modification:
//   - memoria_identity partition (device identity, server connectivity requires it)
//   - activation_ctr (monotonic replay-attack prevention)
// Returns true if all erasures succeeded, false if any failed (still best-effort attempted).
bool EraseLocalChildData();

}  // namespace memoria

#endif  // MEMORIA_LOCAL_DATA_ERASE_H_
