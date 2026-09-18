from openflowbi.jira.budget import Budget, parse_headers


def test_parse_headers_normal():
    status = parse_headers({"X-RateLimit-Remaining": "500"})
    assert status.remaining == 500
    assert status.near_limit is False
    assert status.should_back_off is False


def test_parse_headers_near_limit():
    status = parse_headers({"X-RateLimit-Remaining": "50", "X-RateLimit-NearLimit": "true"})
    assert status.near_limit is True
    assert status.should_back_off is True


def test_parse_headers_exhausted():
    status = parse_headers({"X-RateLimit-Remaining": "0"})
    assert status.should_back_off is True


def test_parse_headers_retry_after_and_reason():
    status = parse_headers({"Retry-After": "30", "RateLimit-Reason": "WRITE_LIMIT_EXCEEDED"})
    assert status.retry_after == 30.0
    assert status.reason == "WRITE_LIMIT_EXCEEDED"


def test_parse_headers_missing_all():
    status = parse_headers({})
    assert status.remaining is None
    assert status.near_limit is False
    assert status.should_back_off is False


def test_budget_observe_tracks_latest_status():
    budget = Budget()
    assert budget.status is None
    status = budget.observe({"X-RateLimit-Remaining": "10"})
    assert budget.status is status


def test_budget_acquire_allows_within_pool():
    budget = Budget(hourly_pool=100)
    assert budget.acquire(weight=1) is True
