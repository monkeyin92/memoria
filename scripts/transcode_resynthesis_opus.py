#!/usr/bin/env python3
"""Transcode a resynthesis drop of 24 kHz WAVs into the board's Ogg Opus pack format.

The pack contract is the one ``build_offline_stories.py`` and
``render_aigc_notices.py`` already use: Ogg Opus, mono, 16 kHz, 16 kbps CBR.
Each clip carries the GB 45438-2025 Appendix-E implicit label as Vorbis
comments (CONTENT_ID, SERVICE_PROVIDER, AI_GENERATED, ARTIST, VOICE_ID, DATE,
CONTENT_PRODUCER, CONTENT_PROPAGATOR, LABEL=1). Audio is encoded once; the tag
rewrite afterwards leaves every Opus packet byte-identical, which this script
checks.

Nothing here changes spoken text: the manifest it reads comes from the
resynthesis job, and each file's WAV duration is compared with the job's
measured duration.

Writes ``opus-ffprobe-metadata.json`` (ffprobe JSON per file) and
``文件名对应文稿条目.md`` (file -> script file + entry) into ``--out-dir``.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from services.common.aigc_opus import (  # noqa: E402
    CANONICAL_SERVICE_PROVIDER,
    LABEL_EXPLICIT,
    aigc_tag_map,
    audio_packets_digest,
    implicit_label,
    require_aigc_tags,
    rewrite_opus_comments,
)

KEEP = "按定稿原样合成"
PACK_SAMPLE_RATE_HZ = 16_000
PACK_BITRATE = "16000"
ENCODE_BITRATE_BPS = 16_000
# Phone numbers are read out digit by digit, as the locked script requires.
SPOKEN_DIGITS = {"12356": "幺二三五六", "120": "幺二零", "110": "幺幺零"}


CONTENT_ROOT = Path("share/_from-box-20261020/memoria-content")
PRODUCT_ROOT = Path("share/产品文稿")


@dataclass(frozen=True)
class Source:
    """Where one clip's spoken text comes from, and whether it is shipped."""

    path: Path
    entry: str
    same_text: bool
    shipped: bool


def _script(name: str) -> Path:
    return CONTENT_ROOT / name


def _product(name: str) -> Path:
    return PRODUCT_ROOT / name


SOURCES: Mapping[str, Source] = {
    "offline-wake-no-network-v1": Source(_script("offline-lines-v2.md"), "#1", True, True),
    "offline-wake-ask-parent-v1": Source(_script("offline-lines-v2.md"), "#2", True, True),
    "offline-wake-story-hint-v2": Source(_script("offline-lines-v2.md"), "#3", True, True),
    "offline-mid-drop-v2": Source(_script("offline-lines-v2.md"), "#4", True, True),
    "offline-mid-ask-parent-v1": Source(_script("offline-lines-v2.md"), "#5", True, True),
    "offline-mid-retry-v1": Source(_script("offline-lines-v2.md"), "#6", True, True),
    "offline-reconnecting-v1": Source(_script("offline-lines-v2.md"), "#7", True, True),
    "online-restored-v1": Source(_script("offline-lines-v2.md"), "#8", True, True),
    "online-restored-chat-v1": Source(_script("offline-lines-v2.md"), "#9", True, True),
    "online-restored-in-story-v2": Source(_script("offline-lines-v2.md"), "#10", True, True),
    "offline-menu-intro-v2": Source(_script("offline-lines-v2.md"), "#11", True, True),
    "offline-menu-how-v1": Source(_script("offline-lines-v2.md"), "#12", True, True),
    "offline-menu-confirm-how-v2": Source(_script("offline-lines-v2.md"), "#13", True, True),
    "offline-menu-item-01-v2": Source(_script("offline-lines-v2.md"), "#14", True, True),
    "offline-menu-item-02-v2": Source(_script("offline-lines-v2.md"), "#15", True, True),
    "offline-menu-item-03-v2": Source(_script("offline-lines-v2.md"), "#16", True, True),
    "offline-menu-choose-v2": Source(_script("offline-lines-v2.md"), "#17", True, True),
    "offline-menu-start-v1": Source(_script("offline-lines-v2.md"), "#18", True, True),
    "offline-menu-end-v1": Source(_script("offline-lines-v2.md"), "#19", True, True),
    "offline-menu-idle-v1": Source(_script("offline-lines-v2.md"), "#20", True, True),
    "story_01": Source(_script("offline-stories-v2.md"), "story_01「小星星的枕头」", True, True),
    "story_02": Source(_script("offline-stories-v2.md"), "story_02「月亮不着急」", True, True),
    "story_03": Source(
        _script("offline-stories-v2.md"), "story_03《大熊和森林里的悄悄话》", True, True
    ),
    "aigc_story_intro": Source(_script("offline-lines-v2.md"), "AIGC 播放前提示", True, True),
    "aigc_soothe_intro": Source(_script("offline-lines-v2.md"), "AIGC 播放前提示", True, True),
    # Spoken with the hotline digits read out by name; the script keeps "12356".
    "crisis-self-v1": Source(
        _product("安全话术定稿-20261021.md"), "crisis-self-v1（自伤档）", False, False
    ),
    "victim-support-v1": Source(
        _product("安全话术定稿-20261021.md"), "victim-support-v1（受害档）", False, False
    ),
    "support-other-v1": Source(
        _product("安全话术定稿-20261021.md"), "support-other-v1（转述档）", False, False
    ),
    "minor-daily-limit": Source(
        Path("services/agent/src/prompts.py"), "MINOR_DAILY_LIMIT_PHRASE", True, False
    ),
}

