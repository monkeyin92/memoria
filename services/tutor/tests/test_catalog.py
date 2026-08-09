from services.tutor.catalog import ENGLISH_LESSONS, lesson_task, lessons_for


def test_first_release_has_fifty_reviewable_voice_scenarios() -> None:
    assert len(ENGLISH_LESSONS) == 50
    assert len({task.task_id for task in ENGLISH_LESSONS}) == 50
    assert all(task.focus == "tutor_english" for task in ENGLISH_LESSONS)
    assert all(task.skill_keys and task.opening_prompt for task in ENGLISH_LESSONS)


def test_catalog_lookup_is_server_owned_and_bounded() -> None:
    task = lesson_task("english-new-classmate")

    assert task is not None
    assert task.title == "认识新同学"
    assert lesson_task("unknown") is None
    assert set(lessons_for(difficulty="starter")) < set(ENGLISH_LESSONS)

