"""Stable prefix tracker for FunASR interim results (ch.12.5)."""

from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass, field

_INCOMPLETE_TAIL = re.compile(
    r"(?:"
    r"\d{1,}$|"
    r"\d{1,}\.\d*$|"
    r"\d{4}[-/年]\d{0,2}$|"
    r"1[3-9]\d{0,9}$"
    r")$"
)

_CJK = re.compile(r"[\u4e00-\u9fff]")


def normalize_interim(text: str) -> str:
    return re.sub(r"[ \t]+", " ", text).strip()


def longest_common_prefix(texts: list[str]) -> str:
    if not texts:
        return ""
    prefix = texts[0]
    for t in texts[1:]:
        i = 0
        limit = min(len(prefix), len(t))
        while i < limit and prefix[i] == t[i]:
            i += 1
        prefix = prefix[:i]
        if not prefix:
            break
    return prefix


def snap_to_boundary(text: str) -> str:
    """Retreat LCP left to last complete CJK char, English word, or punctuation."""
    if not text:
        return ""
    if text[-1].isascii() and text[-1].isalnum():
        # If trailing ASCII word looks complete (followed by nothing more), keep it
        # only when entire text ends at a natural point — for partial English word
        # at end of LCP, drop the partial word.
        i = len(text) - 1
        while i >= 0 and text[i].isascii() and text[i].isalnum():
            i -= 1
        # Keep if we have at least one complete char before
        if i >= 0:
            return text[: i + 1]
        return text
    return text


def count_cjk(text: str) -> int:
    return len(_CJK.findall(text))


def has_complete_english_word_delta(prev: str, new: str) -> bool:
    if not new.startswith(prev):
        return False
    delta = new[len(prev) :]
    return bool(re.search(r"[A-Za-z]{2,}", delta))


@dataclass
class StablePrefixTracker:
    sentence_id: int | None = None
    recent: deque[str] = field(default_factory=lambda: deque(maxlen=3))
    last_published: str = ""
    candidate: str = ""
    candidate_since_ns: int | None = None
    stability_ms: int = 250

    def reset(self, sentence_id: int | None = None) -> None:
        self.sentence_id = sentence_id
        self.recent.clear()
        self.last_published = ""
        self.candidate = ""
        self.candidate_since_ns = None

    def observe(self, sentence_id: int, text: str, *, now_ns: int | None = None) -> str | None:
        """Return a newly publishable stable prefix, or None."""
        now = now_ns if now_ns is not None else time.monotonic_ns()
        if self.sentence_id != sentence_id:
            self.reset(sentence_id)

        norm = normalize_interim(text)
        if not norm:
            return None
        self.recent.append(norm)
        if len(self.recent) < 3:
            return None

        raw_lcp = longest_common_prefix(list(self.recent))
        lcp = snap_to_boundary(raw_lcp)
        if not lcp:
            self.candidate = ""
            self.candidate_since_ns = None
            return None

        # If a candidate was already tracking a shorter prefix that remains valid
        # under the new LCP, keep timing on that candidate until published or invalidated.
        if self.candidate and lcp.startswith(self.candidate) and self.candidate != self.last_published:
            lcp_for_publish = self.candidate
        else:
            lcp_for_publish = lcp

        if lcp_for_publish == self.last_published:
            # Try to advance to longer LCP when available.
            if lcp != self.last_published and lcp.startswith(self.last_published):
                lcp_for_publish = lcp
            else:
                return None
        if self.last_published and not lcp_for_publish.startswith(self.last_published):
            self.candidate = ""
            self.candidate_since_ns = None
            return None

        if _INCOMPLETE_TAIL.search(lcp_for_publish):
            return None

        delta = lcp_for_publish[len(self.last_published) :]
        cjk_gain = count_cjk(delta)
        eng_ok = has_complete_english_word_delta(self.last_published, lcp_for_publish)
        if cjk_gain < 2 and not eng_ok:
            return None

        if self.candidate != lcp_for_publish:
            # Prefer establishing the current LCP as candidate when previous was empty.
            self.candidate = lcp_for_publish
            self.candidate_since_ns = now
            return None

        assert self.candidate_since_ns is not None
        elapsed_ms = (now - self.candidate_since_ns) / 1_000_000
        if elapsed_ms < self.stability_ms:
            return None

        # Confirm candidate still prefix of latest interim (not rewritten away).
        if not norm.startswith(self.candidate):
            self.candidate = ""
            self.candidate_since_ns = None
            return None

        published = self.candidate
        self.last_published = published
        self.candidate = ""
        self.candidate_since_ns = None
        return published

    def on_final(self, sentence_id: int) -> None:
        if self.sentence_id == sentence_id:
            self.reset(None)
