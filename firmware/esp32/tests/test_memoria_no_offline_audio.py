"""Bench image that packs no offline .ogg, and the checks that keep that switch bench-only."""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

FIRMWARE_ROOT = pathlib.Path(__file__).resolve().parents[1]
BOARD_DIR = FIRMWARE_ROOT / "overlay/files/main/boards/memoria/esp-vocat"
BOARD_ASSETS = BOARD_DIR / "assets"
STAGER = BOARD_DIR / "memoria_assets.cmake"
PATCH_0027 = FIRMWARE_ROOT / "overlay/patches/0027-memoria-mascot-display.patch"
PATCH_0041 = FIRMWARE_ROOT / "overlay/patches/0041-memoria-no-offline-audio.patch"
SCRIPTS = FIRMWARE_ROOT / "scripts"
PINNED = 'set(DEFAULT_ASSETS_EXTRA_FILES "${CMAKE_CURRENT_SOURCE_DIR}/boards/memoria/esp-vocat/assets")'
MN6_INFO = b"mn6_cn model info fixture"
MN6_DATA = bytes(range(256)) * 4

_STORY_CHECK = '"$MEMORIA_FIRMWARE_ROOT/scripts/build_offline_stories.py" --check-only'
_AIGC_CHECK = '"$MEMORIA_FIRMWARE_ROOT/scripts/check_device_ogg_aigc.py"'


@pytest.fixture
def assets_fixture(tmp_path: pathlib.Path) -> pathlib.Path:
    """A copy of the real board assets, plus srmodels/mn6_cn and stories/nested/LOUD.OGG."""
    root = tmp_path / "assets"
    shutil.copytree(BOARD_ASSETS, root)
    model = root / "srmodels" / "mn6_cn"
    model.mkdir(parents=True)
    (model / "_MODEL_INFO_").write_bytes(MN6_INFO)
    (model / "mn6_cn_data").write_bytes(MN6_DATA)
    loud = root / "stories" / "nested" / "LOUD.OGG"
    loud.parent.mkdir(parents=True)
    loud.write_bytes(b"not-an-ogg")
    return root


def _stage(source: pathlib.Path, build_dir: pathlib.Path) -> pathlib.Path:
    """Run the stager in cmake script mode. Fails when cmake is missing."""
    build_dir.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        ["cmake", f"-DMEMORIA_ASSETS_SOURCE={source}", "-P", str(STAGER)],
        cwd=build_dir,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr)
    stage = pathlib.Path(completed.stderr.strip().splitlines()[-1])
    if not stage.is_dir():
        raise AssertionError(completed.stderr)
    return stage


def _relpaths(root: pathlib.Path) -> set[str]:
    return {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}


def _packed_basenames(root: pathlib.Path) -> set[str]:
    names: set[str] = set()
    for _dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if not name.startswith(".")]
        for name in filenames:
            if name.startswith("."):
                continue
            names.add(name)
    return names


