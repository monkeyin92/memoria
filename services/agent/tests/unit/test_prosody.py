from __future__ import annotations

from services.agent.src.orchestration.prosody import (
    ProsodyController,
    ProsodyFeatures,
    SpeakingStyle,
)


def test_explicit_slow_down() -> None:
    c = ProsodyController()
    st = c.update(ProsodyFeatures(explicit_request="说慢一点"))
    assert st.style is SpeakingStyle.CALM
    assert 0.90 <= st.rate <= 1.10
    assert st.rate < 1.0
    assert c.instruction_for_cosyvoice() == "你正在进行闲聊互动，你说话的情感是neutral。"


def test_low_confidence_neutral() -> None:
    c = ProsodyController()
    st = c.update(ProsodyFeatures(rms_dbfs=-10, speech_rate_cps=7.0))
    # excited conf 0.65 -> neutral
    assert st.style is SpeakingStyle.NEUTRAL


def test_no_sensitive_labels_in_state() -> None:
    c = ProsodyController()
    c.update(ProsodyFeatures(explicit_request="慢一点"))
    dumped = str(c.state)
    assert "焦虑" not in dumped
    assert "抑郁" not in dumped
