from app.ratelimit import SlidingWindowLimiter
from tests.conftest import FakeClock


def make(clock, burst=5, burst_window=10, sustained=20, sustained_window=60):
    return SlidingWindowLimiter(burst_limit=burst, burst_window=burst_window,
                                sustained_limit=sustained,
                                sustained_window=sustained_window, clock=clock)


def test_ordinary_use_passes():
    clock = FakeClock()
    limiter = make(clock)
    for _ in range(5):
        assert limiter.retry_after("1.2.3.4") is None
        clock.advance(3)


def test_burst_gets_429_with_retry_after():
    clock = FakeClock()
    limiter = make(clock)
    for _ in range(5):
        assert limiter.retry_after("1.2.3.4") is None
    wait = limiter.retry_after("1.2.3.4")
    assert isinstance(wait, int) and 1 <= wait <= 10


def test_burst_recovers_after_window():
    clock = FakeClock()
    limiter = make(clock)
    for _ in range(5):
        limiter.retry_after("1.2.3.4")
    assert limiter.retry_after("1.2.3.4") is not None
    clock.advance(11)
    assert limiter.retry_after("1.2.3.4") is None


def test_sustained_limit_enforced():
    clock = FakeClock()
    limiter = make(clock)
    # spaced so the burst window never fills, only the sustained one
    for _ in range(20):
        assert limiter.retry_after("1.2.3.4") is None
        clock.advance(2.5)
    wait = limiter.retry_after("1.2.3.4")
    assert isinstance(wait, int) and wait >= 1
    clock.advance(wait + 1)
    assert limiter.retry_after("1.2.3.4") is None


def test_other_clients_unaffected():
    clock = FakeClock()
    limiter = make(clock)
    for _ in range(5):
        limiter.retry_after("attacker")
    assert limiter.retry_after("attacker") is not None
    assert limiter.retry_after("5.6.7.8") is None


def test_rejected_attempts_are_not_counted():
    clock = FakeClock()
    limiter = make(clock)
    for _ in range(5):
        limiter.retry_after("1.2.3.4")
    for _ in range(50):  # hammering while limited must not extend the penalty
        limiter.retry_after("1.2.3.4")
    clock.advance(11)
    assert limiter.retry_after("1.2.3.4") is None


def test_client_count_stays_bounded():
    clock = FakeClock()
    limiter = SlidingWindowLimiter(5, 10, 20, 60, max_clients=100, clock=clock)
    for i in range(500):
        limiter.retry_after(f"10.0.0.{i}")
    assert len(limiter._hits) <= 100
