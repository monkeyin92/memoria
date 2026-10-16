"""sync-pstack.sh must not delete local skills via bad manifest names."""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "sync-pstack.sh"

CHILD_AUDIO_BULLET = "- Child-facing stories, lines, or audio → never synthesize or add from draft scripts on your own. Such PRs need 产品部 and 合规部 sign-off before merge. Fill `.github/pull_request_template.md` section 「儿童可听内容（故事/台词/音频）」. See also `AGENTS.md`."

_UPSTREAM_POTETO = """\
# Poteto mode

Remaining triggers:

- Nontrivial change → the **how** skill.
"""

_EXPECTED_POTETO = (
    "# Poteto mode\n"
    "\n"
    "Remaining triggers:\n"
    "\n"
    + CHILD_AUDIO_BULLET
    + "\n"
    "- Nontrivial change → the **how** skill.\n"
)

_WEDGED_POTETO = (
    "# Poteto mode\n"
    "\n"
    "Remaining triggers:\n"
    + CHILD_AUDIO_BULLET
    + "\n"
    "\n"
    "- Nontrivial change → the **how** skill.\n"
)

_RENAMED_HEADING = """\
# Poteto mode

Other triggers:

- Nontrivial change → the **how** skill.
"""

_NO_BLANK = """\
# Poteto mode

Remaining triggers:
- Nontrivial change → the **how** skill.
"""

_TWO_PAIRS = """\
# Poteto mode

Remaining triggers:

- Nontrivial change → the **how** skill.

Remaining triggers:

- Any code.
"""

_TWO_HEADINGS_ONE_BLANK = """\
# Poteto mode

Remaining triggers:
- Nontrivial change → the **how** skill.

Remaining triggers:

- Any code.
"""


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _make_upstream(path: Path, poteto_body: str | None = _UPSTREAM_POTETO) -> None:
    skill = path / "pstack" / "skills" / "ketchup"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# ketchup\n", encoding="utf-8")
    if poteto_body is not None:
        poteto = path / "pstack" / "skills" / "poteto-mode"
        poteto.mkdir(parents=True)
        (poteto / "SKILL.md").write_text(poteto_body, encoding="utf-8")
    _git(path, "init")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "test")
    _git(path, "checkout", "-b", "main")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "seed")


def _install_script(repo: Path) -> Path:
    script_dst = repo / "scripts"
    script_dst.mkdir(parents=True)
    dest = script_dst / "sync-pstack.sh"
    dest.write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
    dest.chmod(0o755)
    return dest


def _seed_verify_memoria(repo: Path) -> Path:
    survivor = repo / ".cursor" / "skills" / "verify-memoria"
    survivor.mkdir(parents=True)
    skill = survivor / "SKILL.md"
    skill.write_text("local-only\n", encoding="utf-8")
    return skill


def _write_version(repo: Path, skills_line: str, upstream: Path) -> None:
    skills = repo / ".cursor" / "skills"
    skills.mkdir(parents=True, exist_ok=True)
    (skills / "PSTACK_VERSION").write_text(
        textwrap.dedent(
            f"""\
            repo: {upstream}
            ref: main
            commit: deadbeef
            date: 2026-01-01T00:00:00+00:00
            skills: {skills_line}
            """
        ),
        encoding="utf-8",
    )


def _snapshot_skills(repo: Path) -> dict[str, bytes]:
    root = repo / ".cursor" / "skills"
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _run_sync(repo: Path, upstream: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(repo / "scripts" / "sync-pstack.sh")],
        cwd=repo,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PSTACK_UPSTREAM_REPO": str(upstream),
            "PSTACK_UPSTREAM_REF": "main",
        },
        check=False,
    )


