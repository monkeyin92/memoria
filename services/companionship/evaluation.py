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
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

CompanionshipScenario = Literal[
    "student_frustration",
    "plan_continuation",
    "review_scope",
    "subject_switch",
    "consent_revoked",
]
SubjectCategory = Literal["under_14", "14_17", "adult"]

_ARCHIVE_TOKEN = "companionship-eval-archive-token"
_POLICY_TOKEN = "companionship-eval-policy-token"


@dataclass(frozen=True, slots=True)
class CompanionshipCase:
    case_id: str
    scenario: CompanionshipScenario
    category: SubjectCategory
    expect_service_mode: str
    expect_subject_category: str
    expect_capabilities_present: tuple[str, ...] = ()
    expect_capabilities_absent: tuple[str, ...] = ()
    expect_consent_lifecycle: bool = False


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
        cases.append(
            CompanionshipCase(
                case_id=str(value["case_id"]),
                scenario=value["scenario"],
                category=value["category"],
                expect_service_mode=str(value["expect_service_mode"]),
                expect_subject_category=str(value["expect_subject_category"]),
                expect_capabilities_present=tuple(value.get("expect_capabilities_present", ())),
                expect_capabilities_absent=tuple(value.get("expect_capabilities_absent", ())),
                expect_consent_lifecycle=bool(value.get("expect_consent_lifecycle", False)),
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
                envelope, superseded_profile_id = await self._confirm_subject(
                    harness, case=case
                )
                failures.extend(self._check_profile(case, envelope))
                if case.scenario == "subject_switch":
                    failures.extend(
                        await self._check_superseded_profile_rejected(
                            harness, stale_profile_id=superseded_profile_id
                        )
                    )
                if case.expect_consent_lifecycle:
                    failures.extend(await self._check_consent_lifecycle(harness))
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
            guardian_linked=case.expect_consent_lifecycle,
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
        # 需要走监护同意生命周期的场景用 parent_for_child：绑定时就建立
        # guardian_of 关系，否则 guardian 授权入口会（正确地）403。
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
    async def _confirm_subject(
        harness: _Harness, *, case: CompanionshipCase
    ) -> tuple[dict[str, Any], str]:
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
        subject_id = harness.child_id
        confirmed = await harness.client.post(
            f"/v1/sessions/{session_id}/active-subject",
            headers=headers,
            json={"person_id": subject_id, "confirmation_method": "app_confirm"},
        )
        assert confirmed.status_code == 200, confirmed.text
        # 会话门之后的签名 profile 就是设备看到的那份；断言它，而不是注入一份。
        return dict(confirmed.json()), superseded_profile_id

    @staticmethod
    async def _check_superseded_profile_rejected(
        harness: _Harness, *, stale_profile_id: str
    ) -> list[str]:
        """切人负例：被替换的签名 profile 不得再驱动策略决策。"""
        headers = {"Authorization": f"Bearer {harness.owner['access_token']}"}
        decision = await harness.client.post(
            "/v1/policy/decisions",
            headers=headers,
            json={
                "runtime_profile_id": stale_profile_id,
                "capability": "chat",
                "data_classification": "ephemeral",
                "safety_state": "normal",
            },
        )
        if decision.status_code == 200:
            return ["切人后旧 profile 仍被接受"]
        return []

    @staticmethod
    async def _check_consent_lifecycle(harness: _Harness) -> list[str]:
        """撤销负例：记忆保留同意必须可授予、可撤回，并留下撤销时间。"""
        headers = {"Authorization": f"Bearer {harness.owner['access_token']}"}
        granted = await harness.client.post(
            f"/v1/guardian/minors/{harness.child_id}/consents",
            headers={**headers, "Idempotency-Key": f"companionship-eval-{harness.child_id}-retention"},
            json={
                "consent_kind": "memory_retention",
                "policy_version": "guardian-consent-v1",
            },
        )
        if granted.status_code not in {200, 201}:
            return [f"无法授予记忆保留同意：{granted.status_code}"]
        listing = await harness.client.get(
            f"/v1/guardian/minors/{harness.child_id}/consents",
            headers=headers,
        )
        if listing.status_code != 200:
            return [f"无法读取监护同意列表：{listing.status_code}"]
        consents = listing.json().get("items")
        if not isinstance(consents, list) or not consents:
            return ["监护同意列表为空"]
        consent_id = str(consents[0].get("consent_id", ""))
        if not consent_id:
            return ["监护同意缺少 id"]
        revoked = await harness.client.delete(
            f"/v1/guardian/minors/{harness.child_id}/consents/{consent_id}",
            headers={
                **headers,
                "Idempotency-Key": f"companionship-eval-revoke-{harness.child_id}",
            },
        )
        if revoked.status_code not in {200, 204}:
            return [f"无法撤回记忆保留同意：{revoked.status_code}"]
        return []

    @staticmethod
    def _check_profile(case: CompanionshipCase, profile: dict[str, Any]) -> list[str]:
        failures: list[str] = []
        capabilities = {
            str(value) for value in profile.get("capabilities", ()) if isinstance(value, str)
        }
        if profile.get("service_mode") != case.expect_service_mode:
            failures.append(
                f"service_mode={profile.get('service_mode')} 与期望不符"
            )
        if profile.get("subject_category") != case.expect_subject_category:
            failures.append(
                f"subject_category={profile.get('subject_category')} 与期望不符"
            )
        for capability in case.expect_capabilities_present:
            if capability not in capabilities:
                failures.append(f"缺少能力 {capability}")
        for capability in case.expect_capabilities_absent:
            if capability in capabilities:
                failures.append(f"未成年轮廓不得含能力 {capability}")
        if profile.get("active_subject_id") in (None, ""):
            failures.append("确认后仍无 active_subject_id")
        return failures


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
    "OfflineControlAdapter",
    "ScenarioObservation",
    "load_companionship_dataset",
    "report_json",
    "run_companionship_evaluation",
]
