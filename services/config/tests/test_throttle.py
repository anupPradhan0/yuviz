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
