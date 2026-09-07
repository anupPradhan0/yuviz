"""
FixedWindowCounter — the in-process rate-limit primitive behind
InviteThrottle/AcceptThrottle (app.py). AcceptThrottle in particular keys on
the client IP of two *public, unauthenticated* routes (GET/POST
/invites/accept), so an unbounded map would be one permanent dict entry per
distinct source address that ever hit them — this file is about proving
that doesn't happen, not about the rate-limit math itself (covered by
test_invite_routes.py's throttle tests).
"""

from __future__ import annotations

from unittest.mock import patch

from services.config.app import FixedWindowCounter


def test_stale_entries_are_evicted_not_retained_forever():
    counter = FixedWindowCounter(limit=100, window_seconds=1)

    with patch("services.config.app.time.monotonic", return_value=0.0):
        counter.increment("ip-1")
        assert len(counter._buckets) == 1

    # Past ip-1's 1s window. A fresh key from a different source shouldn't
    # just add a second entry alongside ip-1's stale one — ip-1's window has
    # expired, so nothing will ever consult it again; it must be swept.
    with patch("services.config.app.time.monotonic", return_value=5.0):
        counter.increment("ip-2")
        assert "ip-1" not in counter._buckets
        assert len(counter._buckets) == 1


def test_map_size_does_not_grow_with_a_stream_of_expired_one_off_keys():
    # The shape an unbounded map actually fails on: many distinct source
    # IPs, each seen once, each long past its window by the time the next
    # one arrives. Would fail (len growing unboundedly) without eviction.
    counter = FixedWindowCounter(limit=10, window_seconds=1)

    for i in range(500):
        with patch("services.config.app.time.monotonic", return_value=float(i * 2)):
            counter.increment(f"ip-{i}")
        # Each call is 2s after the last — always past the 1s window — so
        # at most the just-inserted key should remain.
        assert len(counter._buckets) == 1


def test_map_is_bounded_when_many_distinct_keys_arrive_within_one_window():
    # The shape the two tests above can never produce (lesson 12): all
    # keys arrive at the *same* instant, so they're all within the window
    # at once and eviction has nothing stale to sweep — this is
    # AcceptThrottle's `hour` counter (window_seconds=3600) under a flood of
    # distinct source IPs within that hour. Review finding 2 on the first
    # version of this test: looping only up to _MAX_BUCKETS can never
    # exceed the cap even with the cap deleted — nothing was ever asked to
    # overshoot it. This loops _MAX_BUCKETS + 5,000 times, so a version
    # with the cap (or the `_at_capacity_for_new_key` guards) removed would
    # land at 25,000 entries, not 20,000. Asserting `==`, not `<=`, is what
    # actually pins the bound rather than merely permitting it.
    counter = FixedWindowCounter(limit=10, window_seconds=3600)

    with patch("services.config.app.time.monotonic", return_value=0.0):
        for i in range(FixedWindowCounter._MAX_BUCKETS + 5_000):
            counter.increment(f"ip-{i}")

    assert len(counter._buckets) == FixedWindowCounter._MAX_BUCKETS


def test_limiter_recovers_after_a_flood_once_the_window_passes():
    # Review finding 1 (round 1): the first version of the capacity fix
    # returned from over_limit/increment *before* ever calling
    # _maybe_sweep, and _maybe_sweep was only reachable from inside
    # _current — which that early return skipped. Once the map hit
    # _MAX_BUCKETS during a flood, nothing could ever evict the
    # (now-stale) entries afterward, so every new source IP was refused
    # forever rather than just until the window passed.
    #
    # Round 2 finding: an earlier version of *this test* lowered
    # _SWEEP_INTERVAL to 1 to force the periodic sweep deterministically —
    # which quietly reconfigured the counter into a state production never
    # runs in (the real value is 500) and would have stayed green even
    # after the fix regressed to "recovers, but only ~500 accesses late."
    # This version uses the real _SWEEP_INTERVAL and asserts recovery on
    # the very *first* access after the flood ends, which only the
    # capacity path's own forced sweep (_refuse_new_key) can produce — the
    # periodic one can't have fired yet (accesses_since_sweep is nowhere
    # near 500). Would fail (over stays True) either under the original
    # capacity-starves-sweep bug or under a fix that only re-adds the
    # periodic sweep without forcing one on a capacity refusal.
    counter = FixedWindowCounter(limit=10, window_seconds=60)

    with patch("services.config.app.time.monotonic", return_value=0.0):
        for i in range(FixedWindowCounter._MAX_BUCKETS):
            counter.increment(f"ip-{i}")
        # Flood is still ongoing (same instant): a brand-new source is
        # correctly refused — the map is genuinely full of live entries.
        over, _ = counter.over_limit("brand-new-ip-during-flood")
    assert over is True
    assert len(counter._buckets) == FixedWindowCounter._MAX_BUCKETS

    # Quiet period: the 60s window has fully passed and no more traffic
    # arrives. Every flood entry is now stale. The very first request from
    # a brand-new IP must succeed immediately, not ~500 accesses later.
    with patch("services.config.app.time.monotonic", return_value=61.0):
        over, _ = counter.over_limit("very-first-new-ip-after-quiet-period")
        counter.increment("very-first-new-ip-after-quiet-period")
    assert over is False
    assert len(counter._buckets) == 1
    assert "very-first-new-ip-after-quiet-period" in counter._buckets


def test_new_key_is_rejected_once_map_is_at_capacity():
    # Fails closed: once the cap is reached, a source never seen before is
    # treated as already over limit rather than silently admitted and
    # growing the map past its cap.
    counter = FixedWindowCounter(limit=1000, window_seconds=3600)
    counter._buckets = {f"ip-{i}": (0.0, 0) for i in range(FixedWindowCounter._MAX_BUCKETS)}

    with patch("services.config.app.time.monotonic", return_value=0.0):
        over, retry_after = counter.over_limit("brand-new-ip")
        counter.increment("brand-new-ip")

    assert over is True
    assert retry_after > 0
    assert "brand-new-ip" not in counter._buckets
    assert len(counter._buckets) == FixedWindowCounter._MAX_BUCKETS
