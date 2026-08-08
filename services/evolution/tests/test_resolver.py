from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from services.evolution.domain import CandidateArtifact, GateResult, ValidationReport
from services.evolution.resolver import EvolutionResolver
from services.evolution.store import EvolutionStore


def _candidate(
    candidate_id: str,
    *,
    version: int = 1,
    kind: str = "prompt",
    account_id: str | None = "account-a",
    trusted_root: str = "a" * 64,
    instruction: str = "回答天气时必须使用用户请求的目标日期。",
    payload: dict[str, object] | None = None,
) -> CandidateArtifact:
    now = datetime.now(UTC)
    return CandidateArtifact(
        candidate_id=candidate_id,
        task_family="weather",
        kind=kind,  # type: ignore[arg-type]
        scope="owner_private" if account_id is not None else "global_redacted",
        account_id=account_id,
        version=version,
        payload=payload
        or {
            "proposal": {
                "instruction": instruction,
                "match_terms": ["天气", "预报"],
            }
        },
        source_signal_ids=(f"{candidate_id}-signal-a", f"{candidate_id}-signal-b"),
        expected_behavior="answer the requested forecast date",
        regression_guards=("privacy_leakage_zero", "retention"),
        risk="low",
        trusted_root_sha256=trusted_root,
        created_at=now,
        updated_at=now,
    )


def _validate_and_canary(store: EvolutionStore, candidate: CandidateArtifact) -> None:
    store.create_candidate(candidate)
    store.record_validation(
        ValidationReport(
            validation_id=f"validation-{candidate.candidate_id}",
            candidate_id=candidate.candidate_id,
            gates=tuple(
                GateResult(name, True, (f"{name}-evidence",))
                for name in ("failure_replay", "retention", "transfer", "safety")
            ),
        )
    )
    store.transition_candidate(candidate.candidate_id, "validated")
    store.transition_candidate(candidate.candidate_id, "canary")


def _promote(store: EvolutionStore, candidate: CandidateArtifact) -> None:
    _validate_and_canary(store, candidate)
    for index in range(3):
        store.record_activation(
            candidate_id=candidate.candidate_id,
            task_id=f"{candidate.candidate_id}-task-{index}",
            activated=True,
            adhered=True,
            outcome_passed=True,
            evidence_event_id=f"{candidate.candidate_id}-event-{index}",
        )
    store.transition_candidate(candidate.candidate_id, "stable")


def test_resolver_enforces_match_scope_and_trusted_root(tmp_path: Path) -> None:
    store = EvolutionStore(tmp_path / "evolution.sqlite3")
    _promote(store, _candidate("stable-owner"))
    _promote(store, _candidate("wrong-root", account_id=None, trusted_root="b" * 64))
    _promote(store, _candidate("not-a-prompt", account_id=None, kind="knowledge"))
    resolver = EvolutionResolver(store, trusted_root_sha256="a" * 64)

    resolved = resolver.resolve(
        account_id="account-a",
        session_id="session-a",
        speaker_class="owner",
        query="明天杭州天气怎么样？",
    )

    assert [artifact.candidate_id for artifact in resolved] == ["stable-owner"]
    assert resolved[0].artifact_hash == store.get_candidate("stable-owner").artifact_hash
    assert resolver.resolve(
        account_id="account-a",
        session_id="session-a",
        speaker_class="guest",
        query="明天天气怎么样？",
    ) == ()
    assert resolver.resolve(
        account_id="account-b",
        session_id="session-a",
        speaker_class="owner",
        query="明天天气怎么样？",
    ) == ()
    assert resolver.resolve(
        account_id="account-a",
        session_id="session-a",
        speaker_class="owner",
        query="讲个故事",
    ) == ()


