from dataclasses import dataclass

from pyrate_limiter import Duration, Limiter, Rate

CLOUD_HOURLY_POOL = 65_000


@dataclass(frozen=True)
class RateLimitStatus:
    remaining: int | None
    near_limit: bool
    reason: str | None
    retry_after: float | None

    @property
    def should_back_off(self) -> bool:
        if self.near_limit:
            return True
        return self.remaining is not None and self.remaining <= 0


def parse_headers(headers: dict[str, str]) -> RateLimitStatus:
    """Parse Jira Cloud's rate-limit budget headers.

    WRITE: pyrate-limiter (and every other Python rate limiter) enforces a
    client-chosen rate; none of them consume a server-reported budget. Jira
    Cloud reports its own remaining budget instead — this is the feedback loop
    that no library provides (CLAUDE.md "Jira API facts" / rate limiting).
    """
    remaining_raw = headers.get("X-RateLimit-Remaining")
    retry_after_raw = headers.get("Retry-After")
    return RateLimitStatus(
        remaining=int(remaining_raw) if remaining_raw is not None else None,
        near_limit=headers.get("X-RateLimit-NearLimit", "").lower() == "true",
        reason=headers.get("RateLimit-Reason"),
        retry_after=float(retry_after_raw) if retry_after_raw is not None else None,
    )


class Budget:
    """Local leaky-bucket (pyrate-limiter) as a floor, plus Jira's reported budget on top.

    The bucket alone would happily keep sending requests up to the site-wide
    65,000/hour pool even as Jira signals NearLimit — `observe()` is what
    makes callers back off before that happens.
    """

    def __init__(self, hourly_pool: int = CLOUD_HOURLY_POOL) -> None:
        self._limiter = Limiter(Rate(hourly_pool, Duration.HOUR))
        self.status: RateLimitStatus | None = None

    def acquire(self, weight: int = 1) -> bool:
        result = self._limiter.try_acquire("jira", weight=weight)
        return bool(result)

    def observe(self, headers: dict[str, str]) -> RateLimitStatus:
        self.status = parse_headers(headers)
        return self.status
