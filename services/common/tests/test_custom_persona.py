from services.common.custom_persona import parse_custom_persona


def test_parse_custom_persona_reads_the_miniprogram_marker() -> None:
    parsed = parse_custom_persona(
        "[memoria.custom_persona.v1]\nname: 小北\n---\n说话短一点，像朋友。"
    )

    assert parsed.active is True
    assert parsed.name == "小北"
    assert parsed.text == "说话短一点，像朋友。"


def test_parse_custom_persona_ignores_ordinary_bio() -> None:
    parsed = parse_custom_persona("喜欢做产品")

    assert parsed.active is False
    assert parsed.name == ""
    assert parsed.text == ""
