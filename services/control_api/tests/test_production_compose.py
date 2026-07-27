from __future__ import annotations

from pathlib import Path

import pytest
from scripts.split_production_env import split_env

ROOT = Path(__file__).resolve().parents[3]


def test_production_services_use_separate_env_files_and_persistent_agent_spool() -> None:
    compose = (ROOT / "docker-compose.production.yml").read_text(encoding="utf-8")

    assert "/etc/memoria.env" not in compose
    assert "/etc/memoria-control-api.env" in compose
    assert "/etc/memoria-agent.env" in compose
    assert "/etc/memoria-speaker-model.env" in compose
    assert "/etc/memoria-miniprogram-gateway.env" in compose
    assert "source: /var/lib/memoria-agent" in compose
    assert "target: /data" in compose


def test_production_agent_healthcheck_uses_accepted_heartbeat_checker() -> None:
    compose = (ROOT / "docker-compose.production.yml").read_text(encoding="utf-8")
    readiness = (ROOT / "services/control_api/app/routes/readiness.py").read_text(encoding="utf-8")
    control = compose.split("  control-api:\n", 1)[1].split("  agent:\n", 1)[0]
    agent = compose.split("  agent:\n", 1)[1]

    assert "http://127.0.0.1:8000/health/live" in control
    assert "AGENT_HEARTBEAT_MAX_AGE_S = 45" in readiness
    assert "control-api:\n        condition: service_healthy" in agent
    assert "kill -0 1" not in agent
    assert "services.agent.src.heartbeat" in agent
    assert "--check-health" in agent
    assert "interval: 10s" in agent
    assert "timeout: 3s" in agent
    assert "retries: 2" in agent


def test_production_control_disables_query_bearing_uvicorn_access_logs() -> None:
    compose = (ROOT / "docker-compose.production.yml").read_text(encoding="utf-8")
    control = compose.split("  control-api:\n", 1)[1].split("  agent:\n", 1)[0]
    dockerfile = (ROOT / "infra" / "Dockerfile.control-api").read_text(encoding="utf-8")

    assert "--no-access-log" in control
    assert '"--no-access-log"' in dockerfile


def test_miniprogram_gateway_is_isolated_and_only_exposes_loopback_wss_upstream() -> None:
    compose = (ROOT / "docker-compose.production.yml").read_text(encoding="utf-8")
    dockerfile = (ROOT / "infra" / "Dockerfile.miniprogram-gateway").read_text(encoding="utf-8")
    nginx = (ROOT / "infra" / "nginx-memoria-https.conf").read_text(encoding="utf-8")
    limits = (ROOT / "infra" / "nginx-memoria-limits.conf").read_text(encoding="utf-8")
    loopback_limits = (ROOT / "infra" / "nginx-memoria-loopback-smoke.conf").read_text(
        encoding="utf-8"
    )
    gateway = compose.split("  miniprogram-gateway:\n", 1)[1].split("  agent:\n", 1)[0]

    assert "memoria-miniprogram-gateway:${MEMORIA_RELEASE_TAG" in gateway
    assert "dockerfile: infra/Dockerfile.miniprogram-gateway" in gateway
    assert "/etc/memoria-miniprogram-gateway.env" in gateway
    assert "127.0.0.1:8792:8010" in gateway
    assert "read_only: true" in gateway
    assert "no-new-privileges:true" in gateway
    assert "cap_drop:" in gateway
    assert "--no-access-log" in gateway
    assert '"--no-access-log"' in dockerfile
    assert "USER 65532:65532" in dockerfile
    assert "location = /memoria-mini-media/v1/mini-program/media {" in nginx
    assert "proxy_pass http://127.0.0.1:8792/v1/mini-program/media;" in nginx
    assert "access_log off;" in nginx
    assert "limit_req zone=memoria_media burst=6 nodelay;" in nginx
    assert "limit_req_zone $binary_remote_addr zone=memoria_media:10m rate=30r/m;" in limits
    assert (
        "limit_req_zone $binary_remote_addr zone=memoria_media:10m rate=30r/m;" in loopback_limits
    )


