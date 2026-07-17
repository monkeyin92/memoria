"""Chinese streaming spoken-phrase segmenter (ch.14)."""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field

from services.agent.src.contracts.ids import GenerationFence

STRONG_BOUNDARY = set("。！？；\n")
WEAK_BOUNDARY = set("，、：")
HARD_LIMIT = 42
FIRST_TARGET_MIN = 6
WEAK_SUBMIT_MIN = 12
WEAK_SUBMIT_MAX = 28
FIRST_WEAK_MIN = 6  # first segment may cut earlier for lower TTFB
FIRST_WAIT_MS = 180
FIRST_HARD_WAIT_MS = 320

_MD_STRIP = re.compile(
    r"(```[\s\S]*?```)|(^#{1,6}\s+)|(^[\*\-\+]\s+)|(\[([^\]]+)\]\([^)]+\))|(`+)",
    re.MULTILINE,
)
_PROTECTED = re.compile(
    r"("
    r"https?://\S+|www\.\S+|"
    r"\b[\w.+-]+@[\w.-]+\.\w+\b|"
    r"¥?\d+(?:,\d{3})*(?:\.\d+)?|"
    r"\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?|"
    r"v?\d+\.\d+(?:\.\d+)?|"
    r"\d+\.\d+"
    r")"
)


def normalize_for_tts(text: str) -> tuple[str, str]:
    """Return (tts_text, canonical_text). Does not alter amounts/dates/order ids."""
    canonical = text
    t = _MD_STRIP.sub(lambda m: m.group(5) if m.group(5) else "", text)
    t = re.sub(r"\n{2,}", "。", t)
    t = re.sub(r"\n", "。", t)
    t = re.sub(
        r"[\U0001F300-\U0001FAFF\U00002700-\U000027BF]+",
        "",
        t,
    )
    t = re.sub(r"[ \t]{2,}", " ", t).strip()
    return t, canonical


def _is_protected_interior(text: str, index: int) -> bool:
    for m in _PROTECTED.finditer(text):
        if m.start() < index < m.end():
            return True
    return False


def _unbalanced_quotes_or_brackets(text: str) -> bool:
    pairs = [
        ("「", "」"),
        ("『", "』"),
        ("“", "”"),
        ("(", ")"),
        ("（", "）"),
        ("[", "]"),
        ("【", "】"),
    ]
    for left, right in pairs:
        if text.count(left) != text.count(right):
            return True
    if text.count('"') % 2 == 1:
        return True
    return False


def cjk_len(text: str) -> int:
    return sum(
        1
        for ch in text
        if "\u4e00" <= ch <= "\u9fff" or (ch.isalnum() and not ch.isspace())
    )


@dataclass(frozen=True, slots=True)
class PhraseSegment:
    segment_id: str
    text: str
    fence: GenerationFence
    index: int


