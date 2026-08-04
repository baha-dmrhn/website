"""Security helpers shared by the unified Baha Enerji server."""

from __future__ import annotations

import collections
import math
import threading
import time
from typing import Any


class LoginRateLimiter:
    """Limit short bursts of failed login attempts per remote key."""

    def __init__(
        self,
        *,
        max_attempts: int = 5,
        window_seconds: int = 600,
        block_seconds: int = 300,
        clock: Any = time.monotonic,
    ) -> None:
        self.max_attempts = max(1, int(max_attempts))
        self.window_seconds = max(1, int(window_seconds))
        self.block_seconds = max(1, int(block_seconds))
        self._clock = clock
        self._attempts: dict[str, collections.deque[float]] = {}
        self._blocked_until: dict[str, float] = {}
        self._lock = threading.Lock()

    def retry_after(self, key: str) -> int:
        now = self._clock()
        with self._lock:
            blocked_until = self._blocked_until.get(key, 0.0)
            if blocked_until > now:
                return max(1, math.ceil(blocked_until - now))
            if blocked_until:
                self._blocked_until.pop(key, None)
                self._attempts.pop(key, None)
            attempts = self._attempts.get(key)
            if attempts is not None:
                while attempts and now - attempts[0] >= self.window_seconds:
                    attempts.popleft()
                if not attempts:
                    self._attempts.pop(key, None)
            return 0

    def record_failure(self, key: str) -> int:
        now = self._clock()
        with self._lock:
            attempts = self._attempts.setdefault(key, collections.deque())
            while attempts and now - attempts[0] >= self.window_seconds:
                attempts.popleft()
            attempts.append(now)
            if len(attempts) < self.max_attempts:
                return 0
            blocked_until = now + self.block_seconds
            self._blocked_until[key] = blocked_until
            return self.block_seconds

    def reset(self, key: str) -> None:
        with self._lock:
            self._attempts.pop(key, None)
            self._blocked_until.pop(key, None)