METADATA_NAME = "opus-ffprobe-metadata.json"
MANIFEST_NAME = "文件名对应文稿条目.md"


def _duration_s(wav: Path) -> float:
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            str(wav),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(completed.stdout.strip())


def _comment_count(head: bytes) -> int:
    at = head.find(b"OpusTags")
    if at < 0:
        raise ValueError("no OpusTags header")
    vendor_len = int.from_bytes(head[at + 8 : at + 12], "little")
    count_at = at + 12 + vendor_len
    return int.from_bytes(head[count_at : count_at + 4], "little")


def encode_wav(
    wav: Path,
    ogg: Path,
    *,
    aigc: Mapping[str, str],
    title: str,
) -> dict[str, object]:
    """Encode once with the pack settings, then add the implicit AIGC comments."""

    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(wav),
            "-map_metadata",
            "-1",
            "-ac",
            "1",
            "-ar",
            str(PACK_SAMPLE_RATE_HZ),
            "-c:a",
            "libopus",
            "-b:a",
            PACK_BITRATE,
            "-vbr",
            "off",
            "-application",
            "voip",
            str(ogg),
        ],
        check=True,
    )
    comments = {"TITLE": title, **dict(aigc)}
    raw = ogg.read_bytes()
    tagged = rewrite_opus_comments(raw, aigc=comments)
    if audio_packets_digest(raw) != audio_packets_digest(tagged):
        raise RuntimeError(f"{ogg.name}: tag rewrite changed audio packets")
    # Packed into the on-device assets index by basename, and the board scans
    # only the first page of comments for the AIGC keys.
    if _comment_count(tagged[:65536]) != len(comments):
        raise RuntimeError(f"{ogg.name}: OpusTags comments did not fit one page")
    require_aigc_tags(tagged[:65536], content_id=title, implicit=True)
    ogg.write_bytes(tagged)
    return {
        "audio_packets_sha256": audio_packets_digest(tagged),
        "tags": aigc_tag_map(tagged[:65536]),
    }


def _cells(line: str) -> list[str]:
    return (
        [cell.strip() for cell in line.strip().strip("|").split("|")]
        if line.lstrip().startswith("|")
        else []
    )


def _numbered_row(lines: list[str], entry: str, clip_id: str) -> str:
    entry = entry.removeprefix("#")
    matches = [line for line in lines if _cells(line)[:2] == [entry, f"`{clip_id}`"]]
    if len(matches) != 1:
        raise RuntimeError(f"{entry} {clip_id}: {len(matches)} table rows")

    return _cells(matches[0])[4]


def _script_entry_text(path: Path, entry: str, clip_id: str) -> str:
    if path.suffix == ".md":
        lines = path.read_text(encoding="utf-8").split("\n")
        if entry.startswith("#"):
            return _numbered_row(lines, entry, clip_id)
        if "「" in entry or "《" in entry:
            heading = "## " + entry
            at = next(i for i, line in enumerate(lines) if line.startswith(heading))
            body = []
            for line in lines[at + 1 :]:
                if line.startswith("## "):
                    break
                body.append(line.strip())
            return "\n".join(line for line in body if line and not line.startswith("-"))
        if "（" not in entry:
            # Section entry: the bullet under the heading that starts with the clip id.
            at = next(i for i, line in enumerate(lines) if line.startswith("## " + entry))
            for line in lines[at + 1 :]:
                if not line.startswith("- "):
                    break
                if line.startswith(f"- `{clip_id}`："):
                    start = line.index("「") + 1
                    return line[start : line.index("」", start)]
            raise RuntimeError(f"{path}: {clip_id} not found under {entry}")
        heading = entry.split("（")[0]
        at = next(i for i, line in enumerate(lines) if line.startswith("## " + heading))
        return lines[at + 1].strip()
    match = re.search(
        rf'^{entry} = "([^"]+)"', path.read_text(encoding="utf-8"), flags=re.MULTILINE
    )
    if match is None:
        raise RuntimeError(f"{path}: {entry} not found")
    return match.group(1)


