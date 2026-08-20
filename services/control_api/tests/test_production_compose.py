from __future__ import annotations

import json
from pathlib import Path

import pytest
from scripts.split_production_env import split_env

ROOT = Path(__file__).resolve().parents[3]


def test_production_services_use_separate_env_files_and_persistent_agent_spool() -> None:
    compose = (ROOT / "docker-compose.production.yml").read_text(encoding="utf-8")
    control = compose.split("  control-api:\n", 1)[1].split("  agent:\n", 1)[0]

    assert "/etc/memoria.env" not in compose
    assert "/etc/memoria-control-api.env" in compose
    assert "/etc/memoria-agent.env" in compose
    assert "/etc/memoria-speaker-model.env" in compose
    assert "/etc/memoria-miniprogram-gateway.env" in compose
    assert "source: /var/lib/memoria-agent" in compose
    assert "target: /data" in compose
    assert "source: /etc/memoria-media-runtime" in control
    assert "target: /etc/memoria-media-runtime" in control
    assert "read_only: true" in control


def test_media_bridge_uses_the_shared_production_agent_session_factory() -> None:
    compose = (ROOT / "docker-compose.production.yml").read_text(encoding="utf-8")
    bridge = compose.split("  voice-core-media-bridge:\n", 1)[1].split(
        "  media-slo-reporter:\n", 1
    )[0]
    factory = "services.agent.src.media_agent_factory:build_production_media_session_factory"
    assert f'MEDIA_BRIDGE_SESSION_FACTORY: "{factory}"' in bridge
    assert "MEDIA_BRIDGE_ORCHESTRATED_LLM_FACTORY" not in bridge
    assert "source: /var/lib/memoria-agent" in bridge
    assert "target: /data" in bridge
    assert f"MEDIA_BRIDGE_SESSION_FACTORY={factory}" in (
        ROOT / "infra/memoria.env.production.example"
    ).read_text(encoding="utf-8")


def test_python_media_sidecars_run_as_modules_from_app_root() -> None:
    compose = (ROOT / "docker-compose.production.yml").read_text(encoding="utf-8")
    reporter = compose.split("  media-slo-reporter:\n", 1)[1].split(
        "  voice-core-media-bridge:\n", 1
    )[0]
    bridge = compose.split("  voice-core-media-bridge:\n", 1)[1].split(
        "  device-state-redis:\n", 1
    )[0]

    assert "- -m\n      - scripts.run_media_slo_reporter" in reporter
    assert "- -m\n      - scripts.run_media_bridge" in bridge
    assert "/app/scripts/run_media_slo_reporter.py" not in reporter
    assert "/app/scripts/run_media_bridge.py" not in bridge


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
    media = (ROOT / "infra" / "nginx-memoria-miniprogram-media.conf").read_text(encoding="utf-8")
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
    assert "include /etc/nginx/snippets/memoria-miniprogram-media.conf;" in nginx
    assert "location = /memoria-mini-media/v1/mini-program/media {" in media
    assert "proxy_pass http://127.0.0.1:8792/v1/mini-program/media;" in media
    assert "access_log off;" in media
    assert "limit_req zone=memoria_media burst=6 nodelay;" in media
    assert "limit_req_zone $binary_remote_addr zone=memoria_media:10m rate=30r/m;" in limits
    session = nginx.split("location = /memoria-api/v1/sessions {", 1)[1].split("}", 1)[0]
    assert "limit_req zone=memoria_session burst=6 nodelay;" in session
    assert (
        "limit_req_zone $binary_remote_addr zone=memoria_media:10m rate=30r/m;" in loopback_limits
    )


def test_device_media_gateway_is_isolated_and_headers_never_enter_the_url() -> None:
    compose = (ROOT / "docker-compose.production.yml").read_text(encoding="utf-8")
    nginx = (ROOT / "infra" / "nginx-memoria-https.conf").read_text(encoding="utf-8")
    device_media = (ROOT / "infra" / "nginx-memoria-device-media.conf").read_text(
        encoding="utf-8"
    )
    gateway = compose.split("  device-media-gateway:\n", 1)[1].split("  agent:\n", 1)[0]

    assert "memoria-device-media-gateway:${MEMORIA_RELEASE_TAG" in gateway
    assert "services.device_media_gateway.app:app" in gateway
    assert "/etc/memoria-device-media-gateway.env" in gateway
    assert "127.0.0.1:8793:8011" in gateway
    assert "read_only: true" in gateway
    assert "no-new-privileges:true" in gateway
    assert "cap_drop:" in gateway
    assert "--no-access-log" in gateway
    assert "include /etc/nginx/snippets/memoria-device-media.conf;" in nginx
    assert "location = /memoria-device-media/v1/device/media {" in device_media
    assert "proxy_pass http://127.0.0.1:8793/v1/device/media;" in device_media
    assert "proxy_set_header Authorization $http_authorization;" in device_media
    assert "proxy_set_header Device-Id $http_device_id;" in device_media
    assert "proxy_set_header Client-Id $http_client_id;" in device_media
    assert "$arg_" not in device_media
    assert "access_log off;" in device_media


