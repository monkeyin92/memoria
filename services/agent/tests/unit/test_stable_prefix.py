from __future__ import annotations

from services.agent.src.orchestration.stable_prefix import (
    StablePrefixTracker,
    longest_common_prefix,
    snap_to_boundary,
)


def test_lcp_and_boundary() -> None:
    assert longest_common_prefix(["我想定", "我想订下", "我想订下周"]) == "我想"
    assert snap_to_boundary("我想订") == "我想订"


def test_stable_prefix_publishes_after_stability() -> None:
    tr = StablePrefixTracker(stability_ms=250)
    t0 = 0
    # Build LCP "我想" from three interims; must not publish wrong "我想定"
    assert tr.observe(1, "我想定", now_ns=t0) is None
    assert tr.observe(1, "我想订下", now_ns=t0 + 10_000_000) is None
    # Third sample → candidate "我想" (LCP), not yet stable
    r = tr.observe(1, "我想订下周", now_ns=t0 + 20_000_000)
    assert r is None
    # Same LCP after 250ms → publish
    r = tr.observe(1, "我想订下周", now_ns=t0 + 300_000_000)
    assert r is not None
    assert "想定" not in r
    assert r.startswith("我想")


def test_does_not_publish_wrong_rewrite() -> None:
    tr = StablePrefixTracker(stability_ms=0)
    t0 = 0
    tr.observe(1, "我想定", now_ns=t0)
    tr.observe(1, "我想订下", now_ns=t0 + 1)
    # With stability_ms=0, still need candidate then second observe of same LCP
    r1 = tr.observe(1, "我想订下周", now_ns=t0 + 2)
    r2 = tr.observe(1, "我想订下周", now_ns=t0 + 3)
    published = r1 or r2
    assert published is not None
    assert published == "我想" or published.startswith("我想")
    assert "想定" not in published


def test_final_clears() -> None:
    tr = StablePrefixTracker()
    tr.observe(1, "你好啊", now_ns=0)
    tr.on_final(1)
    assert tr.sentence_id is None
