"""Run deterministic media-runtime fixture, chaos, and load checks."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from services.agent.src.voice_core.replay_harness import (
    AudioReplayHarness,
    ChaosEvent,
    ChaosRunner,
    FixtureMetadata,
    LoadScenario,
    SyntheticFixture,
    estimate_load,
)


def main() -> None:
    manifest_path = Path(__file__).parents[1] / "packages/contracts/child-speech-corpus.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("status") == "consented_recorded":
        fixtures = manifest.get("fixtures", [])
        if len(fixtures) < 200:
            raise SystemExit("consented_recorded manifest requires at least 200 fixtures")
        consent_artifact = manifest.get("consent_artifact", "")
        if not consent_artifact:
            raise SystemExit("consented_recorded manifest requires consent_artifact")
        root = Path(__file__).parents[1]
        for item in fixtures:
            audio_path = item.get("audio_path")
            if not audio_path or not (root / audio_path).is_file():
                raise SystemExit(f"recorded fixture audio is missing: {audio_path!r}")
    replay = AudioReplayHarness()
    results = []
    for item in manifest["fixtures"]:
        metadata = FixtureMetadata(
            fixture_id=item["id"],
            **{key: value for key, value in item.items() if key not in {"id", "audio_path"}},
        )
        result = replay.replay(SyntheticFixture(metadata), stream_epoch=1)
        results.append(result)
    chaos = ChaosRunner().run(
        (
            ChaosEvent(100, "asr_disconnect", 300),
            ChaosEvent(300, "edge_restart", 1_000),
            ChaosEvent(400, "late_final"),
        )
    )
    load = estimate_load(LoadScenario(sessions=10, duration_s=60))
    summary = {
        "fixtures": [asdict(result) for result in results],
        "chaos": asdict(chaos),
        "load": asdict(load),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    if not all(result.passed for result in results) or not chaos.passed or not load.passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