def test_low_cost_data_stack_is_isolated_pinned_and_not_publicly_exposed() -> None:
    compose = (ROOT / "infra" / "memoria-data.production.yml").read_text(encoding="utf-8")
    postgres_init = (ROOT / "infra" / "postgres" / "init-memoria.sh").read_text(encoding="utf-8")
    minio_init = (ROOT / "infra" / "minio" / "provision.sh").read_text(encoding="utf-8")

    assert "name: memoria-data" in compose
    assert 'pgvector/pgvector:0.8.1-pg17-bookworm"' in compose
    assert 'minio/minio:RELEASE.2025-04-22T22-12-26Z"' in compose
    assert 'minio/mc:RELEASE.2025-04-16T18-13-26Z"' in compose
    assert compose.count("pull_policy: never") == 3
    assert "ports:" not in compose
    assert "pocketsparks" not in compose
    assert "memoria_default" in compose
    assert "archive_mode=on" in compose
    assert "memoria_app" in postgres_init
    assert "memoria_archive_compiler" in postgres_init
    assert "NOBYPASSRLS" in postgres_init
    assert "mc version enable local/memoria-archive" in minio_init
    assert "mc version enable local/memoria-voice" in minio_init
    assert "s3:DeleteObjectVersion" in minio_init
    assert "MC_CONFIG_DIR: /tmp/.mc" in compose
    assert compose.count("create_host_path: false") == 2


def test_production_runbook_pins_data_compose_path_and_network_bootstrap_order() -> None:
    runbook = (ROOT / "docs" / "production-deployment.md").read_text(encoding="utf-8")

    assert "DATA_COMPOSE_DIR=/opt/memoria/current/infra" in runbook
    network_create = "docker compose -f docker-compose.production.yml create --no-build"
    data_start = 'docker compose --project-directory "$DATA_COMPOSE_DIR"'
    assert network_create in runbook
    assert data_start in runbook
    assert runbook.index(network_create) < runbook.index(data_start)


def test_production_stack_contains_pinned_authenticated_campplus_model() -> None:
    compose = (ROOT / "docker-compose.production.yml").read_text(encoding="utf-8")
    dockerfile = (ROOT / "infra" / "Dockerfile.speaker-model").read_text(encoding="utf-8")
    requirements = (ROOT / "infra" / "requirements-speaker-model.txt").read_text(encoding="utf-8")

    assert "\n  speaker-model:\n" in compose
    assert "dockerfile: infra/Dockerfile.speaker-model" in compose
    assert "/etc/memoria-speaker-model.env" in compose
    assert "http://speaker-model:8001/v1/embeddings/speaker" in compose
    control_dependencies = compose.split("  control-api:\n", 1)[1].split("  agent:\n", 1)[0]
    assert "speaker-model:\n        condition: service_healthy" in control_dependencies
    assert "065629c313eaf1a01c65c640c46d77e61e9607b4" in dockerfile
    assert "v1.0.0" in dockerfile
    assert "3388cf5fd3493c9ac9c69851d8e7a8badcfb4f3dc631020c4961371646d5ada8" in dockerfile
    assert "7a39d2e5e5664be9b365d5a9cb76732566fca0539899382a1962b1ded3263ca9" in dockerfile
    assert "sha256sum -c" in dockerfile
    assert "campplus-average-pool.patch" in dockerfile
    assert "download.pytorch.org/whl/cpu" in dockerfile
    assert "torch==2.10.0+cpu" in dockerfile
    assert "torch==2.10.0 onnx" not in dockerfile
    exporter = (ROOT / "scripts" / "export_campplus_onnx.py").read_text(encoding="utf-8")
    assert "len(pool_attributes) != 52" in exporter
    assert "worst_cosine < 0.99999" in exporter
    assert "USER 65532:65532" in dockerfile
    for dependency in ("onnxruntime==", "kaldi-native-fbank=="):
        assert dependency in requirements
    assert "soxr==" not in requirements


def test_readiness_refresh_passes_required_provider_gate_into_run_container() -> None:
    script = (ROOT / "scripts" / "refresh_readiness.sh").read_text(encoding="utf-8")

    assert "-e MEMORIA_PROVIDER_SMOKE_REQUIRED=true" in script
    assert 'provider_output="$(run_required_provider_smoke 2>&1)"' in script
    assert "Doubao, InterruptSemantic" in script
    assert 'agent_env="${MEMORIA_AGENT_ENV:-/etc/memoria-agent.env}"' in script
    assert '"$agent_env"' in script
    assert "run_agent -m scripts.verify_env" in script
    assert "run_control -m scripts.mark_readiness" in script
    assert "--control-api-url http://control-api:8000" in script
    assert "--skip-ready-check" in script
    assert "wait_for_current_release_readiness" in script
    assert "http://127.0.0.1:8791/health/ready" in script
    assert 'payload.get("release_tag") == expected' in script
    assert "--mark-smokes-passed" not in script
    assert "--check-ready" not in script

    control_dockerfile = (ROOT / "infra" / "Dockerfile.control-api").read_text(encoding="utf-8")
    delta_builder = (ROOT / "scripts" / "delta_build_images.sh").read_text(encoding="utf-8")
    copy_line = "COPY scripts/mark_readiness.py ./scripts/mark_readiness.py"
    assert copy_line in control_dockerfile
    assert copy_line in delta_builder

    agent_dockerfile = (ROOT / "infra" / "Dockerfile.agent").read_text(encoding="utf-8")
    assert "mark_readiness.py" not in agent_dockerfile