@dataclass
class PhraseSegmenter:
    fence: GenerationFence
    buffer: str = ""
    segments: list[PhraseSegment] = field(default_factory=list)
    first_token_ns: int | None = None
    stream_ended: bool = False
    _index: int = 0

    def reset(self, fence: GenerationFence) -> None:
        self.fence = fence
        self.buffer = ""
        self.segments.clear()
        self.first_token_ns = None
        self.stream_ended = False
        self._index = 0

    def push_token(self, token: str, *, now_ns: int | None = None) -> list[PhraseSegment]:
        if not token:
            return []
        now = now_ns if now_ns is not None else time.monotonic_ns()
        if self.first_token_ns is None:
            self.first_token_ns = now
        tts_part, _ = normalize_for_tts(token)
        self.buffer += tts_part
        return self._drain(now_ns=now, force=False)

    def flush(self, *, end_of_stream: bool = False) -> list[PhraseSegment]:
        self.stream_ended = end_of_stream or self.stream_ended
        return self._drain(now_ns=time.monotonic_ns(), force=end_of_stream)

    def _drain(self, *, now_ns: int, force: bool) -> list[PhraseSegment]:
        out: list[PhraseSegment] = []
        while True:
            cut = self._find_cut(force=force, now_ns=now_ns)
            if cut is None:
                break
            piece = self.buffer[: cut + 1]
            self.buffer = self.buffer[cut + 1 :]
            if piece.strip():
                out.append(self._emit(piece))
        if force and self.buffer.strip():
            out.append(self._emit(self.buffer))
            self.buffer = ""
        return out

    def _weak_min(self) -> int:
        return FIRST_WEAK_MIN if self._index == 0 else WEAK_SUBMIT_MIN

    def _find_cut(self, *, force: bool, now_ns: int) -> int | None:
        text = self.buffer
        if not text:
            return None

        length = cjk_len(text)
        weak_min = self._weak_min()

        # Left-to-right: earliest strong cut, or weak cut when length band matches.
        for i, ch in enumerate(text):
            if _is_protected_interior(text, i):
                continue
            prefix = text[: i + 1]
            if not prefix.strip():
                continue
            if ch in STRONG_BOUNDARY:
                return i
            if ch in WEAK_BOUNDARY:
                if _unbalanced_quotes_or_brackets(prefix):
                    continue
                ln = cjk_len(prefix)
                if weak_min <= ln <= WEAK_SUBMIT_MAX:
                    # Keep short trailing clauses with the same phrase
                    # e.g. "订单号是…，请确认。" stays one segment.
                    rest = text[i + 1 :]
                    if rest:
                        # remainder until next strong boundary
                        end = len(rest)
                        for j, rch in enumerate(rest):
                            if rch in STRONG_BOUNDARY:
                                end = j + 1
                                break
                        if cjk_len(rest[:end]) <= 4:
                            continue
                    return i

        # Hard limit
        if length >= HARD_LIMIT:
            weak = self._last_weak_in_band(text, min_len=self._weak_min(), max_len=HARD_LIMIT)
            if weak is not None:
                return weak
            return self._last_word_boundary(text)

        if force:
            return len(text) - 1

        # First-segment timing rules
        if self._index == 0 and self.first_token_ns is not None:
            waited_ms = (now_ns - self.first_token_ns) / 1_000_000
            if waited_ms >= FIRST_WAIT_MS and length >= FIRST_TARGET_MIN:
                weak = self._last_weak_in_band(text, min_len=FIRST_WEAK_MIN, max_len=WEAK_SUBMIT_MAX)
                if weak is not None:
                    return weak
            if waited_ms >= FIRST_HARD_WAIT_MS and length >= FIRST_TARGET_MIN:
                weak = self._last_weak_in_band(text, min_len=1, max_len=HARD_LIMIT)
                if weak is not None:
                    return weak
                return len(text) - 1
        return None

    def _last_weak_in_band(self, text: str, *, min_len: int, max_len: int) -> int | None:
        found: int | None = None
        for i, ch in enumerate(text):
            if ch in WEAK_BOUNDARY and not _is_protected_interior(text, i):
                prefix = text[: i + 1]
                if _unbalanced_quotes_or_brackets(prefix):
                    continue
                ln = cjk_len(prefix)
                if min_len <= ln <= max_len:
                    found = i
        return found

    def _last_word_boundary(self, text: str) -> int:
        for i in range(len(text) - 1, 0, -1):
            if text[i - 1].isspace() or "\u4e00" <= text[i - 1] <= "\u9fff":
                return i - 1 if text[i - 1].isspace() else i
        return len(text) - 1

    def _emit(self, text: str) -> PhraseSegment:
        seg = PhraseSegment(
            segment_id=str(uuid.uuid4()),
            text=text,
            fence=self.fence,
            index=self._index,
        )
        self._index += 1
        self.segments.append(seg)
        return seg


def segment_all(text: str, fence: GenerationFence | None = None) -> list[str]:
    """Deterministic offline segmentation used by unit tests."""
    f = fence or GenerationFence(session_id="test", turn_id=1, generation_id=1, tool_epoch=0)
    seg = PhraseSegmenter(fence=f)
    # Simulate streaming enough history that timing does not block cuts.
    seg.first_token_ns = time.monotonic_ns() - 1_000_000_000
    segs = seg.push_token(text)
    segs.extend(seg.flush(end_of_stream=True))
    return [s.text for s in segs if s.text.strip()]
