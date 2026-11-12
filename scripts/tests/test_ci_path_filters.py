from fnmatch import fnmatchcase
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def selected(gate: str, path: str) -> bool:
    workflow = yaml.safe_load((ROOT / '.github/workflows/ci.yml').read_text())
    steps = workflow['jobs']['changes']['steps']
    filters = yaml.safe_load(next(s['with']['filters'] for s in steps if s.get('id') == 'filter'))
    patterns = filters[gate]
    return any(fnmatchcase(path, p) for p in patterns if not p.startswith('!')) and not any(
        fnmatchcase(path, p[1:]) for p in patterns if p.startswith('!')
    )


@pytest.mark.parametrize(('gate', 'path'), [
    ('python', 'devicetest/safety_soak_report.py'),
    ('python', 'devicetest/safety_card5_score.py'),
    ('python', 'devicetest/tapwake_gate.py'),
    ('python', 'devicetest/voice_soak_tapwake.py'),
    ('miniprogram', 'scripts/upload_miniprogram_test.js'),
    ('miniprogram', 'scripts/compile_miniprogram_preflight.js'),
    ('miniprogram', 'scripts/miniprogram_ci_runtime.js'),
    ('miniprogram', '.github/workflows/ci.yml'),
    ('miniprogram', 'services/session_runtime/profile_service.py'),
    ('miniprogram', 'packages/contracts/schemas/multi-subject/profile.json'),
])
def test_gate_selects_runtime_inputs(gate: str, path: str) -> None:
    assert selected(gate, path)


@pytest.mark.parametrize(('gate', 'path'), [
    ('miniprogram', 'scripts/release_ops.sh'),
    ('miniprogram', 'services/session_runtime/service.py'),
    ('miniprogram', 'packages/contracts/schemas/unrelated/profile.json'),
    ('miniprogram', 'devicetest/safety_soak_report.py'),
    ('python', 'apps/miniprogram/pages/home/index.js'),
    ('python', 'scripts/deploy_agent_component.sh'),
])
def test_gate_keeps_unrelated_work_out(gate: str, path: str) -> None:
    assert not selected(gate, path)