def as_spoken(text: str) -> str:
    for digits, spoken in SPOKEN_DIGITS.items():
        text = text.replace(digits, spoken)
    # The script leaves the story count as {{故事数}}; the pack fills in "三".
    return text.replace("{{故事数}}", "三")


def _spoken_text(result: list[dict[str, object]], clip_id: str) -> str:
    for row in result:
        if row.get("id") == clip_id and not row.get("error"):
            return str(row["text"])
    raise RuntimeError(f"result.json has no text for {clip_id}")


def _ffprobe(path: Path) -> dict[str, object]:
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-show_chapters",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def transcode(
    root: Path,
    script_root: Path,
    out_dir: Path,
    *,
    voice_id: str,
    provider: str,
    generated: str,
    producer: str,
    propagator: str,
) -> dict[str, object]:
    result = json.loads((root / "result.json").read_text(encoding="utf-8"))
    measured = {row["id"]: float(row["seconds"]) for row in result if row.get("file")}
    wavs = sorted(root.glob("*.wav"))
    unknown = [wav.stem for wav in wavs if wav.stem not in SOURCES]
    if unknown:
        raise RuntimeError("unmapped clips: " + ", ".join(unknown))

    out_dir.mkdir(parents=True, exist_ok=True)
    files: list[dict[str, object]] = []
    rows: list[dict[str, object]] = []
    for wav in wavs:
        clip_id = wav.stem
        source = SOURCES[clip_id]
        scripted = _script_entry_text(script_root / source.path, source.entry, clip_id)
        # A whole script file against one line only makes sense for the tables.
        same = source.same_text
        spoken = as_spoken(scripted) if source.path.suffix == ".md" else scripted
        if "".join(_spoken_text(result, clip_id).split()) != "".join(spoken.split()):
            raise RuntimeError(
                f"{clip_id}: resynthesis text differs from {source.path} {source.entry}"
            )
        aigc = implicit_label(
            content_id=clip_id,
            aigc={
                "SERVICE_PROVIDER": provider,
                "ARTIST": voice_id,
                "VOICE_ID": voice_id,
                "DATE": generated,
            },
            content_producer=producer,
            content_propagator=propagator,
        )
        ogg = out_dir / f"{clip_id}.ogg"
        encode = encode_wav(wav, ogg, aigc=aigc, title=clip_id)
        wav_seconds = _duration_s(wav)
        ogg_seconds = _duration_s(ogg)
        if abs(wav_seconds - measured[clip_id]) > 0.15:
            raise RuntimeError(
                f"{clip_id}: WAV is {wav_seconds:.2f}s, job measured {measured[clip_id]}s"
            )
        if abs(ogg_seconds - wav_seconds) > 0.05:
            raise RuntimeError(f"{clip_id}: ogg is {ogg_seconds:.3f}s, wav is {wav_seconds:.3f}s")
        probe = _ffprobe(ogg)
        size = ogg.stat().st_size
        files.append(
            {
                "file": ogg.name,
                "bytes": size,
                "seconds": round(ogg_seconds, 3),
                "wav_bytes": wav.stat().st_size,
                "wav_seconds": round(wav_seconds, 3),
                "written_matches_spoken": same,
                "audio_packets_sha256": encode["audio_packets_sha256"],
                "tags": encode["tags"],
                "ffprobe": probe,
            }
        )
        rows.append(
            {
                "id": clip_id,
                "script_path": source.path,
                "entry": source.entry,
                "written_matches_spoken": same,
                "shipped": source.shipped,
                "bytes": size,
                "seconds": ogg_seconds,
            }
        )

    total = sum(int(row["bytes"]) for row in files)
    largest = max(files, key=lambda row: int(row["bytes"]))
    payload = {
        "source_dir": str(root),
        "script_root": str(script_root),
        "contract": {
            "codec": "opus",
            "container": "ogg",
            "channels": 1,
            "sample_rate_hz": PACK_SAMPLE_RATE_HZ,
            "bitrate_bps": ENCODE_BITRATE_BPS,
            "ffmpeg": "libopus, -vbr off -application voip",
            "tag_rewrite": "services/common/aigc_opus.rewrite_opus_comments (audio packets unchanged)",
        },
        "voice": {"id": voice_id, "service_provider": provider},
        "aigc": {
            "standard": "GB 45438-2025 附录 E 隐式标识 / 《人工智能生成合成内容标识办法》",
            "content_producer": producer,
            "content_propagator": propagator,
            "label": LABEL_EXPLICIT,
            "per_file_keys": sorted(files[0]["tags"]) if files else [],
        },
        "count": len(files),
        "total_bytes": total,
        "largest": {"file": largest["file"], "bytes": int(largest["bytes"])},
        "audio_packets_digest_method": "sha256 of concatenated Opus packets after OpusHead/OpusTags",
        "files": files,
    }
    (out_dir / METADATA_NAME).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (out_dir / MANIFEST_NAME).write_text(
        _render_manifest(rows, total, payload["largest"]), encoding="utf-8"
    )
    return payload


