from services.tutor.turn_policy import TutorTurnPolicy


def test_two_distinct_stuck_turns_are_required_before_a_minimal_hint() -> None:
    policy = TutorTurnPolicy()

    first = policy.observe(
        session_id="session-a",
        turn_id=1,
        focus="tutor_homework",
        intent="request_hint",
    )
    replay = policy.observe(
        session_id="session-a",
        turn_id=1,
        focus="tutor_homework",
        intent="request_hint",
    )
    second = policy.observe(
        session_id="session-a",
        turn_id=2,
        focus="tutor_homework",
        intent="request_hint",
    )

    assert first == replay
    assert first.minimal_hint_allowed is False
    assert first.reason == "first_stuck_reframe"
    assert second.minimal_hint_allowed is True
    assert second.reason == "second_stuck_minimal_hint"


def test_progress_resets_stuck_count_and_stale_turn_fails_closed() -> None:
    policy = TutorTurnPolicy()
    policy.observe(
        session_id="session-a",
        turn_id=2,
        focus="tutor_english",
        intent="request_hint",
    )
    progressed = policy.observe(
        session_id="session-a",
        turn_id=3,
        focus="tutor_english",
        intent="chat",
    )
    stale = policy.observe(
        session_id="session-a",
        turn_id=1,
        focus="tutor_english",
        intent="request_hint",
    )

    assert progressed.stuck_count == 0
    assert stale.minimal_hint_allowed is False
    assert stale.reason == "stale_tutor_turn_fail_closed"
