from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_control_component_release_is_offline_commit_bound_and_fail_closed() -> None:
    overlay = (ROOT / "infra" / "Dockerfile.control-api-source-overlay").read_text(
        encoding="utf-8"
    )
    deploy = (ROOT / "scripts" / "deploy_control_component.sh").read_text(encoding="utf-8")
    verifier = (ROOT / "scripts" / "verify_control_release_artifact.py").read_text(
        encoding="utf-8"
    )

    assert "FROM ${BASE_IMAGE}" in overlay
    assert "RUN rm -rf /app/services /app/packages" in overlay
    assert "memoria/services /app/services" in overlay
    assert "memoria/packages /app/packages" in overlay
    assert "COPY --chmod" not in overlay
    assert "control-api-source-overlay" in overlay
    assert "verify_control_release_artifact" in overlay
    assert "postgresql" not in overlay.lower()

    assert "dry_run=true" in deploy
    assert "only a read-only SSH base-image provenance check" in deploy
    assert "--cutover" in deploy
    assert "scripts/verify_release_source.py" in deploy
    assert "dependency inputs changed" in deploy
    assert "changes escape the Control/archive scope" in deploy
    # Session Runtime code ships with Control, but never its schema.
    assert "services/control_api/*|services/archive/*|services/session_runtime/*)" in deploy
    assert deploy.index("services/session_runtime/*.sql)") < deploy.index(
        "services/control_api/*|services/archive/*|services/session_runtime/*)"
    )
    assert "git get-tar-commit-id" in deploy
    assert "--network=none" in deploy
    assert "verify_authoritative_postgres.sh" in deploy
    assert deploy.index("verify_authoritative_postgres.sh") < deploy.index("trap rollback ERR")
    assert "docker-compose.production.snapshot.yml" in deploy
    assert "production Compose file does not match the Control dependency base" in deploy
    assert 'previous_files=("$base_file" "$live_override")' in deploy
    assert 'validate_control_override "$live_override"' in deploy
    assert "Control component override is not image-only" in deploy
    assert "--no-deps --no-build control-api" in deploy
    assert "control component rollback=PASS" in deploy
    assert "control component rollback=FAILED" in deploy
    assert 'rollback_image="memoria-control-api:rollback-${release_tag}-pre-control"' in deploy
    assert "--services control-api" in deploy
    assert '--expected-image "control-api=$target_image"' in deploy
    assert '--override "$live_override" --override "$override"' in deploy
    # The bundled voice registries were retired with Doubao/CosyVoice v3.5, so
    # they are neither a dependency input nor an allowed release path.
    assert "infra/voices" not in deploy
    assert "schema" in verifier.lower()
    assert "_safe_environment" in verifier


def test_ci_python_filter_covers_docs_tests_and_control_release_tools() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    for path in (
        "'HANDOFF.md'",
        "'TODOLIST.md'",
        "'README.md'",
        "'docs/**'",
        "'tests/**'",
        "'infra/Dockerfile.control-api-source-overlay'",
    ):
        assert path in workflow
    assert "scripts/tests/test_verify_control_release_artifact.py" in workflow
    assert "scripts/tests/test_control_release_contract.py" in workflow
    assert "python -m scripts.verify_control_release_artifact" in workflow
    assert "bash -n scripts/deploy_agent_component.sh\n          bash -n scripts/deploy_control_component.sh" in workflow
    assert "bash -n scripts/deploy_agent_component.sh scripts/deploy_control_component.sh" not in workflow


def test_ci_control_api_image_gate_is_offline_and_blocks_python() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "control-api-image:" in workflow
    assert "needs: [changes, control-api-image]" in workflow
    assert "infra/Dockerfile.control-api" in workflow
    assert "infra/Dockerfile.control-api-source-overlay" in workflow
    assert "DOCKER_BUILDKIT=0 docker build --network none" in workflow
    assert "docker run --rm --network none" in workflow
    assert '"$RUNNER_TEMP/control-api-overlay-context"' in workflow
    assert 'cp -a services "$context/memoria/services"' in workflow
    assert 'cp -a packages "$context/memoria/packages"' in workflow
    assert "--build-arg \"BASE_IMAGE=$BASE_IMAGE\"" in workflow
    assert "--expected-commit \"$RELEASE_COMMIT\"" in workflow
    assert "--expected-tag \"$RELEASE_TAG\" --require-metadata" in workflow
