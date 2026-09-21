"""
destinations.bandwidth
=======================

A simple token-bucket rate limiter applied around a destination's
put_object/get_object calls, so a backup running over someone's home
uplink doesn't saturate it. Deliberately implemented at the "whole object
per call" granularity rather than streaming byte-by-byte: our repository
objects are already small (one per 8KB chunk, or one per snapshot/plan
JSON document), so throttling per-call sleeps are fine-grained enough in
practice without needing a custom streaming HTTP/SFTP layer.
"""

from __future__ import annotations

import threading
import time
from typing import Optional


class BandwidthLimiter:
    """Token bucket: ``rate_kbps`` kilobits/sec sustained, with a small
    burst allowance equal to one second's worth of tokens so short bursts
    (e.g. a handful of tiny snapshot-manifest objects) aren't needlessly
    delayed.
    """

    def __init__(self, rate_kbps: Optional[int]):
        self.rate_bytes_per_sec = (rate_kbps * 1024 / 8) if rate_kbps else None
        self._tokens = self.rate_bytes_per_sec or 0.0
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.rate_bytes_per_sec is not None and self.rate_bytes_per_sec > 0

    def throttle(self, num_bytes: int) -> None:
        """Block until ``num_bytes`` worth of tokens are available. No-op
        if no limit was configured.

        Deliberately computes the exact wait time in one shot rather than
        looping with a token cap: capping accumulated tokens at one
        second's worth (the burst allowance) while looping would mean a
        single object *larger* than that cap could never accumulate enough
        tokens to be released -- an infinite loop. Instead, unused burst
        capacity is banked (up to the cap) for small objects, but a single
        large object is allowed to "borrow" whatever wait time it needs.
        """
        if not self.enabled:
            return
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._last_refill = now
            self._tokens = min(
                self.rate_bytes_per_sec,
                self._tokens + elapsed * self.rate_bytes_per_sec,
            )
            if self._tokens >= num_bytes:
                self._tokens -= num_bytes
                return

            deficit = num_bytes - self._tokens
            wait_time = deficit / self.rate_bytes_per_sec
            self._tokens = 0.0

        # Sleep outside the lock so other threads' throttle() calls (for
        # other, possibly smaller, objects) aren't blocked behind this
        # one's wait.
        time.sleep(wait_time)
        with self._lock:
            self._last_refill = time.monotonic()