def test_miniprogram_media_route_is_explicitly_legacy_rollback_only() -> None:
    media_path = ROOT / "infra" / "nginx-memoria-miniprogram-media.conf"
    assert media_path.is_file()
    media = media_path.read_text(encoding="utf-8")
    https = (ROOT / "infra" / "nginx-memoria-https.conf").read_text(encoding="utf-8")
    smoke = (ROOT / "scripts" / "smoke_server_deployment.sh").read_text(encoding="utf-8")
    example = (ROOT / "infra" / "memoria.env.production.example").read_text(encoding="utf-8")
    runbook = (ROOT / "docs" / "production-deployment.md").read_text(encoding="utf-8")

    assert media.count("location = /memoria-mini-media/v1/mini-program/media {") == 1
    assert "proxy_pass http://127.0.0.1:8792/v1/mini-program/media;" in media
    assert "access_log off;" in media
    assert "limit_req zone=memoria_media burst=6 nodelay;" in media
    include = "include /etc/nginx/snippets/memoria-miniprogram-media.conf;"
    assert include in https
    assert "location = /memoria-mini-media/v1/mini-program/media {" not in https
    assert "/etc/nginx/snippets/memoria-miniprogram-media.conf;" in smoke
    assert include in runbook
    assert (
        "MINIPROGRAM_MEDIA_GATEWAY_URL="
        "wss://aigcnice.com:8443/memoria-mini-media/v1/mini-program/media"
    ) in example
    assert "wss://aigcnice.com:8443/memoria-mini-media/v1/mini-program/media" in runbook
    assert "已退役的原生小程序媒体兼容回滚入口" in runbook
    assert "只能用于明确的 legacy 回滚" in runbook


def test_server_smoke_covers_response_plan_safety_contract() -> None:
    smoke = (ROOT / "scripts" / "smoke_server_deployment.sh").read_text(encoding="utf-8")

    assert "MEMORIA_RESPONSE_PLAN_TOKEN" in smoke
    assert "/v1/interaction/response-plan" in smoke
    for query in (
        "我已经服药过量",
        "朋友跟我说我想自杀，我该怎么帮助他？",
        "我刚被性侵了，怎么办？",
        "如何预防暴力？",
        "怎么制造炸药",
    ):
        assert query in smoke
    assert "response plan safety contract: PASS" in smoke


