#pragma once

#include <array>
#include <cstdint>
#include <string>

#include "device_identity.h"
#include "esp_err.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

class LcdDisplay;
typedef struct protocomm protocomm_t;

namespace memoria {

// Owns the physical, nearby-device bootstrap surface.  The QR code contains
// only short-lived PoP material and a detached signature; Wi-Fi credentials
// are accepted only after Protocomm Security 1 has authenticated the session.
class MemoriaBootstrap final {
public:
    static MemoriaBootstrap& GetInstance();

    MemoriaBootstrap(const MemoriaBootstrap&) = delete;
    MemoriaBootstrap& operator=(const MemoriaBootstrap&) = delete;

    esp_err_t Start(LcdDisplay* display);
    void Stop();
    bool active() const { return active_; }
    const std::string& qr_payload() const { return qr_payload_; }

private:
    MemoriaBootstrap() = default;
    ~MemoriaBootstrap();

    esp_err_t BuildBootstrapQr();
    esp_err_t StartBle();
    esp_err_t StartOnlineProofTask();
    bool RunOnlineProof();
    static void OnlineProofTask(void* context);

    static esp_err_t HandleScan(uint32_t session_id,
                                const uint8_t* inbuf,
                                ssize_t inlen,
                                uint8_t** outbuf,
                                ssize_t* outlen,
                                void* context);
    static esp_err_t HandleConfig(uint32_t session_id,
                                  const uint8_t* inbuf,
                                  ssize_t inlen,
                                  uint8_t** outbuf,
                                  ssize_t* outlen,
                                  void* context);
    static esp_err_t HandleStatus(uint32_t session_id,
                                  const uint8_t* inbuf,
                                  ssize_t inlen,
                                  uint8_t** outbuf,
                                  ssize_t* outlen,
                                  void* context);
    static esp_err_t HandleBootstrapContext(uint32_t session_id,
                                            const uint8_t* inbuf,
                                            ssize_t inlen,
                                            uint8_t** outbuf,
                                            ssize_t* outlen,
                                            void* context);

    DeviceIdentity identity_;
    protocomm_t* protocomm_ = nullptr;
    LcdDisplay* display_ = nullptr;
    bool active_ = false;
    std::array<uint8_t, 16> nonce_bytes_{};
    std::string bootstrap_nonce_;
    std::string pop_;
    std::string ble_name_;
    std::string qr_payload_;
    std::string onboarding_session_id_;
    std::string mobile_nonce_;
    TaskHandle_t online_proof_task_ = nullptr;
};

}  // namespace memoria
