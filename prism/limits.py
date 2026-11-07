"""Race-safe sliding-window rate limiter (single gateway instance).

check-and-record happens inside one lock, so concurrent requests can never both
see 'room left' and both get in (no read-then-write race).
"""
import threading
import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self, window_s=60.0, clock=time.monotonic):
        self.window_s = window_s
        self.clock = clock
        self._hits = defaultdict(deque)
        self._lock = threading.Lock()

    def try_acquire(self, key, limit):
        """Atomically admit one request if fewer than `limit` happened in the window."""
        now = self.clock()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] >= self.window_s:
                q.popleft()
            if len(q) >= limit:
                retry_after = max(1, int(self.window_s - (now - q[0])) + 1)
                return False, retry_after
            q.append(now)
            return True, 0
