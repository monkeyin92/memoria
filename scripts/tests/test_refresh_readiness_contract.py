"""Guards for the scheduled readiness refresh script.

The 2026-09-22 production incident: a component cutover installed a Compose base
snapshot that declares ``MEMORIA_RELEASE_COMMIT`` with ``:?``, but the refresh
script only exported ``MEMORIA_RELEASE_TAG``.  ``docker compose config`` then
refused to render, its stdout was not JSON, the inline parser raised, and the
scheduled unit failed every 12h until the smoke evidence crossed its 24h TTL and
took production readiness to ``not_ready``.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _script() -> str:
    return (ROOT / "scripts" / "refresh_readiness.sh").read_text(encoding="utf-8")


def test_release_identity_comes_from_the_live_control_container() -> None:
    script = _script()

    assert 'MEMORIA_RELEASE_TAG="$(container_env_value "$control_container" MEMORIA_RELEASE_TAG)"' in script
    assert 'MEMORIA_RELEASE_COMMIT="$(container_env_value "$control_container" MEMORIA_RELEASE_COMMIT)"' in script
    # Compose reads the exported environment, so both values must be exported.
    assert "export MEMORIA_RELEASE_TAG=" in script
    assert "export MEMORIA_RELEASE_COMMIT" in script
    # Derive both before the first Compose render, otherwise the base snapshot
    # that requires the commit fails closed before the fix can help.
    first_render = script.index('config --format json')
    assert script.index('MEMORIA_RELEASE_COMMIT="$(container_env_value') < first_render
    assert script.index("export MEMORIA_RELEASE_COMMIT") < first_render


def test_compose_render_failure_is_reported_instead_of_masked() -> None:
    script = _script()

    # The render must not discard Compose's own explanation, and the parser must
    # fail with a readable message rather than a bare JSON traceback.
    assert '2>"$compose_stderr"' in script
    assert 'sed \'s/^/  compose: /\' "$compose_stderr" >&2' in script
    assert "compose config did not render JSON" in script
    assert "json.load(sys.stdin).get" not in script