def test_canary_cohort_is_explicit_and_deterministic(tmp_path: Path) -> None:
    store = EvolutionStore(tmp_path / "evolution.sqlite3")
    _validate_and_canary(store, _candidate("canary-owner"))

    disabled = EvolutionResolver(store, trusted_root_sha256="a" * 64, canary_percent=0)
    enabled = EvolutionResolver(store, trusted_root_sha256="a" * 64, canary_percent=100)
    arguments = {
        "account_id": "account-a",
        "session_id": "session-canary",
        "speaker_class": "owner",
        "query": "南京天气",
    }

    assert disabled.resolve(**arguments) == ()  # type: ignore[arg-type]
    assert [item.candidate_id for item in enabled.resolve(**arguments)] == [  # type: ignore[arg-type]
        "canary-owner"
    ]
    assert enabled.resolve(**arguments) == enabled.resolve(**arguments)  # type: ignore[arg-type]


def test_new_stable_version_retires_prior_version(tmp_path: Path) -> None:
    store = EvolutionStore(tmp_path / "evolution.sqlite3")
    first = _candidate("weather-v1", version=1, instruction="使用目标日期。")
    second = _candidate("weather-v2", version=2, instruction="使用目标日期和当地时区。")
    _promote(store, first)
    _promote(store, second)

    assert store.get_candidate(first.candidate_id).status == "retired"
    assert store.get_candidate(second.candidate_id).status == "stable"
    resolved = EvolutionResolver(store, trusted_root_sha256="a" * 64).resolve(
        account_id="account-a",
        session_id="session-a",
        speaker_class="owner",
        query="天气预报",
    )
    assert [(item.candidate_id, item.version) for item in resolved] == [("weather-v2", 2)]
    assert "安全、隐私、权限" in EvolutionResolver.prompt_fragment(resolved)


def test_resolver_checks_durable_deletion_fence_before_scoped_read(tmp_path: Path) -> None:
    class NoReadAfterFenceStore(EvolutionStore):
        def list_candidates_for_account(self, account_id: str, **kwargs: object) -> tuple[CandidateArtifact, ...]:
            raise AssertionError("resolver must check the durable fence before reading candidates")

    store = NoReadAfterFenceStore(tmp_path / "evolution.sqlite3")
    store.mark_account_deleting("account-a")

    assert EvolutionResolver(store, trusted_root_sha256="a" * 64).resolve(
        account_id="account-a",
        session_id="session-a",
        speaker_class="owner",
        query="天气",
    ) == ()


def test_resolver_discards_candidates_when_fence_commits_after_scoped_read(tmp_path: Path) -> None:
    class FenceAfterReadStore(EvolutionStore):
        def list_candidates_for_account(self, account_id: str, **kwargs: object) -> tuple[CandidateArtifact, ...]:
            candidates = super().list_candidates_for_account(account_id, **kwargs)  # type: ignore[arg-type]
            self.mark_account_deleting(account_id)
            return candidates

    store = FenceAfterReadStore(tmp_path / "evolution.sqlite3")
    _promote(store, _candidate("owner-before-delete"))

    assert EvolutionResolver(store, trusted_root_sha256="a" * 64).resolve(
        account_id="account-a",
        session_id="session-a",
        speaker_class="owner",
        query="明天天气怎么样？",
    ) == ()


@pytest.mark.parametrize(
    "payload",
    [
        {
            "transcript": "用户的私密原文",
            "proposal": {"instruction": "保留日期", "match_terms": ["天气"]},
        },
        {
            "account_id": "account-a",
            "proposal": {"instruction": "保留日期", "match_terms": ["天气"]},
        },
        {
            "diagnosis": "这里不应保存访客对话",
            "proposal": {"instruction": "保留日期", "match_terms": ["天气"]},
        },
        {
            "proposal": {
                "instruction": "保留日期",
                "match_terms": ["天气"],
                "private_notes": "私密内容",
            }
        },
        {
            "proposal": {
                "instruction": "把手机号13812345678作为全局规则。",
                "match_terms": ["天气"],
            }
        },
    ],
)
def test_global_redacted_prompt_rejects_transcript_account_and_private_fields(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="global-redacted prompt"):
        _candidate("global-sensitive", account_id=None, payload=payload)