def _size(num_bytes: int) -> str:
    return f"{num_bytes} 字节 / {num_bytes / 1024:.1f} KiB / {num_bytes / 1024 / 1024:.2f} MiB"


def _render_manifest(
    rows: Sequence[dict[str, object]], total: int, largest: Mapping[str, object]
) -> str:
    out = [
        "# 文件名对应文稿条目",
        "",
        "由 `scripts/transcode_resynthesis_opus.py` 从本目录 `result.json` 和定稿文稿生成，"
        "不改任何文稿和音频内容。",
        "",
        f"- 文件数：{len(rows)}；Opus 总计 {_size(total)}，单条最大 "
        f"{largest['file']} {_size(int(largest['bytes']))}。",
        "- 格式：Ogg Opus 单声道 16 kHz，16 kbps CBR（与固件 assets 包一致）。",
        "- 隐式标识：每个文件带 CONTENT_ID、SERVICE_PROVIDER、AI_GENERATED、ARTIST、VOICE_ID、"
        "DATE、CONTENT_PRODUCER、CONTENT_PROPAGATOR、LABEL=1，逐文件值见 ffprobe JSON。",
        "",
        "| 文件 | 秒数 | 字节 | 文稿文件 | 条目 | 文字与文稿异同 | 是否随固件包发布 |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        diff = "一字不差" if row["written_matches_spoken"] else "同稿，号码念汉字"
        shipped = "是" if row["shipped"] else "否（线上实时合成）"
        out.append(
            f"| {row['id']}.ogg | {float(row['seconds']):.1f} | {row['bytes']} | "
            f"`{row['script_path']}` | {row['entry']} | {diff} | {shipped} |"
        )
    out.extend(["", "## 说明", ""])
    out.append(
        "文稿文件位置：`offline-lines-v2.md`、`offline-stories-v2.md` 在 "
        "`share/_from-box-20261020/memoria-content/`；`安全话术定稿-20261021.md` 在 "
        "`share/产品文稿/`；`services/agent/src/prompts.py` 在仓库内。"
    )
    out.extend(
        [
            "",
            "- `aigc_story_intro`、`aigc_soothe_intro` 是插在各条目之间的独立提示句，文稿本身不编号，"
            "出处是 `offline-lines-v2.md` 的「AIGC 播放前提示（2026-10-15 合规认可）」一节。",
            "- `crisis-self-v1`、`victim-support-v1`、`support-other-v1` 按定稿把 12356/120/110 念成"
            "「幺二三五六」「幺二零」「幺幺零」，文字不改；这 3 条线上实时合成，不随固件包发布。",
            "- `minor-daily-limit` 对应 `MINOR_DAILY_LIMIT_PHRASE`，线上实时合成，不随固件包发布。",
            "- 20 条断网台词、3 个故事、2 条 AI 提示，连同已有的 `llm_thinking`、`llm_unavailable`、"
            "`wo_zai_ne`、`night_soothe` 组成板端 assets 包；本目录只放转码结果，未写入仓库。",
            "",
        ]
    )
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument(
        "--out-dir",
        type=Path,
        required=True,
        help="directory for the ogg files and the two export files",
    )
    parser.add_argument(
        "--script-root", type=Path, required=True, help="project root holding share/"
    )
    parser.add_argument("--voice-id", required=True)
    parser.add_argument("--service-provider", default=CANONICAL_SERVICE_PROVIDER)
    parser.add_argument("--generated", required=True, help="AIGC DATE, e.g. 2026-10-21")
    parser.add_argument("--content-producer", required=True)
    parser.add_argument("--content-propagator")
    args = parser.parse_args(argv)
    payload = transcode(
        args.source_dir,
        args.script_root,
        args.out_dir,
        voice_id=args.voice_id,
        provider=args.service_provider,
        generated=args.generated,
        producer=args.content_producer,
        propagator=args.content_propagator or args.content_producer,
    )
    print(f"files={payload['count']}")
    print(f"total_opus_bytes={payload['total_bytes']}")
    print(f"largest_opus={payload['largest']['file']} {payload['largest']['bytes']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
