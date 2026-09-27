#pragma once

#include <functional>
#include <string>

#include "device_identity.h"

namespace memoria {

// Over-the-air firmware updates from Control API.
//
// The server offers one signed release document per board
// (GET /v1/devices/{id}/firmware-release, device-signed like the activation
// manifest). The device installs it only when the build is newer than its own
// and the Ed25519 signature verifies against the release key compiled into
// this image; the image is streamed into the inactive OTA slot while its
// SHA-256 and size are checked, and the boot slot changes only after the
// bootloader-format image validates. A new image boots in PENDING_VERIFY and
// is confirmed only once Control API has answered it (ConfirmRunningImage);
// a reset before that rolls the bootloader back to the previous slot.
enum class FirmwareCheck {
    kNothingNew,  // up to date, nothing published, or nothing installable
    kStaged,      // new image written and selected for the next boot
    kRetryLater,  // transport failure or download paused; try again soon
};

class MemoriaFirmwareUpdate final {
public:
    // Marks a freshly installed image valid. No-op for an image that is
    // already confirmed or was flashed over USB.
    static void ConfirmRunningImage();

    // Checks for a newer signed release and, when there is one, writes it to
    // the inactive slot and selects that slot for the next boot (kStaged);
    // the caller restarts at a quiet moment. `may_continue` is polled between
    // chunks; returning false aborts the download (e.g. the user started
    // talking) and leaves the slot unused.
    static FirmwareCheck CheckAndStage(const DeviceIdentity& identity,
                                       const std::string& control_api_url,
                                       const std::function<bool()>& may_continue);
};

}  // namespace memoria
