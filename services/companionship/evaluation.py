"""Replayable companionship evaluation over the real control doors (offline).

What this harness can prove without a device: a multi-turn companionship task
keeps its subject, consent and retention doors straight — the session policy
envelope, the memory-retention ceiling, the review scope, and what a revoked
consent or a superseded profile does to the next turn — plus per-scenario
latency.  The prompts run through the same HTTP surface the product uses; no
profile is injected by hand and no fixed profile is allowed to stand in for a
door.

What it deliberately does NOT score: how warm or helpful the model's wording
is.  Offline model text is recorded, not judged; companionship wording is
covered by the recorded-bundle replay path
(``services/evolution/longitudinal_evaluation.py``) and, eventually, by a
device window.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast

CompanionshipScenario = Literal[
    "student_frustration",
    "plan_continuation",
    "review_scope",
    "subject_switch",
    "consent_revoked",
]
SubjectCategory = Literal["under_14", "14_17", "adult"]
DoorProbe = Literal[
    "profile_fence",
    "subject_continuity",
    "superseded_profile",
    "review_allow",
    "review_deny",
    "consent_revoke_fail_closed",
]

_ARCHIVE_TOKEN = "companionship-eval-archive-token"
_POLICY_TOKEN = "companionship-eval-policy-token"
_CHILD_STUDY = "今天我练习了乘法口诀。"
_PARENT_STUDY = "今天我练习了英语单词。"
_CHILD_MARKER = "乘法口诀"
_PARENT_MARKER = "英语单词"
_CHILD_EMOTION = "happy"
_PARENT_EMOTION = "angry"

# One legal sequence per uniform scenario. review_scope is the only scenario
# with two sequences, because allow and deny are different doors, not a new
# scenario name. observe() walks the case's sequence and never branches on
# the scenario string.
_SCENARIO_PROBES: Mapping[CompanionshipScenario, tuple[tuple[DoorProbe, ...], ...]] = {
    "student_frustration": (("profile_fence", "subject_continuity"),),
    "plan_continuation": (("profile_fence", "subject_continuity"),),
    "review_scope": (
        ("profile_fence", "review_allow"),
        ("profile_fence", "review_deny"),
    ),
    "subject_switch": (("profile_fence", "superseded_profile"),),
    "consent_revoked": (("profile_fence", "consent_revoke_fail_closed"),),
}
_GUARDIAN_LINK_PROBES: frozenset[DoorProbe] = frozenset(
    {"review_allow", "review_deny", "consent_revoke_fail_closed"}
)


@dataclass(frozen=True, slots=True)
class CompanionshipCase:
    case_id: str
    scenario: CompanionshipScenario
    category: SubjectCategory
    expect_service_mode: str
    expect_subject_category: str
    probes: tuple[DoorProbe, ...]
    expect_capabilities_present: tuple[str, ...] = ()
    expect_capabilities_absent: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CompanionshipDataset:
    version: str
    cases: tuple[CompanionshipCase, ...]


@dataclass(frozen=True, slots=True)
class ScenarioObservation:
    case_id: str
    scenario: CompanionshipScenario
    passed: bool
    failures: tuple[str, ...]
    latency_ms: float
    policy_envelope: dict[str, Any]


@dataclass(frozen=True, slots=True)
class CompanionshipReport:
    adapter: str
    dataset_version: str
    case_count: int
    passed: int
    gate_violations: int
    unauthorized_recall: int
    latency_p50_ms: float
    latency_p95_ms: float
    observations: tuple[ScenarioObservation, ...]

    @property
    def accuracy(self) -> float:
        return self.passed / self.case_count if self.case_count else 0.0


def _parse_probes(
    case_id: str, scenario: CompanionshipScenario, raw: object
) -> tuple[DoorProbe, ...]:
    if (
        not isinstance(raw, list)
        or not raw
        or not all(isinstance(item, str) and item for item in raw)
    ):
        raise ValueError(f"companionship case {case_id} must declare probes")
    probes = tuple(raw)
    if probes not in _SCENARIO_PROBES[scenario]:
        raise ValueError(f"companionship case {case_id} probe sequence is not legal for {scenario}")
    return cast(tuple[DoorProbe, ...], probes)


def load_companionship_dataset(path: str | Path) -> CompanionshipDataset:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("cases"), list):
        raise ValueError("companionship dataset must contain a cases array")
    version = raw.get("version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError("companionship dataset needs a version")
    cases: list[CompanionshipCase] = []
    for value in raw["cases"]:
        if not isinstance(value, dict):
            raise ValueError("companionship cases must be objects")
        scenario_raw = value.get("scenario")
        if not isinstance(scenario_raw, str) or scenario_raw not in _SCENARIO_PROBES:
            raise ValueError(f"unknown companionship scenario {scenario_raw!r}")
        scenario = cast(CompanionshipScenario, scenario_raw)
        case_id = str(value["case_id"])
        cases.append(
            CompanionshipCase(
                case_id=case_id,
                scenario=scenario,
                category=value["category"],
                expect_service_mode=str(value["expect_service_mode"]),
                expect_subject_category=str(value["expect_subject_category"]),
                probes=_parse_probes(case_id, scenario, value.get("probes")),
                expect_capabilities_present=tuple(value.get("expect_capabilities_present", ())),
                expect_capabilities_absent=tuple(value.get("expect_capabilities_absent", ())),
            )
        )
    if len({case.case_id for case in cases}) != len(cases):
        raise ValueError("companionship case ids must be unique")
    return CompanionshipDataset(version=version, cases=tuple(cases))


@dataclass(frozen=True, slots=True)
class _Harness:
    client: Any
    app: Any
    owner: dict[str, Any]
    child_id: str
    device_id: str


@dataclass(frozen=True, slots=True)
class _ConfirmedTurn:
    envelope: dict[str, Any]
    session_id: str
    superseded_profile_id: str


def _needs_guardian_link(probes: tuple[DoorProbe, ...]) -> bool:
    return any(probe in _GUARDIAN_LINK_PROBES for probe in probes)


class OfflineControlAdapter:
    """Drive the offline Control API (SQLite) through its own HTTP surface."""

    name = "memoria-offline-control"

    async def observe(self, case: CompanionshipCase) -> ScenarioObservation:
        started = time.perf_counter()
        failures: list[str] = []
        envelope: dict[str, Any] = {}
        with tempfile.TemporaryDirectory(prefix="memoria-companionship-eval-") as directory:
            harness = await self._build(Path(directory), case=case)
            try:
                turn = await self._confirm_subject(harness)
                envelope = turn.envelope
                for probe in case.probes:
                    failures.extend(await _PROBE_RUNNERS[probe](harness, case, turn))
            finally:
                await harness.client.__aexit__(None, None, None)
        return ScenarioObservation(
            case_id=case.case_id,
            scenario=case.scenario,
            passed=not failures,
            failures=tuple(failures),
            latency_ms=(time.perf_counter() - started) * 1000,
            policy_envelope=envelope,
        )

    # ---- harness ---------------------------------------------------------
    async def _build(self, directory: Path, *, case: CompanionshipCase) -> _Harness:
        os.environ.update(
            {
                "MEMORIA_DB_PATH": str(directory / "memoria.sqlite3"),
                "MEMORIA_SPEAKER_DB_PATH": str(directory / "speaker.sqlite3"),
                "MEMORIA_EVOLUTION_DB_PATH": str(directory / "evolution.sqlite3"),
                "MEMORIA_ARCHIVE_OBJECT_STORE_PATH": str(directory / "archive-objects"),
                "MEMORIA_AUTH_SECRET": "companionship-eval-auth-secret-long-enough",
                "MEMORIA_ARCHIVE_INTERNAL_TOKEN": _ARCHIVE_TOKEN,
                "MEMORIA_INTERACTION_POLICY_TOKEN": _POLICY_TOKEN,
                "OFFLINE_MOCK": "true",
            }
        )
        from httpx import ASGITransport, AsyncClient

        from services.control_api.app.main import create_app

        app = create_app()
        client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        await client.__aenter__()
        # 用户名上限 32 字符：用 case_id 的短哈希保持可复现。
        suffix = hashlib.sha256(case.case_id.encode("utf-8")).hexdigest()[:12]
        owner = await self._register_adult(client, app, username=f"eval-{suffix}")
        device_id = f"eval-device-{case.case_id}"
        child_id = await self._bind_device(
            client,
            app,
            owner=owner,
            device_id=device_id,
            category=case.category,
            guardian_linked=_needs_guardian_link(case.probes),
        )
        return _Harness(
            client=client,
            app=app,
            owner=owner,
            child_id=child_id,
            device_id=device_id,
        )

    @staticmethod
    async def _register_adult(client: Any, app: Any, *, username: str) -> dict[str, Any]:
        created = await client.post(
            "/v1/auth/register",
            json={"username": username, "password": "safe-password"},
        )
        assert created.status_code == 201, created.text
        identity: dict[str, Any] = dict(created.json())
        # 审核证据走 Identity 权威：夹具成人账号带显式证据 id 注册，
        # 本地资料位只是同一个事实的投影（与产品路径一致，不伪造门）。
        await app.state.identity_service.register_person(
            person_id=identity["user_id"],
            display_name="家长",
            timezone="Asia/Shanghai",
            subject_category="adult",
            age_band="adult",
            age_evidence_status="verified",
            age_evidence_id="companionship-eval-adult-evidence",
            now=_now(),
        )
        app.state.memory_store.update_subject_profile(
            user_id=identity["user_id"],
            subject_category="adult",
            birth_year_band="adult",
            age_evidence_status="verified",
            now=_now().isoformat(),
        )
        # 监护面是微信小程序面：没有微信身份的账号不能签发监护同意。
        app.state.memory_store.bind_external_identities(
            preferred_user_id=identity["user_id"],
            identities={"wechat_openid": f"{username}-openid"},
            now=_now().isoformat(),
        )
        # 资料升级会吊销注册会话（与产品一致），用重新登录的令牌继续。
        logged_in = await client.post(
            "/v1/auth/login",
            json={"username": username, "password": "safe-password"},
        )
        assert logged_in.status_code == 200, logged_in.text
        fresh: dict[str, Any] = dict(logged_in.json())
        assert fresh["user_id"] == identity["user_id"]
        return fresh

    @staticmethod
    async def _bind_device(
        client: Any,
        app: Any,
        *,
        owner: dict[str, Any],
        device_id: str,
        category: SubjectCategory,
        guardian_linked: bool,
    ) -> str:
        """Bind a device: adult owner plus one subject of the requested category."""
        from services.control_api.app.device_binding_token import mint_device_binding_token

        headers = {"Authorization": f"Bearer {owner['access_token']}"}
        token = mint_device_binding_token(
            device_id=device_id,
            secret=app.state.settings.device_binding_token_key(),
            now=_now(),
            ttl=timedelta(minutes=5),
            nonce=f"companionship-eval-{device_id}",
        )
        # 回顾门和撤销门需要 parent_for_child，绑定时就建立 guardian_of。
        # memory_level 打开时绑定表单要求带上留存报价。这份离线应用没有
        # 绑定同意权威，报价不会写入监护账本；账本上的允许或拒绝只由探针的
        # HTTP 授予决定。
        declared_mode = "parent_for_child" if guardian_linked else "family_shared"
        body: dict[str, Any] = {
            "device_claim_token": token,
            "declared_mode": declared_mode,
            "account_owner_person_id": owner["user_id"],
            "primary_subject": {
                "person_id": "new",
                "relationship": "guardian_of" if guardian_linked else "family_member_of",
                "subject_draft": {
                    "display_name": "小朋友",
                    "age_band": "under_14" if category == "under_14" else "14_17",
                },
            },
            "persona_selection": "starlight",
            "service_preferences": (
                {"memory_level": "family_shared"}
                if guardian_linked
                else {
                    "memory_level": "family_shared",
                    "shared_persona_enabled": True,
                }
            ),
            "consent_offer_ids": (
                [
                    "offer_minor_voice_session_v1",
                    "offer_minor_memory_retention_v1",
                ]
                if guardian_linked
                else ["offer_family_space_v1"]
            ),
        }
        if category == "adult":
            body = {
                "device_claim_token": token,
                "declared_mode": "self_use",
                "account_owner_person_id": owner["user_id"],
                "primary_subject": {
                    "person_id": owner["user_id"],
                    "relationship": "self",
                },
                "persona_selection": "starlight",
                "service_preferences": {
                    "memory_level": "personal",
                    "interview_frequency": "low",
                },
                "consent_offer_ids": ["offer_self_memory_retention_v1"],
            }

        created = await client.post("/v1/device-bindings", headers=headers, json=body)
        assert created.status_code == 201, created.text
        payload = created.json()
        if category == "adult":
            return str(owner["user_id"])
        return str(payload["primary_subject_ids"][0])

    # ---- probes ----------------------------------------------------------
    @staticmethod
    async def _confirm_subject(harness: _Harness) -> _ConfirmedTurn:
        """Open a real session, confirm the subject, then read the signed profile."""
        headers = {"Authorization": f"Bearer {harness.owner['access_token']}"}
        opened = await harness.client.post("/v1/sessions", headers=headers, json={})
        assert opened.status_code == 200, opened.text
        session_id = str(opened.json()["session_id"])
        profile = await harness.client.get(
            f"/v1/devices/{harness.device_id}/runtime-profile",
            headers=headers,
            params={"session_id": session_id},
        )
        assert profile.status_code == 200, profile.text
        superseded_profile_id = str(profile.json().get("runtime_profile_id", ""))
        confirmed = await harness.client.post(
            f"/v1/sessions/{session_id}/active-subject",
            headers=headers,
            json={"person_id": harness.child_id, "confirmation_method": "app_confirm"},
        )
        assert confirmed.status_code == 200, confirmed.text
        # 会话门之后的签名 profile 就是设备看到的那份；断言它，而不是注入一份。
        return _ConfirmedTurn(
            envelope=dict(confirmed.json()),
            session_id=session_id,
            superseded_profile_id=superseded_profile_id,
        )

    @staticmethod
    async def _probe_profile_fence(
        _harness: _Harness, case: CompanionshipCase, turn: _ConfirmedTurn
    ) -> list[str]:
        return _check_profile(case, turn.envelope)

    @staticmethod
    async def _probe_subject_continuity(
        harness: _Harness, case: CompanionshipCase, turn: _ConfirmedTurn
    ) -> list[str]:
        """A second confirm keeps the same subject and the same capability fence.

        ``/session-policy`` reads the persistent session authority, which this
        offline confirm path does not populate, so the continuity door is the
        next session's signed profile.
        """

        failures: list[str] = []
        first_subject = turn.envelope.get("active_subject_id")
        if first_subject != harness.child_id:
            failures.append("确认后的主体与绑定主体不一致")
        second = await OfflineControlAdapter._confirm_subject(harness)
        failures.extend(_check_profile(case, second.envelope))
        if second.envelope.get("active_subject_id") != first_subject:
            failures.append("第二次会话的主体与第一次不一致")
        return failures

    @staticmethod
    async def _probe_superseded_profile(
        harness: _Harness, _case: CompanionshipCase, turn: _ConfirmedTurn
    ) -> list[str]:
        """切人负例：被替换的签名 profile 不得再驱动策略决策。"""
        failures: list[str] = []
        if turn.envelope.get("active_subject_id") != harness.child_id:
            failures.append("切人后的轮廓主体不是当前主体")
        fresh_id = str(turn.envelope.get("runtime_profile_id", ""))
        if not turn.superseded_profile_id or fresh_id == turn.superseded_profile_id:
            failures.append("切人后没有换新的签名 profile")
        headers = {"Authorization": f"Bearer {harness.owner['access_token']}"}
        decision = await harness.client.post(
            "/v1/policy/decisions",
            headers=headers,
            json={
                "runtime_profile_id": turn.superseded_profile_id,
                "capability": "chat",
                "data_classification": "ephemeral",
                "safety_state": "normal",
            },
        )
        if decision.status_code not in {403, 404}:
            failures.append(f"切人后旧 profile 仍被接受：{decision.status_code}")
        return failures

    @staticmethod
    async def _probe_review_allow(
        harness: _Harness, _case: CompanionshipCase, turn: _ConfirmedTurn
    ) -> list[str]:
        failures, _consent_id = await _grant_retention(harness)
        if failures:
            return failures
        failures.extend(await _seed_subject_evidence(harness, session_id=turn.session_id))
        if failures:
            return failures
        return await _assert_review(harness, allowed=True)

    @staticmethod
    async def _probe_review_deny(
        harness: _Harness, _case: CompanionshipCase, turn: _ConfirmedTurn
    ) -> list[str]:
        failures = await _seed_subject_evidence(harness, session_id=turn.session_id)
        failures.extend(await _assert_review(harness, allowed=False))
        return failures

    @staticmethod
    async def _probe_consent_revoke_fail_closed(
        harness: _Harness, _case: CompanionshipCase, turn: _ConfirmedTurn
    ) -> list[str]:
        """撤销负例：撤回之后回顾摘要和记忆队列都要关上，不能只看 DELETE。"""
        failures, consent_id = await _grant_retention(harness)
        if failures:
            return failures
        seeded = await _seed_subject_evidence(harness, session_id=turn.session_id)
        failures.extend(seeded)
        if not seeded:
            failures.extend(await _assert_review(harness, allowed=True))
        headers = {"Authorization": f"Bearer {harness.owner['access_token']}"}
        revoked = await harness.client.delete(
            f"/v1/guardian/minors/{harness.child_id}/consents/{consent_id}",
            headers={
                **headers,
                "Idempotency-Key": f"eval-revoke-{harness.child_id}",
            },
        )
        if revoked.status_code != 200:
            failures.append(f"无法撤回记忆保留同意：{revoked.status_code}")
            return failures
        body = revoked.json()
        if body.get("active") is not False or not body.get("revoked_at"):
            failures.append("撤回未留下撤销时间")
        failures.extend(await _assert_review(harness, allowed=False))
        return failures


def _check_profile(case: CompanionshipCase, profile: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    capabilities = {
        str(value) for value in profile.get("capabilities", ()) if isinstance(value, str)
    }
    if profile.get("service_mode") != case.expect_service_mode:
        failures.append(f"service_mode={profile.get('service_mode')} 与期望不符")
    if profile.get("subject_category") != case.expect_subject_category:
        failures.append(f"subject_category={profile.get('subject_category')} 与期望不符")
    for capability in case.expect_capabilities_present:
        if capability not in capabilities:
            failures.append(f"缺少能力 {capability}")
    for capability in case.expect_capabilities_absent:
        if capability in capabilities:
            failures.append(f"轮廓不得含能力 {capability}")
    if profile.get("active_subject_id") in (None, ""):
        failures.append("确认后仍无 active_subject_id")
    return failures


def _owner_headers(harness: _Harness) -> dict[str, str]:
    return {"Authorization": f"Bearer {harness.owner['access_token']}"}


async def _grant_retention(harness: _Harness) -> tuple[list[str], str]:
    granted = await harness.client.post(
        f"/v1/guardian/minors/{harness.child_id}/consents",
        headers={
            **_owner_headers(harness),
            "Idempotency-Key": f"eval-grant-{harness.child_id}",
        },
        json={
            "consent_kind": "memory_retention",
            "policy_version": "guardian-consent-v1",
        },
    )
    if granted.status_code != 201:
        return [f"无法授予记忆保留同意：{granted.status_code}"], ""
    consent_id = str(granted.json().get("consent_id", ""))
    if not consent_id:
        return ["监护同意缺少 id"], ""
    return [], consent_id


async def _seed_subject_evidence(harness: _Harness, *, session_id: str) -> list[str]:
    """审核队列默认是空的。与监护记忆测试同一套最小种子。

    孩子的练习句留在候选里；绑定拥有者自己的句子不得出现在这个孩子的队列。
    """

    from services.archive.domain import EvidenceEvent

    now = _now()
    owner_id = str(harness.owner["user_id"])
    child_id = harness.child_id
    archive = harness.app.state.life_archive

    def speech(event_id: str, subject_id: str, text: str, turn_id: int) -> EvidenceEvent:
        return EvidenceEvent(
            event_id=event_id,
            account_id=owner_id,
            subject_id=subject_id,
            session_id=session_id,
            turn_id=turn_id,
            generation_id=turn_id,
            event_type="speech.utterance_finalized",
            occurred_at=now + timedelta(minutes=turn_id),
            speaker_class="owner",
            source="companionship-eval",
            payload={
                "text": text,
                "interaction_mode": "companion",
                "prompt_kind": "open",
                "history_eligible": True,
                "owner_projection_eligible": True,
                "memory_retention": "retained",
                "tool_epoch": 0,
            },
        )

    def emotion(event_id: str, subject_id: str, label: str) -> EvidenceEvent:
        return EvidenceEvent(
            event_id=event_id,
            account_id=owner_id,
            subject_id=subject_id,
            event_type="emotion_observation",
            occurred_at=now,
            speaker_class="owner",
            source="companionship-eval",
            payload={
                "label": label,
                "history_eligible": True,
                "owner_projection_eligible": True,
            },
        )

    for event in (
        speech(f"{child_id}-child-study", child_id, _CHILD_STUDY, 1),
        speech(f"{child_id}-parent-study", owner_id, _PARENT_STUDY, 2),
        emotion(f"{child_id}-child-emotion", child_id, _CHILD_EMOTION),
        emotion(f"{child_id}-parent-emotion", owner_id, _PARENT_EMOTION),
    ):
        await archive.record(event)
    report = await harness.app.state.memory_catalog.compile_pending()
    if report.failed_events:
        return [f"记忆编译失败 {report.failed_events}"]
    return []


async def _assert_review(harness: _Harness, *, allowed: bool) -> list[str]:
    headers = _owner_headers(harness)
    summary = await harness.client.get(
        f"/v1/guardian/minors/{harness.child_id}/summary",
        headers=headers,
    )
    memories = await harness.client.get(
        f"/v1/guardian/minors/{harness.child_id}/memories",
        headers=headers,
    )
    if allowed:
        return _review_allowed(summary, memories, child_id=harness.child_id)
    return _review_denied(summary, memories)


def _review_allowed(summary: Any, memories: Any, *, child_id: str) -> list[str]:
    failures: list[str] = []
    if summary.status_code != 200:
        failures.append(f"回顾摘要未打开：{summary.status_code}")
    else:
        body = summary.json()
        emotions = body.get("emotion_distribution")
        if body.get("minor_user_id") != child_id:
            failures.append("回顾摘要的主体不是该孩子")
        if not isinstance(emotions, dict) or emotions.get(_CHILD_EMOTION) != 1:
            failures.append("回顾摘要没有该孩子自己的情绪")
        if isinstance(emotions, dict) and emotions.get(_PARENT_EMOTION):
            failures.append("回顾摘要混入了其他主体")
    if memories.status_code != 200:
        failures.append(f"记忆审核队列未打开：{memories.status_code}")
    else:
        candidates = memories.json().get("candidates")
        if not isinstance(candidates, list) or not any(
            isinstance(item, dict) and _CHILD_MARKER in str(item.get("value", ""))
            for item in candidates
        ):
            failures.append("记忆审核队列没有该孩子的候选")
        if _PARENT_MARKER in memories.text:
            failures.append("记忆审核队列混入了其他主体")
    return failures


def _review_denied(summary: Any, memories: Any) -> list[str]:
    failures: list[str] = []
    for name, response in (("回顾摘要", summary), ("记忆审核队列", memories)):
        if response.status_code != 403:
            failures.append(f"{name}未失败关闭：{response.status_code}")
            continue
        detail = response.json().get("detail")
        code = detail.get("code") if isinstance(detail, dict) else None
        if code != "guardian_consent_required":
            failures.append(f"{name}的关闭原因不是监护同意：{code}")
        if _CHILD_MARKER in response.text:
            failures.append(f"{name}在关闭时泄露了孩子的内容")
    return failures


ProbeRun = Callable[[_Harness, CompanionshipCase, _ConfirmedTurn], Awaitable[list[str]]]

_PROBE_RUNNERS: dict[DoorProbe, ProbeRun] = {
    "profile_fence": OfflineControlAdapter._probe_profile_fence,
    "subject_continuity": OfflineControlAdapter._probe_subject_continuity,
    "superseded_profile": OfflineControlAdapter._probe_superseded_profile,
    "review_allow": OfflineControlAdapter._probe_review_allow,
    "review_deny": OfflineControlAdapter._probe_review_deny,
    "consent_revoke_fail_closed": OfflineControlAdapter._probe_consent_revoke_fail_closed,
}


def _check_probe_registry() -> None:
    known = set(_PROBE_RUNNERS)
    for scenario, sequences in _SCENARIO_PROBES.items():
        for sequence in sequences:
            unknown = [probe for probe in sequence if probe not in known]
            if unknown:
                raise RuntimeError(f"{scenario} probe sequence names unknown probes {unknown}")


_check_probe_registry()


def _now() -> datetime:
    return datetime.now(UTC)


def report_json(report: CompanionshipReport) -> str:
    payload = {
        "adapter": report.adapter,
        "dataset_version": report.dataset_version,
        "case_count": report.case_count,
        "passed": report.passed,
        "accuracy": report.accuracy,
        "gate_violations": report.gate_violations,
        "unauthorized_recall": report.unauthorized_recall,
        "latency_p50_ms": report.latency_p50_ms,
        "latency_p95_ms": report.latency_p95_ms,
        "observations": [
            {
                "case_id": item.case_id,
                "scenario": item.scenario,
                "passed": item.passed,
                "failures": list(item.failures),
                "latency_ms": round(item.latency_ms, 3),
            }
            for item in report.observations
        ],
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1)))))
    return ordered[index]


async def run_companionship_evaluation(
    dataset: CompanionshipDataset,
    adapter: OfflineControlAdapter,
) -> CompanionshipReport:
    observations: list[ScenarioObservation] = []
    for case in dataset.cases:
        observations.append(await adapter.observe(case))
    latencies = [item.latency_ms for item in observations]
    return CompanionshipReport(
        adapter=adapter.name,
        dataset_version=dataset.version,
        case_count=len(observations),
        passed=sum(1 for item in observations if item.passed),
        gate_violations=sum(len(item.failures) for item in observations),
        # 未成年轮廓上出现私密记忆能力即越权读取风险（真实门签发的 profile）。
        unauthorized_recall=sum(
            1
            for item in observations
            if "memory_recall_private"
            in {str(value) for value in item.policy_envelope.get("capabilities", ())}
            and item.policy_envelope.get("subject_category") == "minor"
        ),
        latency_p50_ms=_percentile(latencies, 0.5),
        latency_p95_ms=_percentile(latencies, 0.95),
        observations=tuple(observations),
    )


__all__ = [
    "CompanionshipCase",
    "CompanionshipDataset",
    "CompanionshipReport",
    "DoorProbe",
    "OfflineControlAdapter",
    "ScenarioObservation",
    "load_companionship_dataset",
    "report_json",
    "run_companionship_evaluation",
]