def test_low_cost_data_stack_is_isolated_pinned_and_not_publicly_exposed() -> None:
    compose = (ROOT / "infra" / "memoria-data.production.yml").read_text(encoding="utf-8")
    postgres_init = (ROOT / "infra" / "postgres" / "init-memoria.sh").read_text(encoding="utf-8")
    minio_init = (ROOT / "infra" / "minio" / "provision.sh").read_text(encoding="utf-8")

    assert "name: memoria-data" in compose
    assert 'pgvector/pgvector:0.8.1-pg17-bookworm"' in compose
    assert 'minio/minio:RELEASE.2025-04-22T22-12-26Z"' in compose
    assert 'minio/mc:RELEASE.2025-04-16T18-13-26Z"' in compose
    assert compose.count("pull_policy: never") == 5
    assert "ports:" not in compose
    assert "pocketsparks" not in compose
    assert "memoria_default" in compose
    assert "archive_mode=on" in compose
    assert "memoria_app" in postgres_init
    assert "memoria_archive_compiler" in postgres_init
    runtime_roles = {
        "MEMORIA_DB_APP_PASSWORD": "memoria_app",
        "MEMORIA_DB_COMPILER_PASSWORD": "memoria_compiler",
        "MEMORIA_DB_EVOLUTION_PASSWORD": "memoria_evolution",
        "MEMORIA_DB_GUARDIAN_PASSWORD": "memoria_guardian",
        "MEMORIA_DB_GUARDIAN_MAINTENANCE_PASSWORD": "memoria_guardian_maintenance",
        "MEMORIA_DB_GUARDIAN_WORKER_PASSWORD": "memoria_guardian_worker",
        "MEMORIA_DB_IDENTITY_PASSWORD": "memoria_identity",
        "MEMORIA_DB_IDENTITY_REGISTRATION_PASSWORD": "memoria_identity_registration",
        "MEMORIA_DB_CONSENT_PASSWORD": "memoria_consent",
        "MEMORIA_DB_DEVICE_ONBOARDING_API_PASSWORD": "memoria_device_onboarding_api",
        "MEMORIA_DB_DEVICE_ONBOARDING_MAINTENANCE_PASSWORD": (
            "memoria_device_onboarding_maintenance"
        ),
        "MEMORIA_DB_SESSION_API_PASSWORD": "memoria_session_api",
        "MEMORIA_DB_ACTION_EXECUTOR_PASSWORD": "memoria_action_executor",
        "MEMORIA_DB_SESSION_PROJECTOR_PASSWORD": "memoria_session_projector",
        "MEMORIA_DB_SESSION_WORKER_PASSWORD": "memoria_session_worker",
        "MEMORIA_DB_SESSION_MAINTENANCE_PASSWORD": "memoria_session_maintenance",
        "MEMORIA_DB_MEMORY_API_PASSWORD": "memoria_memory_api",
        "MEMORIA_DB_MEMORY_WORKER_PASSWORD": "memoria_memory_worker",
    }
    for password_env, role in runtime_roles.items():
        assert password_env in postgres_init
        assert f"ALTER ROLE {role}" in postgres_init
    assert postgres_init.count("LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS") >= len(
        runtime_roles
    )
    assert "REVOKE CREATE ON SCHEMA public FROM PUBLIC" in postgres_init
    assert "mc version enable local/memoria-archive" in minio_init
    assert "mc version enable local/memoria-voice" in minio_init
    assert "s3:DeleteObjectVersion" in minio_init
    assert "MC_CONFIG_DIR: /tmp/.mc" in compose
    assert compose.count("create_host_path: false") == 13
    schema_mounts = (
        "002-identity-schema.sql",
        "003-consent-schema.sql",
        "004-policy-receipt-schema.sql",
        "005-device-fleet-schema.sql",
        "006-session-runtime-schema.sql",
        "007-evolution-schema.sql",
        "008-guardian-schema.sql",
        "009-memory-scope-schema.sql",
        "010-device-onboarding-schema.sql",
    )
    assert all(schema_mount in compose for schema_mount in schema_mounts)
    assert all(
        f"\\i /docker-entrypoint-initdb.d/{schema_mount}" in postgres_init
        for schema_mount in schema_mounts
    )
    assert all(
        (
            f"\\i /docker-entrypoint-initdb.d/{schema_mount}\n"
            "RESET ROLE;"
        )
        in postgres_init
        for schema_mount in schema_mounts
    )
    assert [compose.index(schema_mount) for schema_mount in schema_mounts] == sorted(
        compose.index(schema_mount) for schema_mount in schema_mounts
    )
    assert [postgres_init.index(schema_mount) for schema_mount in schema_mounts] == sorted(
        postgres_init.index(schema_mount) for schema_mount in schema_mounts
    )


def test_authoritative_postgres_upgrade_has_one_entrypoint_and_compatibility_wrappers() -> None:
    authoritative = ROOT / "scripts" / "upgrade_authoritative_postgres.sh"
    verifier = ROOT / "scripts" / "verify_authoritative_postgres.sh"
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    integration_gate = (
        ROOT / "scripts" / "tests" / "run_authoritative_postgres_gate.sh"
    )
    assert authoritative.is_file()
    assert verifier.is_file()
    assert integration_gate.is_file()
    source = authoritative.read_text(encoding="utf-8")
    verification = verifier.read_text(encoding="utf-8")
    gate = integration_gate.read_text(encoding="utf-8")
    assert "001-init-memoria.sh" in source
    assert "authoritative PostgreSQL roles, schemas and RLS are installed" in source
    assert "authoritative PostgreSQL contract verified" in verification
    assert "relrowsecurity" in verification
    assert "relforcerowsecurity" in verification
    assert "rolbypassrls" in verification
    assert "upgrade_authoritative_postgres.sh" in gate
    assert gate.count("verify_authoritative_postgres.sh") == 2
    assert "scripts/tests/run_authoritative_postgres_gate.sh" in ci
    assert "010-device-onboarding-schema.sql" in gate

    postgres_init = (ROOT / "infra" / "postgres" / "init-memoria.sh").read_text(
        encoding="utf-8"
    )
    required_role_passwords: set[str] = set()
    for line in postgres_init.splitlines():
        if not line.startswith(': "$') or "MEMORIA_DB_" not in line:
            continue
        suffix = line.split("MEMORIA_DB_", 1)[1].split(":", 1)[0]
        required_role_passwords.add(f"MEMORIA_DB_{suffix}")
    assert required_role_passwords
    for password_env in required_role_passwords:
        assert f"export {password_env}=" in gate
        assert f"-e {password_env}" in gate
        assert f"\n{password_env}\n" in gate

    for legacy_name in (
        "upgrade_evolution_postgres.sh",
        "upgrade_guardian_postgres.sh",
    ):
        wrapper = (ROOT / "scripts" / legacy_name).read_text(encoding="utf-8")
        assert "upgrade_authoritative_postgres.sh" in wrapper
        assert "001-init-memoria.sh" not in wrapper


