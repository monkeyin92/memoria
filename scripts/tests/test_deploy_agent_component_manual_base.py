"""Manual-base release of the Agent component lane.

A candidate built by hand on the release host (for example
``memoria-agent:20261021-a94c438-pr345``) is tagged from the runtime base image
``memoria-agent-runtime-base:uv-...``. At ``--cutover`` the running bridge then
holds exactly the runtime base image id, which the lane used to reject because
the rollback target would not be independent of the new base.
``--allow-manual-base-image --reason TEXT`` accepts that case explicitly,
records it, and leaves the default refusal in place.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "deploy_agent_component.sh"
RUNTIME_BASE_REFUSAL = (
    "runtime base image is missing, has invalid provenance, or is not independent"
)
BASE_ID = "sha256:" + "a" * 64
TARGET_ID = "sha256:" + "b" * 64


def _script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _argument_validation() -> str:
    """The offline argument block that runs before any remote access."""

    script = _script()
    start = script.index('if [[ -n "$manual_base_reason" ]]; then')
    end = script.index("for command_name in git python3 ssh rsync tar; do")
    return script[start:end]


def _cutover_preflight() -> str:
    script = _script()
    start = script.index('allow_manual_base="${13}"')
    return script[start : script.index("REMOTE_CUTOVER\n")]


def test_help_documents_the_manual_base_opt_in_and_its_rollback_cost() -> None:
    script = _script()

    assert "[--allow-manual-base-image --reason TEXT]" in script
    assert "--reason TEXT" in script
    assert "also rolls back the base" in script


def test_option_requires_cutover_and_a_non_blank_reason() -> None:
    block = _argument_validation()

    assert '[[ "$cutover" == true ]] || {' in block
    assert 'echo "--allow-manual-base-image requires --reason TEXT" >&2' in block
    # --reason alone is a typo, not a silent no-op.
    assert 'echo "--reason is only valid together with --allow-manual-base-image" >&2' in block


def _run_arguments_only(*args: str) -> subprocess.CompletedProcess[str]:
    """Run the parser with the remote-dependent tail removed."""

    script = _script()
    end = script.index("for command_name in git python3 ssh rsync tar; do")
    parser = script[:end]
    parser = parser.replace(
        'ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"',
        f'ROOT="{ROOT}"',
    )
    return subprocess.run(
        ["bash", "-s", "--", *args],
        capture_output=True,
        text=True,
        check=False,
        input=parser + 'printf "ACCEPTED\\n"',
        env={"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"},
    )


_BASE_ARGUMENTS = (
    "--remote",
    "deploy",
    "--release-tag",
    "20261021-a94c438-pr345",
    "--base-image",
    "memoria-agent:20261021-a94c438-pr345",
    "--dry-run",
)


@pytest.mark.parametrize(
    ("extra", "expected_stderr"),
    [
        (("--cutover", "--allow-manual-base-image"), "--allow-manual-base-image requires --reason"),
        (("--cutover", "--reason", "pr345 手工构建"), "--reason is only valid together"),
        (("--allow-manual-base-image", "--reason", "pr345 手工构建"), "only changes --cutover"),
        (("--cutover", "--allow-manual-base-image", "--reason", "   "), "--reason needs non-blank"),
        (("--cutover", "--allow-manual-base-image", "--reason"), "--reason needs TEXT"),
    ],
)
def test_argument_validation_rejects_half_declared_manual_bases(
    extra: tuple[str, ...], expected_stderr: str
) -> None:
    completed = _run_arguments_only(*_BASE_ARGUMENTS, *extra)

    assert completed.returncode == 2
    assert expected_stderr in completed.stderr
    assert "ACCEPTED" not in completed.stdout


def test_argument_validation_accepts_the_declared_manual_base() -> None:
    completed = _run_arguments_only(
        *_BASE_ARGUMENTS, "--cutover", "--allow-manual-base-image", "--reason", "PR 345 手工构建"
    )

    assert completed.returncode == 0, completed.stderr
    assert "ACCEPTED" in completed.stdout


def test_default_cutover_still_requires_an_independent_runtime_base() -> None:
    preflight = _cutover_preflight()

    # The independence failure keeps its original message and comes first.
    refusal = preflight.index(f'echo "{RUNTIME_BASE_REFUSAL}" >&2')
    opt_in = preflight.index('if [[ "$allow_manual_base" != true ]]; then')
    assert refusal < opt_in
    # The shared metadata condition no longer treats the bridge id as fatal on
    # its own; only the gated block below does.
    condition = preflight[preflight.index('runtime_base_version="$(') : refusal]
    assert '"$runtime_base_image_id" != "$bridge_image_id"' not in condition
    assert '"$runtime_base_image_id" != "$target_image_id"' in condition
    assert "manual_base_accepted=false" in preflight


def test_accepted_manual_base_is_recorded_in_the_rollback_point_and_cutover_result() -> None:
    preflight = _cutover_preflight()

    assert 'echo "manual base image reason: $manual_base_reason" >&2' in preflight
    assert "printf 'manual_base_image_accepted=%s\\n' \"$manual_base_accepted\"" in preflight
    assert "printf 'manual_base_image_reason=%s\\n' \"$manual_base_reason\"" in preflight
    rollback_point_end = preflight.index('>"$remote_dir/ROLLBACK_POINT.txt"')
    assert (
        "manual_base_image_accepted"
        in preflight[max(0, rollback_point_end - 4000) : rollback_point_end]
    )
    assert preflight.index("manual_base_image_reason") < preflight.index("trap rollback ERR")


def test_manual_base_flag_is_passed_to_the_remote_cutover_only() -> None:
    script = _script()

    assert "cutover_args=(" in script
    assert 'ssh "$remote" "sudo -n bash -s -- ${cutover_args[*]}"' in script
    assert 'for index in "${!cutover_args[@]}"; do' in script
    assert 'cutover_args[index]="$(printf \'%q\' "${cutover_args[index]}")"' in script
    assert 'allow_manual_base="${13}"' in script
    assert 'manual_base_reason="${14}"' in script
    # The remote build step never sees the flag: it is a cutover-only decision.
    build = script[script.index("<<'REMOTE_BUILD'") : script.index("REMOTE_BUILD\n")]
    assert "allow_manual_base" not in build
    # The dry-run log states which mode a following cutover would use.
    assert "printf 'base_image_mode=manual" in script
    assert "base_image_mode=runtime-base-required" in script


def test_the_cutover_argument_line_survives_ssh_resplitting() -> None:
    """ssh joins its arguments into one command line the remote shell re-splits.

    An unquoted empty manual-base reason vanishes there and every later
    positional shifts, which failed the 2026-10-24 cutover after the image had
    already been built. A reason with a space would split in two the same way.
    """

    script = _script()
    start = script.index('for index in "${!cutover_args[@]}"; do')
    end = script.index('ssh "$remote" "sudo -n bash -s -- ${cutover_args[*]}"')
    quoting = script[start:end]
    values = (
        "/opt/memoria/component-releases/t",
        "memoria-agent:t",
        "false",
        "",
        "PR 345 hand made",
    )
    environment = {"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}
    declared = "cutover_args=(" + " ".join(shlex.quote(value) for value in values) + ")"

    quoted = subprocess.run(
        ["bash", "-c", f'{declared}\n{quoting}\nprintf "%s" "${{cutover_args[*]}}"'],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )

    assert quoted.returncode == 0, quoted.stderr
    # ssh sends that one line; the remote shell splits it back into positionals.
    resplit = subprocess.run(
        ["bash", "-c", f'printf "<%s>" {quoted.stdout}'],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )

    assert resplit.returncode == 0, resplit.stderr
    assert resplit.stdout == "".join(f"<{value}>" for value in values)
