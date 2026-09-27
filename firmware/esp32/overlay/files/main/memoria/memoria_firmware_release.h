#pragma once

#include <array>
#include <cstdint>

// Identity of this firmware build for over-the-air updates. The application
// version in esp_app_desc stays the upstream one because the onboarding QR
// and online proof carry it; OTA ordering uses this build number instead.
//
// Bump MEMORIA_FIRMWARE_BUILD for every image that is published through
// scripts/publish_firmware_release.py: the device only installs a release
// whose build is strictly greater than its own, and the publisher refuses an
// image whose embedded marker does not match the build it signs.
#define MEMORIA_FIRMWARE_BUILD 7

namespace memoria {

constexpr uint32_t kFirmwareBuild = MEMORIA_FIRMWARE_BUILD;
constexpr const char* kFirmwareBoard = "memoria-esp-vocat";

// Ed25519 public key of the firmware release key. The private half lives only
// on the release machine (~/.config/memoria/secrets/firmware-release-ed25519.key);
// services/control_api/app/device_firmware.py holds the same bytes.
constexpr std::array<uint8_t, 32> kFirmwareReleasePublicKey = {
    0xb9, 0xfc, 0x4a, 0xd5, 0xde, 0xa7, 0x48, 0xe4,
    0xf6, 0x73, 0x41, 0xb1, 0x55, 0x41, 0x7a, 0xb6,
    0x5e, 0x06, 0xc3, 0x7c, 0x16, 0xc9, 0x5a, 0x26,
    0x96, 0x49, 0x34, 0x61, 0xae, 0x6b, 0x00, 0x9a,
};

}  // namespace memoria