@pytest.mark.parametrize("name", ["", ".", "..", "a/b", "*"])
def test_check_name_rejects_bad_names_without_deleting(
    tmp_path: Path, name: str
) -> None:
    repo = tmp_path / "repo"
    survivor = _seed_verify_memoria(repo)
    script = _install_script(repo)

    result = subprocess.run(
        ["bash", str(script), "--check-name", name],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0, result.stdout + result.stderr
    assert "refuse" in (result.stderr + result.stdout).lower()
    assert survivor.is_file()
    assert survivor.read_text(encoding="utf-8") == "local-only\n"
    assert (repo / ".cursor" / "skills" / "verify-memoria").is_dir()


@pytest.mark.parametrize("skills_line", [".", "..", "a/b", "*", "ketchup *"])
def test_sync_rejects_bad_manifest_names_without_deleting(
    tmp_path: Path, skills_line: str
) -> None:
    upstream = tmp_path / "upstream"
    repo = tmp_path / "repo"
    _make_upstream(upstream)
    survivor = _seed_verify_memoria(repo)
    _install_script(repo)
    _write_version(repo, skills_line, upstream)

    result = _run_sync(repo, upstream)

    assert result.returncode != 0, result.stdout + result.stderr
    assert "refuse" in (result.stderr + result.stdout).lower()
    assert survivor.is_file()
    assert survivor.read_text(encoding="utf-8") == "local-only\n"
    assert (repo / ".cursor" / "skills" / "verify-memoria").is_dir()
    assert not (repo / ".cursor" / "skills" / "ketchup").exists()


def test_set_f_keeps_literal_star_from_expanding_into_deletes(tmp_path: Path) -> None:
    """A skills: line with '*' must not expand to every folder under skills/."""

    upstream = tmp_path / "upstream"
    repo = tmp_path / "repo"
    _make_upstream(upstream)
    survivor = _seed_verify_memoria(repo)
    # A decoy that glob '*' would match if expansion were enabled.
    decoy = repo / ".cursor" / "skills" / "decoy-skill"
    decoy.mkdir(parents=True)
    (decoy / "SKILL.md").write_text("decoy\n", encoding="utf-8")
    _install_script(repo)
    _write_version(repo, "*", upstream)

    result = _run_sync(repo, upstream)

    assert result.returncode != 0, result.stdout + result.stderr
    assert survivor.is_file()
    assert (decoy / "SKILL.md").read_text(encoding="utf-8") == "decoy\n"


def test_safe_sync_preserves_verify_memoria(tmp_path: Path) -> None:
    upstream = tmp_path / "upstream"
    repo = tmp_path / "repo"
    _make_upstream(upstream)
    survivor = _seed_verify_memoria(repo)
    _install_script(repo)
    _write_version(repo, "ketchup", upstream)

    result = _run_sync(repo, upstream)

    assert result.returncode == 0, result.stdout + result.stderr
    assert survivor.is_file()
    assert survivor.read_text(encoding="utf-8") == "local-only\n"
    assert (repo / ".cursor" / "skills" / "ketchup" / "SKILL.md").is_file()


def test_check_name_accepts_safe_name(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".cursor" / "skills").mkdir(parents=True)
    script = _install_script(repo)
    result = subprocess.run(
        ["bash", str(script), "--check-name", "ketchup"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok ketchup" in result.stdout


def test_script_is_valid_bash() -> None:
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


@pytest.mark.parametrize(
    ("upstream_body", "local_poteto"),
    [
        pytest.param(_UPSTREAM_POTETO, None, id="upstream-lacks-bullet"),
        pytest.param(_EXPECTED_POTETO, None, id="upstream-has-bullet"),
        pytest.param(_WEDGED_POTETO, None, id="bullet-between-anchor-and-blank"),
        pytest.param(
            _UPSTREAM_POTETO,
            CHILD_AUDIO_BULLET + "\n" + CHILD_AUDIO_BULLET + "\n",
            id="local-leftover-twice",
        ),
    ],
)
def test_sync_leaves_child_audio_rule_exactly_once(
    tmp_path: Path, upstream_body: str, local_poteto: str | None
) -> None:
    upstream = tmp_path / "upstream"
    repo = tmp_path / "repo"
    _make_upstream(upstream, upstream_body)
    survivor = _seed_verify_memoria(repo)
    if local_poteto is not None:
        local_skill = repo / ".cursor" / "skills" / "poteto-mode"
        local_skill.mkdir(parents=True)
        (local_skill / "SKILL.md").write_text(local_poteto, encoding="utf-8")
    _install_script(repo)
    _write_version(repo, "ketchup poteto-mode", upstream)
    skill = repo / ".cursor" / "skills" / "poteto-mode" / "SKILL.md"

    for _ in range(2):
        result = _run_sync(repo, upstream)
        assert result.returncode == 0, result.stdout + result.stderr
        text = skill.read_text(encoding="utf-8")
        assert text == _EXPECTED_POTETO
        assert text.splitlines().count(CHILD_AUDIO_BULLET) == 1
        assert survivor.read_text(encoding="utf-8") == "local-only\n"
        assert (
            repo / ".cursor" / "skills" / "ketchup" / "SKILL.md"
        ).read_text(encoding="utf-8") == "# ketchup\n"


@pytest.mark.parametrize(
    "poteto_body",
    [
        pytest.param(None, id="absent"),
        pytest.param(_RENAMED_HEADING, id="renamed-heading"),
        pytest.param(_NO_BLANK, id="no-blank"),
        pytest.param(_TWO_PAIRS, id="two-pairs"),
        pytest.param(_TWO_HEADINGS_ONE_BLANK, id="two-headings-one-blank"),
    ],
)
def test_sync_refuses_without_unique_anchor_and_writes_nothing(
    tmp_path: Path, poteto_body: str | None
) -> None:
    upstream = tmp_path / "upstream"
    repo = tmp_path / "repo"
    _make_upstream(upstream, poteto_body)
    survivor = _seed_verify_memoria(repo)
    poteto = repo / ".cursor" / "skills" / "poteto-mode"
    poteto.mkdir(parents=True)
    (poteto / "SKILL.md").write_text(_EXPECTED_POTETO, encoding="utf-8")
    _install_script(repo)
    before = _snapshot_skills(repo)

    result = _run_sync(repo, upstream)

    assert result.returncode != 0, result.stdout + result.stderr
    assert "refuse" in result.stderr.lower()
    assert _snapshot_skills(repo) == before
    assert survivor.read_text(encoding="utf-8") == "local-only\n"


def test_committed_poteto_mode_carries_child_audio_rule() -> None:
    text = (ROOT / ".cursor" / "skills" / "poteto-mode" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    lines = text.splitlines()
    assert lines.count(CHILD_AUDIO_BULLET) == 1
    idx = lines.index("Remaining triggers:")
    assert lines[idx + 1] == ""
    assert lines[idx + 2] == CHILD_AUDIO_BULLET