def test_production_image_context_excludes_runtime_data_and_h5_qa() -> None:
    ignored = {
        line.strip().rstrip("/")
        for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert {"data", "apps/h5/qa"} <= ignored


def test_current_compose_never_builds_the_removed_web_client() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "\n  web:\n" not in compose
    assert "Dockerfile.web" not in compose


def test_runtime_images_include_voice_registries_needed_by_agent_and_legacy_previews() -> None:
    agent_dockerfile = (ROOT / "infra" / "Dockerfile.agent").read_text(encoding="utf-8")
    control_dockerfile = (ROOT / "infra" / "Dockerfile.control-api").read_text(encoding="utf-8")
    delta_builder = (ROOT / "scripts" / "delta_build_images.sh").read_text(encoding="utf-8")

    # Agent imports shared evidence policy which imports services.archive.domain.
    # Keep full and delta images aligned so a locally green release cannot omit
    # a transitive runtime module.
    assert "COPY services ./services" in agent_dockerfile
    assert delta_builder.count("COPY services ./services") == 3
    doubao_registry = "COPY infra/voices/doubao_voice_ids.json ./infra/voices/doubao_voice_ids.json"
    cosyvoice_registry = (
        "COPY infra/voices/designed_voice_ids.json ./infra/voices/designed_voice_ids.json"
    )
    assert doubao_registry in agent_dockerfile
    assert doubao_registry in delta_builder
    assert cosyvoice_registry in agent_dockerfile
    assert cosyvoice_registry in control_dockerfile
    assert delta_builder.count(cosyvoice_registry) == 2


def test_agent_image_installs_the_optional_keyword_spotter_runtime() -> None:
    agent_dockerfile = (ROOT / "infra" / "Dockerfile.agent").read_text(encoding="utf-8")

    assert "uv export --frozen --no-dev --extra kws" in agent_dockerfile


def test_nginx_bounds_raw_voice_upload_without_raising_all_api_body_limits() -> None:
    nginx = (ROOT / "infra" / "nginx-memoria-https.conf").read_text(encoding="utf-8")

    exact = "location = /memoria-api/v1/archive/session-raw-audio {"
    assert nginx.count(exact) == 1
    block = nginx.split(exact, 1)[1].split("}", 1)[0]
    assert "client_max_body_size 4m;" in block
    assert "limit_req_status 429;" in block
    assert "limit_req zone=memoria_api burst=6 nodelay;" in block
    assert "proxy_pass http://127.0.0.1:8791/v1/archive/session-raw-audio;" in block
    generic = nginx.split("location ^~ /memoria-api/ {", 1)[1].split("}", 1)[0]
    assert "client_max_body_size 4m;" not in generic


def test_nginx_protects_h5_with_csp_and_hides_signed_sample_tokens_from_access_logs() -> None:
    nginx = (ROOT / "infra" / "nginx-memoria-https.conf").read_text(encoding="utf-8")

    h5 = nginx.split("location = /memoria-h5/index.html {", 1)[1].split("}", 1)[0]
    assert "Content-Security-Policy" in h5
    assert "connect-src 'self' wss:" in h5
    assert "media-src 'self' blob:" in h5

    exact = "location ^~ /memoria-api/v1/voices/provider-samples/ {"
    assert nginx.count(exact) == 1
    sample = nginx.split(exact, 1)[1].split("}", 1)[0]
    assert "access_log off;" in sample
    assert "proxy_pass http://127.0.0.1:8791/v1/voices/provider-samples/;" in sample


def test_production_example_declares_control_only_object_read_keyrings() -> None:
    example = (ROOT / "infra" / "memoria.env.production.example").read_text(encoding="utf-8")

    assert "MEMORIA_VOICE_SAMPLE_READ_KEYS=" in example
    assert "MEMORIA_ARCHIVE_OBJECT_READ_KEYS=" in example


def test_production_env_split_never_exposes_archive_or_biometric_keys_to_agent() -> None:
    control, agent, speaker_model, gateway = split_env(
        {
            "MEMORIA_AUTH_SECRET": "auth",
            "MEMORIA_ARCHIVE_DATABASE_URL": "postgresql://db/memoria",
            "MEMORIA_ARCHIVE_COMPILER_DATABASE_URL": "postgresql://compiler@db/memoria",
            "MEMORIA_MEMORY_EMBEDDING_API_KEY": "memory-embedding-key",
            "MEMORIA_ARCHIVE_OBJECT_ENCRYPTION_KEY": "archive-key",
            "MEMORIA_ARCHIVE_OBJECT_READ_KEYS": '{"archive-v1":"archive-old-key"}',
            "MEMORIA_ARCHIVE_OBJECT_ACCESS_KEY": "archive-access",
            "MEMORIA_ARCHIVE_OBJECT_SECRET_KEY": "archive-secret",
            "MEMORIA_SPEAKER_TEMPLATE_KEY": "speaker-key",
            "MEMORIA_SPEAKER_EMBEDDING_TOKEN": "speaker-model-token",
            "MEMORIA_VOICE_SAMPLE_ENCRYPTION_KEY": "voice-key",
            "MEMORIA_VOICE_SAMPLE_READ_KEYS": '{"voice-v1":"voice-old-key"}',
            "MEMORIA_VOICE_OBJECT_ACCESS_KEY": "voice-access",
            "MEMORIA_VOICE_OBJECT_SECRET_KEY": "voice-secret",
            "MEMORIA_VOICE_SAMPLE_URL_SECRET": "voice-url",
            "MEMORIA_ARCHIVE_WRITE_TOKEN": "archive-write-token",
            "MEMORIA_AGENT_HEARTBEAT_TOKEN": "agent-heartbeat-token",
            "MEMORIA_MEMORY_READ_TOKEN": "memory-read-token",
            "MEMORIA_PERSONA_READ_TOKEN": "persona-read-token",
            "MEMORIA_VOICE_RESOLUTION_TOKEN": "voice-resolution-token",
            "MEMORIA_VOICE_CLEANUP_TOKEN": "voice-cleanup-token",
            "MEMORIA_INTERACTION_POLICY_TOKEN": "interaction-policy-token",
            "MEMORIA_PERSONA_ENABLED": "false",
            "MEMORIA_MEMORY_CONTEXT_ENABLED": "false",
            "MEMORIA_VOICE_PROFILE_ENABLED": "false",
            "DASHSCOPE_API_KEY": "dashscope",
            "TTS_PROVIDER": "doubao",
            "DOUBAO_TTS_APP_ID": "doubao-app-id",
            "DOUBAO_TTS_ACCESS_TOKEN": "doubao-access-token",
            "DOUBAO_TTS_CONNECT_TIMEOUT_S": "5",
            "FUNASR_MODEL": "fun-asr-realtime",
            "FUNASR_CONTEXT_ENABLED": "false",
            "FUNASR_VOCABULARY_ID": "vocab-control-commands",
            "FUNASR_SPEECH_NOISE_THRESHOLD": "-0.1",
        }
    )

    assert control["MEMORIA_AUTH_SECRET"] == "auth"
    assert control["MEMORIA_SPEAKER_EMBEDDING_TOKEN"] == "speaker-model-token"
    assert control["TTS_PROVIDER"] == "doubao"
    assert agent["TTS_PROVIDER"] == "doubao"
    assert agent["DASHSCOPE_API_KEY"] == "dashscope"
    assert agent["DOUBAO_TTS_APP_ID"] == "doubao-app-id"
    assert agent["DOUBAO_TTS_ACCESS_TOKEN"] == "doubao-access-token"
    assert agent["DOUBAO_TTS_CONNECT_TIMEOUT_S"] == "5"
    assert "DOUBAO_TTS_APP_ID" not in control
    assert "DOUBAO_TTS_ACCESS_TOKEN" not in control
    assert "DOUBAO_TTS_API_KEY" not in control
    assert speaker_model == {"MEMORIA_SPEAKER_MODEL_TOKEN": "speaker-model-token"}
    assert agent["MEMORIA_ARCHIVE_WRITE_TOKEN"] == "archive-write-token"
    assert agent["MEMORIA_AGENT_HEARTBEAT_TOKEN"] == "agent-heartbeat-token"
    assert control["MEMORIA_AGENT_HEARTBEAT_TOKEN"] == "agent-heartbeat-token"
    assert control["MEMORIA_MEMORY_READ_TOKEN"] == "memory-read-token"
    assert control["MEMORIA_PERSONA_READ_TOKEN"] == "persona-read-token"
    assert control["MEMORIA_VOICE_RESOLUTION_TOKEN"] == "voice-resolution-token"
    assert control["MEMORIA_VOICE_CLEANUP_TOKEN"] == "voice-cleanup-token"
    assert control["MEMORIA_INTERACTION_POLICY_TOKEN"] == "interaction-policy-token"
    assert agent["MEMORIA_INTERACTION_POLICY_TOKEN"] == "interaction-policy-token"
    for disabled_capability in (
        "MEMORIA_MEMORY_READ_TOKEN",
        "MEMORIA_PERSONA_READ_TOKEN",
        "MEMORIA_VOICE_RESOLUTION_TOKEN",
        "MEMORIA_VOICE_CLEANUP_TOKEN",
    ):
        assert disabled_capability not in agent
    assert agent["FUNASR_MODEL"] == "fun-asr-realtime"
    assert agent["FUNASR_CONTEXT_ENABLED"] == "false"
    assert agent["FUNASR_VOCABULARY_ID"] == "vocab-control-commands"
    assert agent["FUNASR_SPEECH_NOISE_THRESHOLD"] == "-0.1"
    for forbidden in (
        "MEMORIA_AUTH_SECRET",
        "MEMORIA_ARCHIVE_DATABASE_URL",
        "MEMORIA_ARCHIVE_COMPILER_DATABASE_URL",
        "MEMORIA_MEMORY_EMBEDDING_API_KEY",
        "MEMORIA_ARCHIVE_OBJECT_ENCRYPTION_KEY",
        "MEMORIA_ARCHIVE_OBJECT_READ_KEYS",
        "MEMORIA_SPEAKER_TEMPLATE_KEY",
        "MEMORIA_VOICE_SAMPLE_ENCRYPTION_KEY",
        "MEMORIA_VOICE_SAMPLE_READ_KEYS",
        "MEMORIA_VOICE_SAMPLE_URL_SECRET",
        "MEMORIA_ARCHIVE_OBJECT_SECRET_KEY",
        "MEMORIA_VOICE_OBJECT_SECRET_KEY",
    ):
        assert forbidden not in agent
    assert control["MEMORIA_ARCHIVE_OBJECT_ACCESS_KEY"] == "archive-access"
    assert control["MEMORIA_VOICE_OBJECT_ACCESS_KEY"] == "voice-access"
    assert control["MEMORIA_ARCHIVE_OBJECT_READ_KEYS"] == '{"archive-v1":"archive-old-key"}'
    assert control["MEMORIA_VOICE_SAMPLE_READ_KEYS"] == '{"voice-v1":"voice-old-key"}'
    assert gateway == {}


def test_production_env_split_rejects_unused_doubao_secret_key() -> None:
    with pytest.raises(ValueError, match="must not be deployed"):
        split_env({"DOUBAO_TTS_SECRET_KEY": "unused-secret"})


@pytest.mark.parametrize(
    "auth",
    [
        {
            "DOUBAO_TTS_API_KEY": "api-key",
            "DOUBAO_TTS_APP_ID": "app-id",
            "DOUBAO_TTS_ACCESS_TOKEN": "access-token",
        },
        {"DOUBAO_TTS_APP_ID": "app-id"},
        {"DOUBAO_TTS_ACCESS_TOKEN": "access-token"},
    ],
)
def test_production_env_split_rejects_ambiguous_or_partial_doubao_auth(
    auth: dict[str, str],
) -> None:
    with pytest.raises(ValueError, match="exactly one complete authentication mode"):
        split_env(auth)


@pytest.mark.parametrize("runtime_secret", ["DOUBAO_TTS_API_KEY", "DOUBAO_TTS_ACCESS_TOKEN"])
def test_production_env_split_rejects_shared_clone_and_runtime_tts_secret(
    runtime_secret: str,
) -> None:
    values = {
        "MEMORIA_DOUBAO_VOICE_API_KEY": "shared-key",
        runtime_secret: "shared-key",
    }
    if runtime_secret == "DOUBAO_TTS_ACCESS_TOKEN":
        values["DOUBAO_TTS_APP_ID"] = "doubao-app-id"
    with pytest.raises(ValueError, match="must be independent"):
        split_env(values)


def test_production_env_split_rejects_the_legacy_all_access_token() -> None:
    with pytest.raises(ValueError, match="legacy all-access internal token"):
        split_env(
            {
                "ENVIRONMENT": "production",
                "MEMORIA_ARCHIVE_INTERNAL_TOKEN": "legacy-overbroad-token",
            }
        )


def test_production_env_split_rejects_a_reused_spool_encryption_key() -> None:
    shared = "same-fernet-material"
    with pytest.raises(ValueError, match="encryption keys must be independent"):
        split_env(
            {
                "ENVIRONMENT": "production",
                "MEMORIA_ARCHIVE_SPOOL_KEY": shared,
                "MEMORIA_ARCHIVE_OBJECT_ENCRYPTION_KEY": shared,
            }
        )
