"""Install the two Control policy clients on one DuplexRuntime.

This module owns the fail-closed startup contract: the session policy supplies
the signed Runtime Profile, while deferred side-effect capabilities are checked
through the exact-fence action policy client.  Keeping the assembly here avoids
duplicating endpoint/token/default-deny decisions in the LiveKit entrypoint.
"""

from __future__ import annotations

import logging

from services.agent.src.action_policy_client import (
    ActionPolicyClient,
    ActionPolicyClientConfig,
    ActionPolicyPort,
    DefaultDenyActionPolicy,
)
from services.agent.src.config import AgentSettings
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.mode_policy_client import (
    ModePolicy,
    ModePolicyClient,
    ModePolicyClientConfig,
)
from services.agent.src.runtime_profile import VerifiedRuntimeProfile
from services.agent.src.transactional_effect_commit import (
    TransactionalToolEffectCommitClient,
    TransactionalToolEffectCommitClientConfig,
)

LOGGER = logging.getLogger(__name__)


async def install_runtime_policy_clients(
    *,
    runtime: DuplexRuntime,
    settings: AgentSettings,
    session_id: str,
    offline: bool,
) -> tuple[
    ModePolicyClient | None,
    ActionPolicyClient | None,
    TransactionalToolEffectCommitClient | None,
]:
    """Install session and action policy clients, or explicit deny defaults."""

    mode_client: ModePolicyClient | None = None
    action_client: ActionPolicyClient | None = None
    effect_commit_client: TransactionalToolEffectCommitClient | None = None
    action_port: ActionPolicyPort
    token = settings.internal_token("interaction_policy")
    if token and not offline:
        mode_client = ModePolicyClient(
            ModePolicyClientConfig(
                endpoint=settings.interaction_policy_url,
                internal_token=token,
                timeout_s=settings.interaction_policy_timeout_s,
            )
        )
        policy = await mode_client.fetch(session_id=session_id)
        runtime.set_mode_policy(policy)

        async def refresh_profile() -> VerifiedRuntimeProfile | None:
            assert mode_client is not None
            return (await mode_client.fetch(session_id=session_id)).runtime_profile

        runtime.set_runtime_profile_refresher(refresh_profile)
        if not policy.available:
            LOGGER.error(
                "interaction policy unavailable; session is fail-closed "
                "session_id=%s reason=%s",
                session_id,
                policy.unavailable_reason,
            )
        action_client = ActionPolicyClient(
            ActionPolicyClientConfig(
                endpoint=(
                    settings.interaction_policy_url.rsplit("/", 1)[0]
                    + "/action-policy"
                ),
                internal_token=token,
                timeout_s=settings.interaction_policy_timeout_s,
            )
        )
        action_port = action_client
        effect_commit_client = TransactionalToolEffectCommitClient(
            TransactionalToolEffectCommitClientConfig(
                endpoint=settings.transactional_effect_commit_url,
                reconcile_endpoint=settings.transactional_effect_reconcile_url,
                internal_token=token,
                timeout_s=settings.interaction_policy_timeout_s,
            )
        )
    else:
        runtime.set_mode_policy(
            ModePolicy.unavailable(
                "missing_interaction_policy_token" if not token else "offline_mock"
            )
        )
        LOGGER.error(
            "interaction policy unavailable; session is fail-closed session_id=%s",
            session_id,
        )
        action_port = DefaultDenyActionPolicy()

    def profile_for_fence(
        fence: GenerationFence,
    ) -> VerifiedRuntimeProfile | None:
        return runtime.orchestrator.runtime_profiles.for_fence(
            fence,
            current_fence=runtime.fence,
        )

    runtime.orchestrator.task_manager.set_action_policy_client(
        action_port,
        profile_for_fence=profile_for_fence,
        current_fence=lambda: runtime.fence,
    )
    runtime.orchestrator.task_manager.set_effect_commit_port(effect_commit_client)
    if runtime.mode_policy.allows_conversation():
        return mode_client, action_client, effect_commit_client
    if mode_client is not None:
        await mode_client.aclose()
    if action_client is not None:
        await action_client.aclose()
    if effect_commit_client is not None:
        await effect_commit_client.aclose()
    raise RuntimeError(
        "interaction policy does not authorize a companion conversation; "
        "refusing session start"
    )


__all__ = ["install_runtime_policy_clients"]