def test_stage_is_the_source_minus_every_ogg(
    assets_fixture: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    stage = _stage(assets_fixture, tmp_path / "build")
    expected = {path for path in _relpaths(assets_fixture) if not path.lower().endswith(".ogg")}
    staged = _relpaths(stage)
    assert staged == expected
    assert not [path for path in staged if path.lower().endswith(".ogg")]
    for relative in expected:
        assert (stage / relative).read_bytes() == (assets_fixture / relative).read_bytes()


def test_stage_keeps_multinet_models_byte_for_byte(
    assets_fixture: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    stage = _stage(assets_fixture, tmp_path / "build")
    assert (stage / "srmodels/mn6_cn/_MODEL_INFO_").read_bytes() == MN6_INFO
    assert (stage / "srmodels/mn6_cn/mn6_cn_data").read_bytes() == MN6_DATA


def test_packed_list_keeps_mascots_and_wordmark_and_has_no_ogg(
    assets_fixture: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    stage = _stage(assets_fixture, tmp_path / "build")
    packed = _packed_basenames(stage)
    expected = {
        path.name
        for path in BOARD_ASSETS.iterdir()
        if path.is_file() and not path.name.startswith(".") and not path.name.lower().endswith(".ogg")
    }
    assert "brand_mark.mma" in expected
    assert any(name.startswith("mascot_") and name.endswith(".mmp") for name in expected)
    assert expected <= packed
    assert not any(name.lower().endswith(".ogg") for name in packed)


def test_stage_converges_and_drops_stale_files(
    assets_fixture: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    build = tmp_path / "build"
    seeded = build / "memoria-assets-no-offline-audio"
    seeded.mkdir(parents=True)
    (seeded / "stale.ogg").write_bytes(b"stale")
    (seeded / "old.txt").write_bytes(b"old")
    first = _stage(assets_fixture, build)
    assert not (first / "stale.ogg").exists()
    assert not (first / "old.txt").exists()
    first_tree = {relative: (first / relative).read_bytes() for relative in _relpaths(first)}
    second = _stage(assets_fixture, build)
    second_tree = {relative: (second / relative).read_bytes() for relative in _relpaths(second)}
    assert first_tree == second_tree


def test_stage_never_touches_the_source(
    assets_fixture: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    before = {relative: (assets_fixture / relative).read_bytes() for relative in _relpaths(assets_fixture)}
    real_before = {relative: (BOARD_ASSETS / relative).stat().st_size for relative in _relpaths(BOARD_ASSETS)}
    _stage(assets_fixture, tmp_path / "build")
    after = {relative: (assets_fixture / relative).read_bytes() for relative in _relpaths(assets_fixture)}
    real_after = {relative: (BOARD_ASSETS / relative).stat().st_size for relative in _relpaths(BOARD_ASSETS)}
    assert before == after
    assert real_before == real_after


def test_stage_refuses_a_missing_source(tmp_path: pathlib.Path) -> None:
    build = tmp_path / "build"
    build.mkdir()
    completed = subprocess.run(
        ["cmake", f"-DMEMORIA_ASSETS_SOURCE={tmp_path / 'missing'}", "-P", str(STAGER)],
        cwd=build,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode != 0
    assert "not a directory" in completed.stderr


def test_real_board_assets_keep_their_oggs_and_have_no_models() -> None:
    oggs = [path for path in BOARD_ASSETS.rglob("*") if path.is_file() and path.suffix.lower() == ".ogg"]
    assert oggs
    assert not (BOARD_ASSETS / "srmodels").exists()


def test_ogg_checks_still_fail_on_an_ogg_free_tree(
    assets_fixture: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    stage = _stage(assets_fixture, tmp_path / "build")
    stories = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "build_offline_stories.py"),
            "--check-only",
            "--assets-root",
            str(stage),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    aigc = subprocess.run(
        [sys.executable, str(SCRIPTS / "check_device_ogg_aigc.py"), "--assets-root", str(stage)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert stories.returncode != 0
    assert "error:" in stories.stderr
    assert aigc.returncode != 0
    assert "error:" in aigc.stderr


def test_ogg_checks_remain_unconditional() -> None:
    text = (SCRIPTS / "check-overlay.sh").read_text(encoding="utf-8")
    assert text.count(_STORY_CHECK) == 1
    assert text.count(_AIGC_CHECK) == 1
    lines = text.splitlines()
    story = next(index for index, line in enumerate(lines) if _STORY_CHECK in line)
    aigc = next(index for index, line in enumerate(lines) if _AIGC_CHECK in line)
    assert lines[story].startswith('"$python_bin"')
    assert "--check-only" in lines[story]
    assert lines[aigc].startswith('"$python_bin"')
    window = "\n".join(lines[max(0, story - 3) : aigc + 3])
    assert "if " not in window
    assert "CONFIG_MEMORIA_NO_OFFLINE_AUDIO" not in window


def test_every_manifest_keeps_multinet_and_wake_word_off() -> None:
    manifests = sorted(BOARD_DIR.glob("config*.json"))
    assert [path.name for path in manifests] == [
        "config.bench.json",
        "config.bench.no-offline-audio.json",
        "config.json",
        "config.keep-awake.json",
    ]
    for path in manifests:
        options = json.loads(path.read_text(encoding="utf-8"))["builds"][0]["sdkconfig_append"]
        assert "CONFIG_SR_MN_CN_NONE=y" in options
        assert "CONFIG_WAKE_WORD_DISABLED=y" in options
        assert not any("MULTINET" in option for option in options)
        assert not any("USE_CUSTOM_WAKE_WORD" in option for option in options)


def test_no_offline_audio_manifest_is_bench_plus_one_line() -> None:
    bench = json.loads((BOARD_DIR / "config.bench.json").read_text(encoding="utf-8"))
    no_audio = json.loads((BOARD_DIR / "config.bench.no-offline-audio.json").read_text(encoding="utf-8"))
    bench_options = bench["builds"][0]["sdkconfig_append"]
    assert no_audio["builds"][0]["sdkconfig_append"] == [
        *bench_options,
        "CONFIG_MEMORIA_NO_OFFLINE_AUDIO=y",
    ]
    no_audio["builds"][0]["sdkconfig_append"] = bench_options
    assert no_audio == bench


def test_kconfig_symbol_is_bench_only_and_default_n() -> None:
    added = "\n".join(
        line[1:]
        for line in PATCH_0041.read_text(encoding="utf-8").splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )
    block = added.split("config MEMORIA_NO_OFFLINE_AUDIO\n", 1)[1]
    assert "depends on BOARD_TYPE_MEMORIA_ESP_VOCAT && MEMORIA_BENCH_SERIAL" in block
    assert "default n" in block.split("help", 1)[0]
    assert "default y" not in block


def test_pinned_line_stays_and_staging_follows_it() -> None:
    patch_0027 = PATCH_0027.read_text(encoding="utf-8")
    assert any(
        line.startswith("+") and not line.startswith("+++") and PINNED in line
        for line in patch_0027.splitlines()
    )
    patch_0041 = PATCH_0041.read_text(encoding="utf-8")
    assert any(line.startswith(" ") and PINNED in line for line in patch_0041.splitlines())
    guard = "if(CONFIG_MEMORIA_NO_OFFLINE_AUDIO AND NOT CMAKE_BUILD_EARLY_EXPANSION)"
    call = "memoria_strip_offline_audio(DEFAULT_ASSETS_EXTRA_FILES)"
    assert patch_0041.index(PINNED) < patch_0041.index(guard) < patch_0041.index(call)
    assert patch_0041.count(call) == 1


@pytest.mark.parametrize("args", [["--no-offline-audio"], ["--keep-awake", "--no-offline-audio"]])
def test_build_sh_refuses_the_switch_without_bench(args: list[str]) -> None:
    build = (SCRIPTS / "build.sh").read_text(encoding="utf-8")
    message = "--no-offline-audio builds a bench image only; pass --bench --no-offline-audio"
    assert build.index(message) < build.index('"$SCRIPT_DIR/bootstrap.sh"')
    completed = subprocess.run(
        [str(SCRIPTS / "build.sh"), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode != 0
    assert message in completed.stderr


def test_build_sh_names_and_verifies_the_switch_before_copying() -> None:
    build = (SCRIPTS / "build.sh").read_text(encoding="utf-8")
    copy_at = build.index('cp "$merged"')
    assert "--config config.bench.no-offline-audio.json" in build
    assert 'artifact_name="$artifact_name-no-offline-audio"' in build
    assert 'artifact_name="$MEMORIA_BOARD_NAME-bench"' in build
    assert "build_args+=(--config config.bench.json)" in build
    assert (
        build.index(
            "no-offline-audio build requested, but the generated sdkconfig lacks "
            "CONFIG_MEMORIA_NO_OFFLINE_AUDIO=y"
        )
        < copy_at
    )
    assert (
        build.index("this build carries CONFIG_MEMORIA_NO_OFFLINE_AUDIO=y without --no-offline-audio")
        < copy_at
    )
    assert build.index("generated_assets.bin") < copy_at
    assert build.index('"text_font"') < copy_at
    assert build.index('"emoji_collection"') < copy_at
