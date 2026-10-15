"""Appendix-E AIGC OpusTags on Ogg Opus clips.

Implicit labels for shipped audio follow GB 45438-2025 style Vorbis comments:
CONTENT_ID, SERVICE_PROVIDER, AI_GENERATED, ARTIST, VOICE_ID, DATE, and
CONTENT_PRODUCER. ARTIST and VOICE_ID both carry the TTS voice id.
Provider-embedded marks are never stripped.

``rewrite_opus_comments`` replaces only the OpusTags comment packet and leaves
audio packets byte-identical (verified by ``audio_packets_digest``).
"""

from __future__ import annotations

import hashlib
import struct
from collections.abc import Mapping
from typing import Final

REQUIRED_AIGC_TAG_KEYS: Final = ("CONTENT_ID", "SERVICE_PROVIDER", "AI_GENERATED")
IMPLICIT_AIGC_TAG_KEYS: Final = (
    "CONTENT_ID",
    "SERVICE_PROVIDER",
    "AI_GENERATED",
    "ARTIST",
    "VOICE_ID",
    "DATE",
    "CONTENT_PRODUCER",
)
CANONICAL_SERVICE_PROVIDER: Final = "Volcengine Doubao"
CANONICAL_AI_GENERATED: Final = "true"
OPUS_TAGS_SCAN_BYTES: Final = 64 * 1024


def _build_ogg_crc_table() -> tuple[int, ...]:
    # Ogg CRC table (RFC 3533); polynomial 0x04c11db7.
    table: list[int] = []
    for i in range(256):
        r = i << 24
        for _ in range(8):
            if r & 0x80000000:
                r = ((r << 1) ^ 0x04C11DB7) & 0xFFFFFFFF
            else:
                r = (r << 1) & 0xFFFFFFFF
        table.append(r)
    return tuple(table)


_OGG_CRC_TABLE: Final = _build_ogg_crc_table()


def _ogg_crc(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ _OGG_CRC_TABLE[((crc >> 24) & 0xFF) ^ byte]
    return crc


def opus_tag_pairs(head: bytes) -> list[tuple[str, str]]:
    if not head.startswith(b"OggS"):
        raise ValueError("not an Ogg stream")
    at = head.find(b"OpusTags")
    if at < 0:
        raise ValueError("no OpusTags header")
    pos = at + len(b"OpusTags")
    pairs: list[tuple[str, str]] = []
    try:
        (vendor_len,) = struct.unpack_from("<I", head, pos)
        pos += 4 + vendor_len
        (count,) = struct.unpack_from("<I", head, pos)
        pos += 4
        for _ in range(count):
            (length,) = struct.unpack_from("<I", head, pos)
            pos += 4
            raw = head[pos : pos + length]
            pos += length
            key, sep, value = raw.decode("utf-8").partition("=")
            if sep:
                pairs.append((key, value))
    except (struct.error, UnicodeDecodeError) as exc:
        raise ValueError("truncated OpusTags") from exc
    return pairs


def aigc_tag_map(head: bytes) -> dict[str, str]:
    return {key.upper(): value for key, value in opus_tag_pairs(head)}


def require_aigc_tags(
    head: bytes,
    *,
    content_id: str,
    implicit: bool = False,
) -> dict[str, object]:
    """Return normalised AIGC tags or raise ``ValueError`` when the clip is unlabelled."""

    tags = aigc_tag_map(head)
    required = IMPLICIT_AIGC_TAG_KEYS if implicit else REQUIRED_AIGC_TAG_KEYS
    missing = [key for key in required if not tags.get(key, "").strip()]
    if missing:
        raise ValueError("missing AIGC tags: " + ", ".join(missing))
    if tags.get("CONTENT_ID", "").strip() != content_id:
        raise ValueError(f"CONTENT_ID must be {content_id}")
    if tags.get("AI_GENERATED", "").strip() != CANONICAL_AI_GENERATED:
        raise ValueError(f"AI_GENERATED must be {CANONICAL_AI_GENERATED}")
    if not tags.get("SERVICE_PROVIDER", "").strip():
        raise ValueError("SERVICE_PROVIDER must be non-empty")
    result: dict[str, object] = {
        "content_id": tags["CONTENT_ID"].strip(),
        "service_provider": tags["SERVICE_PROVIDER"].strip(),
        "ai_generated": True,
    }
    if implicit:
        result["artist"] = tags["ARTIST"].strip()
        result["voice_id"] = tags["VOICE_ID"].strip()
        result["date"] = tags["DATE"].strip()
        result["content_producer"] = tags["CONTENT_PRODUCER"].strip()
    return result


def title_is_placeholder(head: bytes) -> bool:
    for key, value in opus_tag_pairs(head):
        if key.lower() == "title" and value.endswith("_placeholder"):
            return True
    return False


def aigc_label_from_mapping(tags: Mapping[str, object]) -> dict[str, object]:
    return {
        "content_id": str(tags["content_id"]),
        "service_provider": str(tags["service_provider"]),
        "ai_generated": bool(tags["ai_generated"]),
    }


def _parse_pages(data: bytes) -> list[bytes]:
    pages: list[bytes] = []
    pos = 0
    while pos + 27 <= len(data):
        if data[pos : pos + 4] != b"OggS":
            raise ValueError(f"bad Ogg capture at offset {pos}")
        nseg = data[pos + 26]
        header_size = 27 + nseg
        if pos + header_size > len(data):
            raise ValueError("truncated Ogg page header")
        body_size = sum(data[pos + 27 : pos + header_size])
        end = pos + header_size + body_size
        if end > len(data):
            raise ValueError("truncated Ogg page body")
        pages.append(data[pos:end])
        pos = end
    if pos != len(data):
        raise ValueError("trailing bytes after last Ogg page")
    return pages


def _page_packets(pages: list[bytes]) -> list[bytes]:
    packets: list[bytes] = []
    cont = bytearray()
    for page in pages:
        nseg = page[26]
        segs = page[27 : 27 + nseg]
        body = page[27 + nseg :]
        offset = 0
        for seg in segs:
            cont.extend(body[offset : offset + seg])
            offset += seg
            if seg < 255:
                packets.append(bytes(cont))
                cont.clear()
    if cont:
        packets.append(bytes(cont))
    return packets


def audio_packets_digest(data: bytes) -> str:
    """SHA-256 of concatenated Opus audio packets (everything after OpusHead/OpusTags)."""

    packets = _page_packets(_parse_pages(data))
    if len(packets) < 2:
        raise ValueError("Ogg Opus stream missing OpusHead/OpusTags")
    if not packets[0].startswith(b"OpusHead"):
        raise ValueError("first packet is not OpusHead")
    if not packets[1].startswith(b"OpusTags"):
        raise ValueError("second packet is not OpusTags")
    return hashlib.sha256(b"".join(packets[2:])).hexdigest()


def pcm_digest(data: bytes) -> str:
    """SHA-256 of ffmpeg-decoded s16le PCM, or raise if the stream does not decode."""

    import subprocess
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "in.ogg"
        src.write_bytes(data)
        completed = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(src),
                "-f",
                "s16le",
                "-acodec",
                "pcm_s16le",
                "-ac",
                "1",
                "-",
            ],
            check=False,
            capture_output=True,
        )
        if completed.returncode != 0 or not completed.stdout:
            raise ValueError(
                "ffmpeg decode failed: "
                + completed.stderr.decode("utf-8", errors="replace").strip()
            )
        return hashlib.sha256(completed.stdout).hexdigest()


