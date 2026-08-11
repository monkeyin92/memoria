"""Single catalog for PostgreSQL account-scoped lifecycle tables.

Most tables carry ``account_id`` directly.  Evolution evidence is deliberately
kept in a controller-only schema and the dependent audit rows inherit account
ownership through their candidate or signal, so it is listed explicitly here
instead of being discovered from a column scan.
"""

from __future__ import annotations

POSTGRES_AUTHORITATIVE_ACCOUNT_TABLES = (
    "archive_consent_grants",
    "archive_evidence_events",
    "archive_processing_outbox",
    "archive_outbox_replay_audit",
    "archive_evidence_blobs",
    "archive_transcript_versions",
    "skill_run_steps",
    "skill_runs",
    "skill_version_evidence",
    "skill_versions",
    "skill_definitions",
    "persona_traits",
    "persona_evidence",
    "persona_observation_receipts",
    "speech_style_stats",
    "persona_learning_consents",
    "persona_versions",
    "digital_self_versions",
    "digital_self_lifecycle_audit_events",
    "self_model_cognitive_claims",
    "self_model_cognitive_claim_sources",
    "self_model_decision_cases",
    "self_model_decision_case_sources",
    "self_model_relationship_profiles",
    "self_model_relationship_profile_sources",
    "self_model_audit_events",
    "self_model_command_receipts",
    "speaker_identities",
    "speaker_profiles",
    "speaker_enrollment_samples",
    "voice_clone_consents",
    "voice_samples",
    "voice_enrollment_operations",
    "voice_profiles",
    "voice_blind_trials",
    "voice_evaluations",
    "voice_quality_measurements",
)

POSTGRES_PROJECTION_ACCOUNT_TABLES = (
    "memory_vector_documents",
    "memory_search_document_sources",
    "memory_search_documents",
    "episode_evidence",
    "timeline_entries",
    "relationships",
    "person_aliases",
    "knowledge_items",
    "life_episodes",
    "person_entities",
    "memory_claims",
    "memory_compile_receipts",
)

POSTGRES_EVOLUTION_ACCOUNT_TABLES = (
    "evolution_learning_signals",
    "evolution_candidates",
    "evolution_validations",
    "evolution_activation_events",
    "evolution_lifecycle_events",
    "evolution_sleep_signal_receipts",
)

# Control state is controller-owned but intentionally absent from account
# lifecycle deletion because it never contains owner-private account rows.
POSTGRES_EVOLUTION_CONTROLLER_TABLES = (
    *POSTGRES_EVOLUTION_ACCOUNT_TABLES,
    "evolution_control_state",
    "evolution_account_deletion_fences",
)

# These tables have RLS for the privileged evolution controller, not the
# application ``app.account_id`` contract audited by the account-scope probe.
POSTGRES_CONTROLLER_ONLY_RLS_TABLES = POSTGRES_EVOLUTION_CONTROLLER_TABLES

POSTGRES_ACCOUNT_LIFECYCLE_TABLES = (
    *POSTGRES_AUTHORITATIVE_ACCOUNT_TABLES,
    *POSTGRES_PROJECTION_ACCOUNT_TABLES,
    *POSTGRES_EVOLUTION_ACCOUNT_TABLES,
)
