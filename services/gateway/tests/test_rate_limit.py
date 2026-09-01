import pytest

from middleware.rate_limit import check_rate_limit, reset_rate_limits


@pytest.fixture(autouse=True)
def _reset():
    reset_rate_limits()
    yield
    reset_rate_limits()


class TestRateLimit:
    def test_requests_within_limit_are_allowed(self, monkeypatch):
        monkeypatch.setenv("GATEWAY_RATE_LIMIT_PER_MINUTE", "5")
        for _ in range(5):
            assert check_rate_limit("tenant-a") is True

    def test_request_over_limit_is_rejected(self, monkeypatch):
        monkeypatch.setenv("GATEWAY_RATE_LIMIT_PER_MINUTE", "3")
        assert check_rate_limit("tenant-a") is True
        assert check_rate_limit("tenant-a") is True
        assert check_rate_limit("tenant-a") is True
        assert check_rate_limit("tenant-a") is False

    def test_tenants_are_isolated(self, monkeypatch):
        """One tenant exhausting its limit must never affect another tenant."""
        monkeypatch.setenv("GATEWAY_RATE_LIMIT_PER_MINUTE", "2")
        assert check_rate_limit("tenant-a") is True
        assert check_rate_limit("tenant-a") is True
        assert check_rate_limit("tenant-a") is False

        # tenant-b is unaffected by tenant-a's exhausted limit
        assert check_rate_limit("tenant-b") is True
        assert check_rate_limit("tenant-b") is True
        assert check_rate_limit("tenant-b") is False

    def test_rejected_attempt_is_not_recorded(self, monkeypatch):
        """A rejected request should not itself count further against the window."""
        monkeypatch.setenv("GATEWAY_RATE_LIMIT_PER_MINUTE", "1")
        assert check_rate_limit("tenant-a") is True
        assert check_rate_limit("tenant-a") is False
        assert check_rate_limit("tenant-a") is False

    def test_sliding_window_frees_up_after_expiry(self, monkeypatch):
        import middleware.rate_limit as rl

        monkeypatch.setenv("GATEWAY_RATE_LIMIT_PER_MINUTE", "1")
        assert check_rate_limit("tenant-a") is True
        assert check_rate_limit("tenant-a") is False

        # Simulate the recorded request having happened over a minute ago by
        # rewinding the stored timestamp rather than sleeping in a test.
        with rl._lock:
            log = rl._request_log["tenant-a"]
            log[0] -= (rl._WINDOW_SECONDS + 1)

        assert check_rate_limit("tenant-a") is True

    def test_default_limit_used_for_invalid_env_value(self, monkeypatch):
        monkeypatch.setenv("GATEWAY_RATE_LIMIT_PER_MINUTE", "not-a-number")
        # Should not raise, and should fall back to the documented default (60)
        for _ in range(10):
            assert check_rate_limit("tenant-c") is True
