from pathlib import Path

SOURCE = (
    Path(__file__).parents[1]
    / "overlay"
    / "files"
    / "main"
    / "memoria"
    / "memoria_protocol.cc"
).read_text(encoding="utf-8")


def test_media_challenge_post_has_an_explicit_json_body() -> None:
    start = SOURCE.index('device_path + "/media-challenge"')
    end = SOURCE.index("&challenge_response", start)
    request = SOURCE[start:end]

    assert '"{}"' in request
    assert "SetContent(std::move(body))" in SOURCE
