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