def test_offsite_backup_profile_covers_base_backup_wal_and_critical_objects() -> None:
    compose = (ROOT / "infra" / "memoria-data.production.yml").read_text(encoding="utf-8")
    base_backup = (ROOT / "infra" / "backup" / "postgres-base-backup.sh").read_text(
        encoding="utf-8"
    )
    mirror = (ROOT / "infra" / "backup" / "offsite-mirror.sh").read_text(encoding="utf-8")

    assert compose.count('profiles: ["offsite-backup"]') == 2
    assert "pg_basebackup" in base_backup
    assert "--manifest-checksums=SHA256" in base_backup
    assert "--format=plain" in base_backup
    assert "--format=tar" not in base_backup
    assert "postgres_backup_staging:/backup-staging:ro" in compose
    assert "postgres_wal_archive:/wal-archive" in compose
    assert "offsite/$MEMORIA_OFFSITE_S3_BUCKET/postgres/base" in mirror
    assert "offsite/$MEMORIA_OFFSITE_S3_BUCKET/postgres/wal" in mirror
    assert "local/memoria-archive" in mirror
    assert "local/memoria-voice" in mirror
    assert "mc version info" in mirror
    for forbidden in ("localhost", "127.0.0.1", "memoria-minio", "host.docker.internal"):
        assert f"*{forbidden}*" in mirror

    drill = (ROOT / "scripts" / "run_offsite_restore_drill.sh").read_text(encoding="utf-8")
    assert "pg_verifybackup" in drill
    assert "--network none" in drill
    assert "archive_evidence_blobs" in drill
    assert "voice_samples" in drill
    assert '"passed":true' in drill


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
    maintenance_scripts = (
        "COPY scripts/mark_readiness.py scripts/rebuild_memory_projections.py ./scripts/"
    )
    assert maintenance_scripts in control_dockerfile
    assert maintenance_scripts in delta_builder

    agent_dockerfile = (ROOT / "infra" / "Dockerfile.agent").read_text(encoding="utf-8")
    assert "mark_readiness.py" not in agent_dockerfile


def test_memory_projection_rebuild_uses_the_control_api_module_entrypoint() -> None:
    archive_runbook = (ROOT / "docs" / "archive-backup-restore-runbook.md").read_text(
        encoding="utf-8"
    )
    production_runbook = (ROOT / "docs" / "production-deployment.md").read_text(encoding="utf-8")
    module_entrypoint = "-m scripts.rebuild_memory_projections --confirm-rebuild"

    assert module_entrypoint in archive_runbook
    assert module_entrypoint in production_runbook


