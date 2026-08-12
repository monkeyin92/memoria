from pathlib import Path

SOURCE = (
    Path(__file__).parents[1]
    / "overlay"
    / "files"
    / "main"
    / "memoria"
    / "memoria_protocol.cc"
).read_text(encoding="utf-8")
APPLICATION_PATCH = (
    Path(__file__).parents[1]
    / "overlay"
    / "patches"
    / "0004-use-memoria-activation-and-media.patch"
).read_text(encoding="utf-8")
AFE_PATCH = (
    Path(__file__).parents[1]
    / "overlay"
    / "patches"
    / "0005-tune-memoria-afe-vad.patch"
).read_text(encoding="utf-8")


def test_media_challenge_post_has_an_explicit_json_body() -> None:
    start = SOURCE.index('device_path + "/media-challenge"')
    end = SOURCE.index("&challenge_response", start)
    request = SOURCE[start:end]

    assert '"{}"' in request
    assert "SetContent(std::move(body))" in SOURCE


def test_afe_vad_is_fenced_and_sent_on_the_device_media_protocol() -> None:
    assert '"type\\\":\\\"vad."' in SOURCE
    assert '"sample_position\\\":" + std::to_string(uplink_sample_start_)' in SOURCE
    assert "speaking == vad_active_" in SOURCE
    assert "vad_active_ = speaking" in SOURCE
    assert "Schedule([this, speaking, listening]()" in APPLICATION_PATCH
    assert "const bool listening = GetDeviceState() == kDeviceStateListening" in APPLICATION_PATCH
    assert "memoria_protocol->SendVadState(speaking)" in APPLICATION_PATCH


def test_memoria_afe_uses_bounded_noise_tolerant_endpointing() -> None:
    assert "CONFIG_BOARD_TYPE_MEMORIA_ATK_DNESP32S3_V1" in AFE_PATCH
    assert "afe_config->vad_mode = VAD_MODE_2" in AFE_PATCH
    assert "afe_config->vad_min_noise_ms = 300" in AFE_PATCH
    assert "+    afe_config->ns_init" not in AFE_PATCH


def test_active_vad_is_closed_at_all_media_lifecycle_boundaries() -> None:
    close_start = SOURCE.index("void MemoriaProtocol::CloseAudioChannel")
    close_end = SOURCE.index("bool MemoriaProtocol::IsAudioChannelOpened", close_start)
    close_body = SOURCE[close_start:close_end]
    assert close_body.index("SendVadState(false)") < close_body.index("SendText(")
    assert close_body.index("SendVadState(false)") < close_body.index("if (send_goodbye)")

    stop_start = SOURCE.index("void MemoriaProtocol::SendStopListening")
    stop_end = SOURCE.index("void MemoriaProtocol::SendVadState", stop_start)
    stop_body = SOURCE[stop_start:stop_end]
    assert stop_body.index("SendVadState(false)") < stop_body.index("SendText(")

    assert "kMaxVadSpeechSamples" in SOURCE
    assert "uplink_sample_start_ - vad_started_sample_ >= kMaxVadSpeechSamples" in SOURCE
    assert "vad_started_sample_ = speaking ? uplink_sample_start_ : 0" in SOURCE
