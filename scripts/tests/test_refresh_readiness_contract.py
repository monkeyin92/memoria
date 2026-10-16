"""Guards for the scheduled readiness refresh script.

The 2026-09-22 production incident: a component cutover installed a Compose base
snapshot that declares ``MEMORIA_RELEASE_COMMIT`` with ``:?``, but the refresh
script only exported ``MEMORIA_RELEASE_TAG``.  ``docker compose config`` then
refused to render, its stdout was not JSON, the inline parser raised, and the
scheduled unit failed every 12h until the smoke evidence crossed its 24h TTL and
took production readiness to ``not_ready``.
"""

import shlex
import subprocess
from pathlib import Path

import pytest
from scripts import provider_smoke_test

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


def test_smokes_run_in_the_media_bridge_after_the_livekit_retirement() -> None:
    script = _script()

    # The LiveKit worker and its room-service smoke are gone; the bridge runs the
    # Agent image, so provider/env smokes run from its Compose service.
    assert "livekit_smoke_test" not in script
    assert "memoria-agent-1" not in script
    assert 'require_live_service_image "$bridge_container" voice-core-media-bridge' in script
    assert "--entrypoint /app/.venv/bin/python voice-core-media-bridge" in script
    # `config` renders profiled services only when their profile is active.
    assert script.count('--project-name "$project_name" --profile media-runtime') == 2
    assert "Doubao, InterruptSemantic" not in script


def _shell_function(text: str, name: str) -> str:
    start = text.index(f"{name}() {{")
    end = text.index("\n}\n", start)
    return text[start : end + 3]


def _provider_expected(agent_env: Path) -> str:
    script = _script()
    start = script.index('llm_provider="$(awk')
    end = script.index("for attempt in 1 2;")
    block = script[start:end]
    program = (
        "set -Eeuo pipefail\n"
        f"agent_env={shlex.quote(str(agent_env))}\n"
        + _shell_function(script, "provider_expected_line")
        + "\n"
        + block
        + 'printf "%s\\n" "$provider_expected"\n'
    )
    result = subprocess.run(
        ["bash", "-c", program],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _required_pass_line(*, search_on: bool, llm_label: str) -> str:
    names = ["FunASR"]
    if search_on:
        names.append("QwenRealtimeSearch")
    names.extend((llm_label, "Doubao"))
    return "provider_smoke_test PASS: " + ", ".join(names)


def _write_agent_env(
    tmp_path: Path,
    *,
    search_value: str | None,
    asr: str,
    omni: str,
    llm_provider: str,
) -> Path:
    lines = [
        f"LLM_PROVIDER={llm_provider}",
        f"ASR_PROVIDER={asr}",
        f"QWEN_OMNI_SESSION_ENABLED={omni}",
    ]
    if search_value is not None:
        lines.append(f"LIVE_SEARCH_QWEN_ENABLED={search_value}")
    path = tmp_path / "agent.env"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


async def _smoke_pass_line(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    search_value: str | None,
    asr: str,
    omni: str,
    llm_provider: str,
    llm_label: str,
) -> str:
    monkeypatch.setenv("OFFLINE_MOCK", "false")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-dashscope-key")
    monkeypatch.setenv("DOUBAO_TTS_API_KEY", "test-doubao-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-deepseek-key")
    monkeypatch.setenv("DOUBAO_TTS_STYLE_CONTROL_ENABLED", "true")
    monkeypatch.setenv("LLM_PROVIDER", llm_provider)
    monkeypatch.setenv("ASR_PROVIDER", asr)
    monkeypatch.setenv("QWEN_OMNI_SESSION_ENABLED", omni)
    if search_value is None:
        monkeypatch.delenv("LIVE_SEARCH_QWEN_ENABLED", raising=False)
    else:
        monkeypatch.setenv("LIVE_SEARCH_QWEN_ENABLED", search_value)

    async def fake_doubao() -> list[tuple[bytes, tuple[str, ...], tuple[str, ...]]]:
        return [(b"\x00\x00", ("测试",), ())]

    async def fake_realtime_search(_settings: object) -> None:
        return None

    async def fake_funasr(
        pcm: bytes,
        *,
        expected_markers: tuple[str, ...],
        forbidden_markers: tuple[str, ...],
    ) -> None:
        assert pcm == b"\x00\x00"

    async def fake_llm() -> str:
        return llm_label

    monkeypatch.setattr(provider_smoke_test, "smoke_doubao", fake_doubao)
    monkeypatch.setattr(provider_smoke_test, "smoke_realtime_search", fake_realtime_search)
    monkeypatch.setattr(provider_smoke_test, "smoke_funasr", fake_funasr)
    monkeypatch.setattr(provider_smoke_test, "smoke_llm", fake_llm)

    assert await provider_smoke_test.main() == 0
    lines = [
        line
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("provider_smoke_test PASS:")
    ]
    assert len(lines) == 1
    return lines[0]


@pytest.mark.parametrize(
    ("search_value", "asr", "omni", "llm_provider", "llm_label", "search_on"),
    [
        ("false", "none", "false", "bailian_deepseek", "DeepSeek", False),
        (None, "none", "false", "bailian_deepseek", "DeepSeek", False),
        ("true", "none", "false", "bailian_deepseek", "DeepSeek", True),
        ("True", "funasr", "true", "qwen", "Qwen", True),
        ("0", "funasr", "true", "qwen", "Qwen", False),
        ("yes", "none", "false", "deepseek", "DeepSeek", True),
        ("no", "funasr", "true", "qwen", "Qwen", False),
    ],
)
async def test_readiness_expected_line_matches_smoke_for_search_flag(
    search_value: str | None,
    asr: str,
    omni: str,
    llm_provider: str,
    llm_label: str,
    search_on: bool,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    required = _required_pass_line(search_on=search_on, llm_label=llm_label)
    agent_env = _write_agent_env(
        tmp_path,
        search_value=search_value,
        asr=asr,
        omni=omni,
        llm_provider=llm_provider,
    )
    shell_line = _provider_expected(agent_env)
    smoke_line = await _smoke_pass_line(
        monkeypatch,
        capsys,
        search_value=search_value,
        asr=asr,
        omni=omni,
        llm_provider=llm_provider,
        llm_label=llm_label,
    )

    assert shell_line == required
    assert smoke_line == required
