#ifndef MEMORIA_LOCAL_DATA_ERASE_H_
#define MEMORIA_LOCAL_DATA_ERASE_H_

namespace memoria {

// Erase child-related local data (Wi-Fi credentials, wake word, UI settings, power settings).
// Preserves memoria_identity partition (device identity must persist for server connectivity).
// Called on device unbinding and factory reset.
// Returns true if erasure succeeded, false if there were errors (but best-effort erasure attempted).
bool EraseLocalChildData();

}  // namespace memoria

#endif  // MEMORIA_LOCAL_DATA_ERASE_H_
