from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.skipif(
    not os.environ.get("MEMORIA_CAMPLUS_ONNX_PATH")
    or not os.environ.get("MEMORIA_CAMPLUS_OFFICIAL_WAV_DIR"),
    reason="real CAM++ artifact smoke requires explicit local model and official WAV paths",
)
def test_pinned_official_campplus_examples_separate_speakers() -> None:
    model = Path(os.environ["MEMORIA_CAMPLUS_ONNX_PATH"])
    wav_dir = Path(os.environ["MEMORIA_CAMPLUS_OFFICIAL_WAV_DIR"])
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "smoke_campplus_onnx.py"),
            "--model",
            str(model),
            "--speaker1-a",
            str(wav_dir / "speaker1_a_cn_16k.wav"),
            "--speaker1-b",
            str(wav_dir / "speaker1_b_cn_16k.wav"),
            "--speaker2-a",
            str(wav_dir / "speaker2_a_cn_16k.wav"),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert '"embedding_dimensions": 192' in completed.stdout
