"""Session-scoped target-speaker enrollment and verification (numpy-only).

This is a lightweight spectral embedding (log-mel mean/std), not a commercial
voiceprint product. It is good enough to reject a clearly different nearby
talker on the same device while remaining dependency-free for production.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum

import numpy as np

_EPS = 1e-8


class SpeakerGateState(StrEnum):
    DISABLED = "disabled"
    PENDING = "pending"
    ENROLLED = "enrolled"
    OPEN = "open"  # fail-open after timeout/failure


@dataclass(frozen=True)
class SpeakerScore:
    score: float
    accepted: bool
    reason: str
    speech_ms: int
    # Mean RMS of voiced frames (near-field owner is louder than tablet/TV bleed).
    rms: float = 0.0
    # Fraction of frames above energy floor in [0, 1] (media often near-continuous).
    duty: float = 0.0
    # Voiced ms above near-field threshold vs quieter ambient/media ms.
    near_ms: int = 0
    far_ms: int = 0


def _pcm16le_to_float(pcm: bytes) -> np.ndarray:
    if not pcm or len(pcm) < 2:
        return np.zeros(0, dtype=np.float32)
    if len(pcm) % 2:
        pcm = pcm[:-1]
    return np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0


def _hz_to_mel(hz: float) -> float:
    return float(2595.0 * math.log10(1.0 + hz / 700.0))


def _mel_to_hz(mel: float) -> float:
    return float(700.0 * (10.0 ** (mel / 2595.0) - 1.0))


def _mel_filterbank(
    *,
    n_fft: int,
    n_mels: int,
    sample_rate: int,
    fmin: float = 80.0,
    fmax: float | None = None,
) -> np.ndarray:
    if fmax is None:
        fmax = sample_rate / 2.0
    n_freqs = n_fft // 2 + 1
    mels = np.linspace(_hz_to_mel(fmin), _hz_to_mel(fmax), n_mels + 2, dtype=np.float64)
    hz = np.array([_mel_to_hz(float(m)) for m in mels], dtype=np.float64)
    bins = np.floor((n_fft + 1) * hz / sample_rate).astype(np.int32)
    bins = np.clip(bins, 0, n_freqs - 1)
    fb = np.zeros((n_mels, n_freqs), dtype=np.float64)
    for i in range(n_mels):
        left, center, right = int(bins[i]), int(bins[i + 1]), int(bins[i + 2])
        if center <= left:
            center = left + 1
        if right <= center:
            right = min(n_freqs - 1, center + 1)
        for j in range(left, center):
            fb[i, j] = (j - left) / max(1, center - left)
        for j in range(center, right):
            fb[i, j] = (right - j) / max(1, right - center)
    return fb.astype(np.float32)


def speech_ms_from_pcm(
    pcm: bytes,
    *,
    sample_rate: int = 16000,
    energy_threshold: float = 0.012,
    frame_ms: int = 20,
) -> int:
    """Count milliseconds of frames above a simple energy floor."""
    stats = voiced_stats_from_pcm(
        pcm,
        sample_rate=sample_rate,
        energy_threshold=energy_threshold,
        frame_ms=frame_ms,
    )
    return int(stats["speech_ms"])


def voiced_stats_from_pcm(
    pcm: bytes,
    *,
    sample_rate: int = 16000,
    energy_threshold: float = 0.012,
    frame_ms: int = 20,
    near_rms_threshold: float | None = None,
) -> dict[str, float]:
    """Voiced-frame speech_ms, mean RMS, duty, and near/far split for media pollution."""
    x = _pcm16le_to_float(pcm)
    if x.size == 0:
        return {
            "speech_ms": 0.0,
            "rms": 0.0,
            "duty": 0.0,
            "near_ms": 0.0,
            "far_ms": 0.0,
        }
    frame = max(1, int(sample_rate * frame_ms / 1000))
    hop = frame
    near_thr = (
        float(near_rms_threshold)
        if near_rms_threshold is not None and near_rms_threshold > 0
        else max(energy_threshold * 3.0, 0.04)
    )
    voiced = 0
    near = 0
    far = 0
    rms_sum = 0.0
    total = 0
    for start in range(0, x.size - frame + 1, hop):
        chunk = x[start : start + frame]
        rms = float(np.sqrt(np.mean(chunk * chunk) + _EPS))
        total += 1
        if rms >= energy_threshold:
            voiced += 1
            rms_sum += rms
            if rms >= near_thr:
                near += 1
            else:
                far += 1
    if total == 0:
        return {
            "speech_ms": 0.0,
            "rms": 0.0,
            "duty": 0.0,
            "near_ms": 0.0,
            "far_ms": 0.0,
        }
    return {
        "speech_ms": float(voiced * frame_ms),
        "rms": (rms_sum / voiced) if voiced else 0.0,
        "duty": float(voiced) / float(total),
        "near_ms": float(near * frame_ms),
        "far_ms": float(far * frame_ms),
    }


def embed_pcm(
    pcm: bytes,
    *,
    sample_rate: int = 16000,
    n_mels: int = 40,
    n_fft: int = 512,
    hop_ms: int = 10,
    win_ms: int = 25,
    min_speech_ms: int = 600,
    energy_threshold: float = 0.012,
) -> np.ndarray | None:
    """Build an L2-normalized log-mel mean+std embedding, or None if too short/quiet.

    Only energy-gated (voiced) frames contribute, so quiet tablet bleed mixed into
    a pause does not pull the embedding toward ambient media.
    """
    speech_ms = speech_ms_from_pcm(pcm, sample_rate=sample_rate)
    if speech_ms < min_speech_ms:
        return None
    x = _pcm16le_to_float(pcm)
    if x.size < n_fft:
        return None
    hop = max(1, int(sample_rate * hop_ms / 1000))
    win = max(n_fft, int(sample_rate * win_ms / 1000))
    window = np.hanning(win).astype(np.float32)
    fb = _mel_filterbank(n_fft=n_fft, n_mels=n_mels, sample_rate=sample_rate)
    frames: list[np.ndarray] = []
    for start in range(0, x.size - win + 1, hop):
        raw = x[start : start + win]
        rms = float(np.sqrt(np.mean(raw * raw) + _EPS))
        if rms < energy_threshold:
            continue
        frame = raw * window
        # real FFT power spectrum
        spec = np.fft.rfft(frame, n=n_fft)
        power = (spec.real * spec.real + spec.imag * spec.imag).astype(np.float32)
        mel = fb @ power[: fb.shape[1]]
        frames.append(np.log(mel + 1e-6))
    if len(frames) < 8:
        return None
    mat = np.stack(frames, axis=0)
    mean = mat.mean(axis=0)
    std = mat.std(axis=0)
    # Light delta (first half vs second) adds discrimination vs steady media.
    mid = mat.shape[0] // 2
    delta = mat[mid:].mean(axis=0) - mat[:mid].mean(axis=0)
    vec = np.concatenate([mean, std, delta]).astype(np.float32)
    norm = float(np.linalg.norm(vec) + _EPS)
    return vec / norm


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    if a.size == 0 or b.size == 0 or a.shape != b.shape:
        return 0.0
    return float(np.dot(a, b) / (float(np.linalg.norm(a) * np.linalg.norm(b)) + _EPS))


@dataclass
class SpeakerVerifier:
    """Collect enrollment audio, then score later utterances."""

    enabled: bool = True
    sample_rate: int = 16000
    enroll_speech_ms: int = 3500
    enroll_timeout_ms: int = 15000
    min_verify_speech_ms: int = 450
    # Dual-window + soft margin in duplex_runtime; base thr ~0.58.
    accept_threshold: float = 0.58
    # Reject when utterance voiced RMS is far below enroll near-field level.
    # 0.28: soft owner after loud enroll stays in; quiet tablet (~0.2×) still out.
    far_field_rms_ratio: float = 0.28
    # Quiet frames relative to enroll RMS count as far-field media pollution.
    near_field_rms_ratio: float = 0.45
    rolling_ms: int = 4000
    state: SpeakerGateState = SpeakerGateState.DISABLED
    owner_embedding: np.ndarray | None = None
    owner_ref_rms: float = 0.0
    _enroll_pcm: bytearray = field(default_factory=bytearray)
    _enroll_speech_ms: int = 0
    _enroll_elapsed_ms: int = 0
    _rolling: bytearray = field(default_factory=bytearray)
    _utterance: bytearray = field(default_factory=bytearray)
    _collecting_utterance: bool = False

    def __post_init__(self) -> None:
        if not self.enabled:
            self.state = SpeakerGateState.DISABLED
        else:
            self.state = SpeakerGateState.PENDING

    @property
    def is_enrolled(self) -> bool:
        return self.state is SpeakerGateState.ENROLLED and self.owner_embedding is not None

    @property
    def active(self) -> bool:
        return self.state is SpeakerGateState.ENROLLED

    def begin_enrollment(self) -> None:
        if not self.enabled:
            self.state = SpeakerGateState.DISABLED
            return
        self.state = SpeakerGateState.PENDING
        self.owner_embedding = None
        self.owner_ref_rms = 0.0
        self._enroll_pcm.clear()
        self._enroll_speech_ms = 0
        self._enroll_elapsed_ms = 0

    def feed_pcm(self, pcm: bytes) -> None:
        if not pcm:
            return
        max_roll = self.sample_rate * 2 * self.rolling_ms // 1000
        self._rolling.extend(pcm)
        if len(self._rolling) > max_roll:
            del self._rolling[: len(self._rolling) - max_roll]
        if self._collecting_utterance:
            self._utterance.extend(pcm)
            # Cap utterance buffer at 8s.
            max_utt = self.sample_rate * 2 * 8
            if len(self._utterance) > max_utt:
                del self._utterance[: len(self._utterance) - max_utt]
        if self.state is not SpeakerGateState.PENDING:
            return
        self._enroll_pcm.extend(pcm)
        chunk_ms = max(1, (len(pcm) // 2) * 1000 // self.sample_rate)
        self._enroll_elapsed_ms += chunk_ms
        self._enroll_speech_ms = speech_ms_from_pcm(
            bytes(self._enroll_pcm),
            sample_rate=self.sample_rate,
        )

    def enrollment_progress(self) -> dict[str, int | str]:
        return {
            "state": self.state.value,
            "speech_ms": self._enroll_speech_ms,
            "target_ms": self.enroll_speech_ms,
            "elapsed_ms": self._enroll_elapsed_ms,
            "timeout_ms": self.enroll_timeout_ms,
        }

    def try_finalize_enrollment(
        self,
        *,
        force: bool = False,
        wall_elapsed_ms: int | None = None,
    ) -> SpeakerScore | None:
        """Return a score when enrollment succeeds, fails-open, or still pending.

        ``force=True`` always leaves PENDING (used after wall-clock deadline).
        ``wall_elapsed_ms`` counts real time even if no PCM was observed — without
        it, zero-PCM enroll never advances ``_enroll_elapsed_ms`` and never times out.
        """
        if self.state is not SpeakerGateState.PENDING:
            return None
        effective_elapsed = self._enroll_elapsed_ms
        if wall_elapsed_ms is not None:
            effective_elapsed = max(effective_elapsed, wall_elapsed_ms)
        if self._enroll_speech_ms >= self.enroll_speech_ms:
            emb = embed_pcm(
                bytes(self._enroll_pcm),
                sample_rate=self.sample_rate,
                min_speech_ms=max(800, self.enroll_speech_ms // 2),
            )
            if emb is None:
                if force or effective_elapsed >= self.enroll_timeout_ms:
                    return self._fail_open("enroll_embedding_failed")
                return None
            self.owner_embedding = emb
            enroll_stats = voiced_stats_from_pcm(
                bytes(self._enroll_pcm), sample_rate=self.sample_rate
            )
            self.owner_ref_rms = float(enroll_stats["rms"])
            self.state = SpeakerGateState.ENROLLED
            self._enroll_pcm.clear()
            return SpeakerScore(
                score=1.0,
                accepted=True,
                reason="enrolled",
                speech_ms=self._enroll_speech_ms,
                rms=self.owner_ref_rms,
                duty=float(enroll_stats["duty"]),
            )
        if force or effective_elapsed >= self.enroll_timeout_ms:
            return self._fail_open("enroll_timeout")
        return None

    def _fail_open(self, reason: str) -> SpeakerScore:
        self.state = SpeakerGateState.OPEN
        self.owner_embedding = None
        self.owner_ref_rms = 0.0
        speech_ms = self._enroll_speech_ms
        self._enroll_pcm.clear()
        return SpeakerScore(score=0.0, accepted=True, reason=reason, speech_ms=speech_ms)

    def mark_utterance_start(self) -> None:
        self._collecting_utterance = True
        self._utterance.clear()

    def mark_utterance_end(self) -> None:
        self._collecting_utterance = False

    def _near_rms_threshold(self) -> float:
        if self.owner_ref_rms > 0.0:
            return max(0.02, self.owner_ref_rms * self.near_field_rms_ratio)
        return 0.04

    def is_far_field(self, score: SpeakerScore) -> bool:
        """True when audio is much quieter than enrollment (tablet/TV across room).

        Never far-field-reject a strong voiceprint match: owner often speaks softer
        after a loud enroll phrase (prod: score 0.70 rejected as far_field right
        after enroll, first real turn silent).
        """
        if self.owner_ref_rms <= 0.0 or score.rms <= 0.0:
            return False
        # High cosine match ⇒ near-mic owner even if quieter than enroll peak.
        if score.score >= max(0.55, self.accept_threshold - 0.05):
            return False
        return score.rms < self.owner_ref_rms * self.far_field_rms_ratio

    def looks_like_continuous_media(self, score: SpeakerScore) -> bool:
        """High duty + long voiced span ≈ steady tablet/TV rather than turn-taking speech."""
        return score.speech_ms >= 2800 and score.duty >= 0.78

    def is_media_polluted(self, score: SpeakerScore) -> bool:
        """Long quiet media plus a short near-field blip (e.g. video + 「等一下」).

        Prod smoking gun: ASR committed「悟空…欢迎来到我的世界…等一下」because the
        short owner phrase pulled the mean score above thr while most ms was tablet.
        """
        far_ms = int(score.far_ms)
        near_ms = int(score.near_ms)
        if far_ms < 1500:
            return False
        return far_ms >= max(1200, int(near_ms * 1.5))

    def score_pcm(self, pcm: bytes | None = None) -> SpeakerScore:
        if self.state is SpeakerGateState.DISABLED:
            return SpeakerScore(1.0, True, "disabled", 0)
        if self.state is SpeakerGateState.OPEN:
            return SpeakerScore(1.0, True, "open", 0)
        if self.state is SpeakerGateState.PENDING:
            # During enrollment, never reject the owner's registration speech.
            return SpeakerScore(1.0, True, "pending_enroll", 0)
        if self.owner_embedding is None:
            return SpeakerScore(1.0, True, "missing_owner", 0)
        payload = pcm if pcm is not None else bytes(self._utterance or self._rolling)
        stats = voiced_stats_from_pcm(
            payload,
            sample_rate=self.sample_rate,
            near_rms_threshold=self._near_rms_threshold(),
        )
        speech_ms = int(stats["speech_ms"])
        rms = float(stats["rms"])
        duty = float(stats["duty"])
        near_ms = int(stats["near_ms"])
        far_ms = int(stats["far_ms"])
        if speech_ms < self.min_verify_speech_ms:
            # Too short to score: fail closed only for long barge-ins later.
            return SpeakerScore(
                0.0,
                False,
                "too_short",
                speech_ms,
                rms=rms,
                duty=duty,
                near_ms=near_ms,
                far_ms=far_ms,
            )
        emb = embed_pcm(
            payload,
            sample_rate=self.sample_rate,
            min_speech_ms=self.min_verify_speech_ms,
            energy_threshold=self._near_rms_threshold(),
        )
        if emb is None:
            # No near-field content long enough → treat as far-field ambient.
            return SpeakerScore(
                0.0,
                False,
                "far_field",
                speech_ms,
                rms=rms,
                duty=duty,
                near_ms=near_ms,
                far_ms=far_ms,
            )
        score = cosine_similarity(self.owner_embedding, emb)
        accepted = score >= self.accept_threshold
        return SpeakerScore(
            score=score,
            accepted=accepted,
            reason="match" if accepted else "mismatch",
            speech_ms=speech_ms,
            rms=rms,
            duty=duty,
            near_ms=near_ms,
            far_ms=far_ms,
        )

    def score_latest_utterance(self) -> SpeakerScore:
        return self.score_pcm(bytes(self._utterance) if self._utterance else None)