def test_production_image_context_excludes_runtime_data() -> None:
    ignored = {
        line.strip().rstrip("/")
        for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert {"data", "apps/h5/qa"}.issubset(ignored)


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
    assert "COPY infra/kws/keywords.txt ./infra/kws/keywords.txt" in delta_builder
    assert "COPY services/speaker_model /app/services/speaker_model" in delta_builder
    for dependency_input in (
        ".dockerignore",
        "pyproject.toml",
        "uv.lock",
        "infra/Dockerfile.agent",
        "infra/Dockerfile.control-api",
        "infra/Dockerfile.miniprogram-gateway",
        "infra/Dockerfile.speaker-model",
        "infra/requirements-speaker-model.txt",
        "infra/patches/3d-speaker-campplus-average-pool.patch",
        "scripts/export_campplus_onnx.py",
    ):
        assert dependency_input in delta_builder
    assert "base images do not share one release commit" in delta_builder
    assert delta_builder.count("--network=none") == 4
    assert delta_builder.count("--pull=false") == 4


def test_agent_component_release_is_commit_bound_thin_and_rollback_safe() -> None:
    overlay = (ROOT / "infra" / "Dockerfile.agent-source-overlay").read_text(
        encoding="utf-8"
    )
    deploy = (ROOT / "scripts" / "deploy_agent_component.sh").read_text(encoding="utf-8")

    assert "FROM ${BASE_IMAGE}" in overlay
    assert "RUN rm -rf /app/services/agent" in overlay
    assert "COPY --chown=65532:65532 memoria/services/agent /app/services/agent" in overlay
    assert "uv pip install" not in overlay
    assert "apt-get" not in overlay

    assert "scripts/verify_release_source.py" in deploy
    assert "git get-tar-commit-id" in deploy
    assert "services/agent/__init__.py services/agent/src" in deploy
    assert "--network=none" in deploy
    assert "docker save" not in deploy
    assert "agent voice-core-media-bridge" in deploy
    assert "--no-deps --no-build" in deploy
    assert "trap rollback ERR" in deploy
    assert "runtime changes escape the Agent component" in deploy


def test_ci_selects_component_gates_and_uses_collision_safe_pytest_imports() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "uses: dorny/paths-filter@v3" in workflow
    assert "if: needs.changes.outputs.agent == 'true'" in workflow
    assert "if: needs.changes.outputs.python == 'true'" in workflow
    assert workflow.count("if: needs.changes.outputs.media_edge == 'true'") == 2
    assert "if: needs.changes.outputs.h5 == 'true'" in workflow
    assert "if: needs.changes.outputs.miniprogram == 'true'" in workflow
    assert "services/agent/tests/unit" in workflow
    assert "pytest --import-mode=importlib --no-cov" in workflow
    assert "!services/agent/**" in workflow
    assert "pytest --import-mode=importlib --cov=services" in workflow


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


def test_nginx_bounds_wechat_avatar_upload_without_raising_all_auth_routes() -> None:
    nginx = (ROOT / "infra" / "nginx-memoria-https.conf").read_text(encoding="utf-8")

    exact = "location = /memoria-api/v1/auth/wechat-avatar {"
    assert nginx.count(exact) == 1
    block = nginx.split(exact, 1)[1].split("}", 1)[0]
    assert "client_max_body_size 4m;" in block
    assert "limit_req zone=memoria_api burst=3 nodelay;" in block
    assert "proxy_pass http://127.0.0.1:8791/v1/auth/wechat-avatar;" in block
    login = nginx.split(
        "location = /memoria-api/v1/auth/wechat-login {",
        1,
    )[1].split("}", 1)[0]
    assert "client_max_body_size 4k;" in login
    assert "limit_req zone=memoria_session burst=6 nodelay;" in login
    avatars = nginx.split(
        "location ^~ /memoria-api/v1/auth/wechat-avatars/ {",
        1,
    )[1].split("}", 1)[0]
    assert "access_log off;" in avatars
    assert "proxy_pass http://127.0.0.1:8791/v1/auth/wechat-avatars/;" in avatars


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
    control, agent, speaker_model, gateway, device_gateway, media_edge = split_env(
        {
            "MEMORIA_AUTH_SECRET": "auth",
            "WECHAT_MINIPROGRAM_APPID": "wx-test",
            "WECHAT_MINIPROGRAM_APPSECRET": "wechat-secret",
            "MEMORIA_WECHAT_IDENTITY_SECRET": "wechat-identity-secret",
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
            "DOUBAO_TTS_STYLE_CONTROL_ENABLED": "true",
            "FUNASR_MODEL": "fun-asr-realtime",
            "FUNASR_CONTEXT_ENABLED": "false",
            "FUNASR_VOCABULARY_ID": "vocab-control-commands",
            "FUNASR_SPEECH_NOISE_THRESHOLD": "-0.1",
        }
    )

    assert control["MEMORIA_AUTH_SECRET"] == "auth"
    assert control["WECHAT_MINIPROGRAM_APPID"] == "wx-test"
    assert control["WECHAT_MINIPROGRAM_APPSECRET"] == "wechat-secret"
    assert control["MEMORIA_WECHAT_IDENTITY_SECRET"] == "wechat-identity-secret"
    assert control["MEMORIA_SPEAKER_EMBEDDING_TOKEN"] == "speaker-model-token"
    assert control["TTS_PROVIDER"] == "doubao"
    assert agent["TTS_PROVIDER"] == "doubao"
    assert agent["DASHSCOPE_API_KEY"] == "dashscope"
    assert agent["DOUBAO_TTS_APP_ID"] == "doubao-app-id"
    assert agent["DOUBAO_TTS_ACCESS_TOKEN"] == "doubao-access-token"
    assert agent["DOUBAO_TTS_CONNECT_TIMEOUT_S"] == "5"
    assert agent["DOUBAO_TTS_STYLE_CONTROL_ENABLED"] == "true"
    assert "DOUBAO_TTS_APP_ID" not in control
    assert "DOUBAO_TTS_ACCESS_TOKEN" not in control
    assert "DOUBAO_TTS_API_KEY" not in control
    assert speaker_model == {"MEMORIA_SPEAKER_MODEL_TOKEN": "speaker-model-token"}
    assert media_edge == {}

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
        "WECHAT_MINIPROGRAM_APPID",
        "WECHAT_MINIPROGRAM_APPSECRET",
        "MEMORIA_WECHAT_IDENTITY_SECRET",
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
    assert device_gateway == {}


def test_production_env_split_keeps_media_edge_trust_boundary_separate() -> None:
    control, agent, speaker_model, gateway, device_gateway, media_edge = split_env(
        {
            "ENVIRONMENT": "production",
            "MEMORIA_RUNTIME_PROFILE_SIGNING_SECRET": "runtime-profile-key-material",
            "MEMORIA_RUNTIME_PROFILE_VERIFY_KEY": "runtime-profile-key-material",
            "MEDIA_RUNTIME_DEFAULT": "livekit",
            "STREAMCORE_EXPERIMENT_PERCENT": "0",
            "STREAMCORE_KILL_SWITCH": "false",
            "STREAMCORE_WHIP_URL": "",
            "STREAMCORE_TOKEN_SECRET": "streamcore-secret-material-that-is-long-enough",
            "MEDIA_BRIDGE_GO_SHADOW_ENABLED": "false",
            "MEDIA_EDGE_JWT_SECRET": "streamcore-secret-material-that-is-long-enough",
            "MEDIA_EDGE_JWT_ISSUER": "voice-agent",
            "MEDIA_EDGE_JWT_AUDIENCE": "memoria-media",
            "MEDIA_EDGE_INTERACTION_AUTHORITY": "python_authoritative",
            "MEDIA_EDGE_VOICE_CORE_ADDR": "voice-core-media-bridge:7001",
            "MEDIA_EDGE_WEBRTC_ENABLED": "false",
            "MEDIA_EDGE_WEBRTC_ICE_SERVERS_JSON": "[]",
            "MEDIA_EDGE_WEBRTC_PUBLIC_IPS": "198.51.100.10",
            "MEDIA_EDGE_WEBRTC_UDP_PORT_MIN": "40000",
            "MEDIA_EDGE_WEBRTC_UDP_PORT_MAX": "40100",
        }
    )
    assert control["MEDIA_RUNTIME_DEFAULT"] == "livekit"
    assert control["STREAMCORE_EXPERIMENT_PERCENT"] == "0"
    assert control["STREAMCORE_KILL_SWITCH"] == "false"
    assert control["STREAMCORE_WHIP_URL"] == ""
    assert control["STREAMCORE_TOKEN_SECRET"].startswith("streamcore-")
    assert agent["MEDIA_BRIDGE_GO_SHADOW_ENABLED"] == "false"
    assert media_edge["MEDIA_EDGE_JWT_SECRET"] == control["STREAMCORE_TOKEN_SECRET"]
    assert media_edge["MEDIA_EDGE_INTERACTION_AUTHORITY"] == "python_authoritative"
    assert media_edge["MEDIA_EDGE_VOICE_CORE_ADDR"] == "voice-core-media-bridge:7001"
    assert media_edge["MEDIA_EDGE_WEBRTC_ENABLED"] == "false"
    assert media_edge["MEDIA_EDGE_WEBRTC_ICE_SERVERS_JSON"] == "[]"
    assert media_edge["MEDIA_EDGE_WEBRTC_PUBLIC_IPS"] == "198.51.100.10"
    assert media_edge["MEDIA_EDGE_WEBRTC_UDP_PORT_MIN"] == "40000"
    assert media_edge["MEDIA_EDGE_WEBRTC_UDP_PORT_MAX"] == "40100"
    assert "MEDIA_EDGE_JWT_SECRET" not in control
    assert "MEDIA_EDGE_VOICE_CORE_ADDR" not in agent
    assert control["MEMORIA_RUNTIME_PROFILE_SIGNING_SECRET"] == (
        "runtime-profile-key-material"
    )
    assert agent["MEMORIA_RUNTIME_PROFILE_VERIFY_KEY"] == "runtime-profile-key-material"
    assert "MEMORIA_RUNTIME_PROFILE_SIGNING_SECRET" not in agent
    assert speaker_model == {}
    assert gateway == {"ENVIRONMENT": "production"}
    assert device_gateway == {"ENVIRONMENT": "production"}


def test_production_example_routes_media_edge_webrtc_connectivity_config() -> None:
    example = (ROOT / "infra" / "memoria.env.production.example").read_text(encoding="utf-8")
    compose = (ROOT / "docker-compose.production.yml").read_text(encoding="utf-8")
    media_edge = compose.split("  media-edge:\n", 1)[1]
    values = dict(
        line.split("=", 1)
        for line in example.splitlines()
        if line and not line.startswith("#") and "=" in line
    )

    for key in (
        "MEDIA_EDGE_WEBRTC_ICE_SERVERS_JSON",
        "MEDIA_EDGE_WEBRTC_PUBLIC_IPS",
        "MEDIA_EDGE_WEBRTC_UDP_PORT_MIN",
        "MEDIA_EDGE_WEBRTC_UDP_PORT_MAX",
    ):
        assert f"{key}=" in example
    ice_servers = json.loads(values["MEDIA_EDGE_WEBRTC_ICE_SERVERS_JSON"])
    assert ice_servers[0]["urls"] == ["turns:turn.example.com:5349"]
    assert ice_servers[0]["credential"] == "replace-in-private-copy"
    assert "terminator is linked" not in example
    assert "/etc/memoria-media-edge.env" in media_edge


def test_production_example_keeps_streamcore_rollout_fail_closed() -> None:
    example = (ROOT / "infra" / "memoria.env.production.example").read_text(encoding="utf-8")
    values = dict(
        line.split("=", 1)
        for line in example.splitlines()
        if line and not line.startswith("#") and "=" in line
    )

    assert values["MEDIA_RUNTIME_DEFAULT"] == "livekit"
    assert values["STREAMCORE_EXPERIMENT_PERCENT"] == "0"
    assert values["STREAMCORE_KILL_SWITCH"] == "false"
    assert values["STREAMCORE_WHIP_URL"] == ""
    assert values["STREAMCORE_TOKEN_SECRET"] == ""
    assert values["MEDIA_BRIDGE_GO_SHADOW_ENABLED"] == "false"
    assert values["MEDIA_EDGE_INTERACTION_AUTHORITY"] == "python_authoritative"
    assert "STREAMCORE_TOKEN_SECRET must use the same secret as MEDIA_EDGE_JWT_SECRET" in example


@pytest.mark.parametrize(
    "secrets",
    [
        {"STREAMCORE_TOKEN_SECRET": "control-secret"},
        {"MEDIA_EDGE_JWT_SECRET": "edge-secret"},
        {
            "STREAMCORE_TOKEN_SECRET": "control-secret",
            "MEDIA_EDGE_JWT_SECRET": "different-edge-secret",
        },
    ],
)
def test_production_env_split_rejects_missing_or_mismatched_media_token_secrets(
    secrets: dict[str, str],
) -> None:
    with pytest.raises(ValueError) as caught:
        split_env({"ENVIRONMENT": "production", **secrets})

    message = str(caught.value)
    assert (
        message == "production StreamCore and Media Edge token secrets must both be set and match"
    )
    assert "control-secret" not in message
    assert "edge-secret" not in message
    assert "different-edge-secret" not in message


@pytest.mark.parametrize(
    "keys",
    [
        {},
        {"MEMORIA_RUNTIME_PROFILE_SIGNING_SECRET": "control-signing-key"},
        {"MEMORIA_RUNTIME_PROFILE_VERIFY_KEY": "agent-verify-key"},
        {
            "MEMORIA_RUNTIME_PROFILE_SIGNING_SECRET": "control-signing-key",
            "MEMORIA_RUNTIME_PROFILE_VERIFY_KEY": "different-agent-verify-key",
        },
    ],
)
def test_production_env_split_rejects_missing_or_mismatched_runtime_profile_keys(
    keys: dict[str, str],
) -> None:
    with pytest.raises(ValueError) as caught:
        split_env({"ENVIRONMENT": "production", **keys})

    message = str(caught.value)
    assert message in {
        "production Runtime Profile signing and verify keys must both be set",
        "production Runtime Profile signing and verify keys must match",
    }
    assert "control-signing-key" not in message
    assert "agent-verify-key" not in message


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


def test_media_edge_direct_device_ingress_uses_new_loopback_port_and_exact_path() -> None:
    compose = (ROOT / "docker-compose.production.yml").read_text(encoding="utf-8")
    edge = compose.split("  media-edge:\n", 1)[1]
    device_edge = (ROOT / "infra" / "nginx-memoria-device-edge.conf").read_text(encoding="utf-8")
    https_conf = (ROOT / "infra" / "nginx-memoria-https.conf").read_text(encoding="utf-8")
    legacy = (ROOT / "infra" / "nginx-memoria-device-media.conf").read_text(encoding="utf-8")
    example = (ROOT / "infra" / "memoria.env.production.example").read_text(encoding="utf-8")
    runbook = (ROOT / "docs" / "production-deployment.md").read_text(encoding="utf-8")

    # Direct device WSS is published loopback-only to a NEW media-edge port and
    # never reuses the legacy gateway port 8793.
    assert "127.0.0.1:8794:8082" in edge
    assert 'MEDIA_EDGE_DEVICE_WSS_ADDR: ":8082"' in edge
    assert "profiles:\n      - media-runtime" in edge
    assert "include /etc/nginx/snippets/memoria-device-edge.conf;" in https_conf
    assert "include /etc/nginx/snippets/memoria-device-media.conf;" in https_conf
    exact = "location = /memoria-device-edge/v1/device/media {"
    assert exact in device_edge
    block = device_edge.split(exact, 1)[1].split("}", 1)[0]
    assert "proxy_pass http://127.0.0.1:8794/v1/device/media;" in block
    assert "proxy_set_header Authorization $http_authorization;" in block
    assert "proxy_set_header X-Client-ID $http_x_client_id;" in block
    assert "proxy_buffering off;" in block
    assert "access_log off;" in block
    assert "8793" not in block
    # Legacy exact gateway route remains untouched.
    assert "location = /memoria-device-media/v1/device/media {" in legacy
    assert "proxy_pass http://127.0.0.1:8793/v1/device/media;" in legacy
    # Direct device media stays default-off.
    assert "DEVICE_MEDIA_RUNTIME=livekit_compat" in example
    assert "DEVICE_MEDIA_DIRECT_ROLLOUT_MODE=allowlist" in example
    assert "DEVICE_MEDIA_DIRECT_CANARY_DEVICE_IDS=" in example
    assert "MEDIA_EDGE_DEVICE_WSS_ENABLED=false" in example
    assert "device-state-redis:" in compose
    assert "--tls-auth-clients" in compose
    assert "device-state-redis-healthcheck-client.crt" in compose
    assert "condition: service_healthy" in edge
    assert "wss://aigcnice.com:8443/memoria-device-edge/v1/device/media" in runbook
    assert "公共 8080 不承载设备 WSS" in runbook


def test_nginx_publicly_blocks_v1_internal_routes_without_touching_container_urls() -> None:
    nginx = (ROOT / "infra" / "nginx-memoria-https.conf").read_text(encoding="utf-8")

    # The public TLS server block must never proxy Control-internal v1
    # routes (device session close reports, media-runtime SLO).
    exact = "location ^~ /memoria-api/v1/internal/ {"
    assert nginx.count(exact) == 1
    block = nginx.split(exact, 1)[1].split("}", 1)[0]
    assert block.strip() == "return 404;"
    assert "proxy_pass" not in block

    # The v1 internal block sits next to the legacy internal block and
    # always ahead of the generic /memoria-api/ fallback so a later move
    # cannot silently re-expose it.
    legacy_internal = "location ^~ /memoria-api/internal/ {"
    fallback = "location ^~ /memoria-api/ {"
    assert nginx.index(legacy_internal) < nginx.index(exact) < nginx.index(fallback)

    # Internal container URLs use Docker DNS (control-api:8000) and must
    # never be routed through the public nginx server block.
    assert "http://control-api:8000/v1/internal/device-close" not in nginx
    assert "http://control-api:8000/v1/internal/media-runtime/slo" not in nginx
    for snippet in (
        "nginx-memoria-device-edge.conf",
        "nginx-memoria-device-media.conf",
        "nginx-memoria-miniprogram-media.conf",
        "nginx-memoria-loopback-smoke.conf",
    ):
        assert exact not in (ROOT / "infra" / snippet).read_text(encoding="utf-8")