def _build_opus_tags_packet(
    vendor: bytes,
    comments: list[tuple[str, str]],
) -> bytes:
    body = bytearray(b"OpusTags")
    body += struct.pack("<I", len(vendor))
    body += vendor
    body += struct.pack("<I", len(comments))
    for key, value in comments:
        raw = f"{key}={value}".encode()
        body += struct.pack("<I", len(raw))
        body += raw
    return bytes(body)


def _build_page(
    *,
    header_type: int,
    granule: int,
    serial: int,
    page_no: int,
    body: bytes,
) -> bytes:
    if len(body) > 255 * 255:
        raise ValueError("page body too large for a single Ogg page")
    segments = []
    remaining = len(body)
    offset = 0
    while True:
        take = min(255, remaining)
        segments.append(take)
        remaining -= take
        offset += take
        if take < 255:
            break
        if remaining == 0:
            # Exact multiple of 255 needs a trailing 0-length segment to end the packet.
            segments.append(0)
            break
    header = bytearray(27 + len(segments))
    header[0:4] = b"OggS"
    header[4] = 0
    header[5] = header_type & 0xFF
    struct.pack_into("<q", header, 6, granule)
    struct.pack_into("<I", header, 14, serial)
    struct.pack_into("<I", header, 18, page_no)
    struct.pack_into("<I", header, 22, 0)  # CRC placeholder
    header[26] = len(segments)
    header[27:] = bytes(segments)
    page = bytes(header) + body
    crc = _ogg_crc(page)
    return page[:22] + struct.pack("<I", crc) + page[26:]


def _parse_opus_tags_packet(packet: bytes) -> tuple[bytes, list[tuple[str, str]]]:
    if not packet.startswith(b"OpusTags"):
        raise ValueError("not an OpusTags packet")
    pos = 8
    (vendor_len,) = struct.unpack_from("<I", packet, pos)
    pos += 4
    vendor = packet[pos : pos + vendor_len]
    pos += vendor_len
    (count,) = struct.unpack_from("<I", packet, pos)
    pos += 4
    comments: list[tuple[str, str]] = []
    for _ in range(count):
        (length,) = struct.unpack_from("<I", packet, pos)
        pos += 4
        raw = packet[pos : pos + length]
        pos += length
        key, sep, value = raw.decode("utf-8").partition("=")
        if not sep:
            raise ValueError("comment missing '='")
        comments.append((key, value))
    return vendor, comments


def merge_aigc_comments(
    existing: list[tuple[str, str]],
    *,
    aigc: Mapping[str, str],
    drop_keys: frozenset[str] | None = None,
) -> list[tuple[str, str]]:
    """Keep non-AIGC comments; set/overwrite the implicit AIGC keys."""

    aigc_upper = {key.upper(): value for key, value in aigc.items()}
    drop_upper = {key.upper() for key in (drop_keys or frozenset())}
    for key in IMPLICIT_AIGC_TAG_KEYS:
        if key not in aigc_upper or not aigc_upper[key].strip():
            raise ValueError(f"missing AIGC value for {key}")
    kept: list[tuple[str, str]] = []
    for key, value in existing:
        upper = key.upper()
        if upper in IMPLICIT_AIGC_TAG_KEYS or upper in drop_upper:
            continue
        kept.append((key, value))
    for key in IMPLICIT_AIGC_TAG_KEYS:
        kept.append((key, aigc_upper[key]))
    return kept


def rewrite_opus_comments(
    data: bytes,
    *,
    aigc: Mapping[str, str],
    drop_keys: frozenset[str] | None = None,
) -> bytes:
    """Rewrite OpusTags only. Audio packets stay byte-identical."""

    pages = _parse_pages(data)
    packets = _page_packets(pages)
    if len(packets) < 2:
        raise ValueError("Ogg Opus stream missing headers")
    if not packets[0].startswith(b"OpusHead"):
        raise ValueError("first packet is not OpusHead")
    if not packets[1].startswith(b"OpusTags"):
        raise ValueError("second packet is not OpusTags")

    # Require OpusHead on page 0 and OpusTags alone on following page(s) before audio.
    if pages[0][5] & 0x01:
        raise ValueError("continued page at start")
    # Find how many pages the OpusTags packet spans (segment lacing).
    tags_page_start = 1
    tags_page_end = 1
    cont = bytearray()
    for idx in range(1, len(pages)):
        page = pages[idx]
        nseg = page[26]
        segs = page[27 : 27 + nseg]
        body = page[27 + nseg :]
        offset = 0
        ended = False
        for seg in segs:
            cont.extend(body[offset : offset + seg])
            offset += seg
            if seg < 255:
                tags_page_end = idx + 1
                ended = True
                break
        if ended:
            break
    else:
        raise ValueError("OpusTags packet never ends")
    if bytes(cont) != packets[1]:
        raise ValueError("failed to isolate OpusTags pages")

    vendor, existing = _parse_opus_tags_packet(packets[1])
    new_tags = _build_opus_tags_packet(
        vendor, merge_aigc_comments(existing, aigc=aigc, drop_keys=drop_keys)
    )

    # Rebuild tags as a single fresh page using the original stream serial / page no.
    tags_page = pages[tags_page_start]
    serial = struct.unpack_from("<I", tags_page, 14)[0]
    page_no = struct.unpack_from("<I", tags_page, 18)[0]
    header_type = tags_page[5] & ~0x01  # not a continuation
    # If tags previously spanned multiple pages, collapse to one page and shift
    # is not allowed without rewriting later page numbers. Require single-page tags.
    if tags_page_end - tags_page_start != 1:
        raise ValueError("multi-page OpusTags rewrite not supported")
    new_tags_page = _build_page(
        header_type=header_type,
        granule=0,
        serial=serial,
        page_no=page_no,
        body=new_tags,
    )
    out = b"".join([pages[0], new_tags_page, *pages[tags_page_end:]])
    before = audio_packets_digest(data)
    after = audio_packets_digest(out)
    if before != after:
        raise ValueError("audio packets changed during comment rewrite")
    return out


__all__ = [
    "CANONICAL_AI_GENERATED",
    "CANONICAL_SERVICE_PROVIDER",
    "IMPLICIT_AIGC_TAG_KEYS",
    "OPUS_TAGS_SCAN_BYTES",
    "REQUIRED_AIGC_TAG_KEYS",
    "aigc_label_from_mapping",
    "aigc_tag_map",
    "audio_packets_digest",
    "merge_aigc_comments",
    "opus_tag_pairs",
    "pcm_digest",
    "require_aigc_tags",
    "rewrite_opus_comments",
    "title_is_placeholder",
]
