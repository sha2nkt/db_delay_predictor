import pytest

from app.bahn_api import CircuitBreaker, UpstreamUnavailable
from tests.conftest import FakeClock


def make(clock, threshold=3, window=60, base=30, cap=300, probes=1):
    return CircuitBreaker(threshold=threshold, window=window, base_cooldown=base,
                          max_cooldown=cap, probes=probes, clock=clock)


def fail_until_open(breaker, n):
    for _ in range(n):
        breaker.record_failure(probe=False)


def test_repeated_failures_open_circuit():
    clock = FakeClock()
    breaker = make(clock)
    fail_until_open(breaker, 2)
    assert breaker.state == "closed"
    breaker.record_failure(probe=False)
    assert breaker.state == "open"


def test_open_circuit_blocks_calls():
    clock = FakeClock()
    breaker = make(clock)
    fail_until_open(breaker, 3)
    with pytest.raises(UpstreamUnavailable) as exc:
        breaker.acquire()
    assert exc.value.retry_after > 0


def test_failures_outside_window_do_not_open():
    clock = FakeClock()
    breaker = make(clock)
    fail_until_open(breaker, 2)
    clock.advance(61)
    breaker.record_failure(probe=False)
    assert breaker.state == "closed"


def test_half_open_probe_success_closes():
    clock = FakeClock()
    breaker = make(clock)
    fail_until_open(breaker, 3)
    # base 30s, doubling x jitter <=1.25 -> open at most 37.5s
    clock.advance(38)
    probe = breaker.acquire()
    assert probe is True
    assert breaker.state == "half-open"
    breaker.record_success(probe)
    assert breaker.state == "closed"
    assert breaker.acquire() is False  # back to normal flow


def test_half_open_probe_failure_reopens_longer():
    clock = FakeClock()
    breaker = make(clock)
    fail_until_open(breaker, 3)
    first_cooldown = breaker._until - clock()
    clock.advance(first_cooldown + 1)
    probe = breaker.acquire()
    breaker.record_failure(probe)
    assert breaker.state == "open"
    assert breaker._until - clock() > first_cooldown  # doubled (minus jitter margin)


def test_half_open_admits_only_configured_probes():
    clock = FakeClock()
    breaker = make(clock, probes=1)
    fail_until_open(breaker, 3)
    clock.advance(38)
    assert breaker.acquire() is True
    with pytest.raises(UpstreamUnavailable):
        breaker.acquire()


def test_release_returns_probe_slot():
    clock = FakeClock()
    breaker = make(clock, probes=1)
    fail_until_open(breaker, 3)
    clock.advance(38)
    probe = breaker.acquire()
    breaker.release(probe)
    assert breaker.acquire() is True  # slot free again


def test_retry_after_floor_opens_immediately_and_is_respected():
    clock = FakeClock()
    breaker = make(clock)
    breaker.record_failure(probe=False, cooldown_floor=120)
    assert breaker.state == "open"
    assert breaker._until - clock() >= 120


def test_retry_after_zero_does_not_force_open():
    clock = FakeClock()
    breaker = make(clock)
    breaker.record_failure(probe=False, cooldown_floor=0)
    assert breaker.state == "closed"  # "retry now" counts toward the threshold only


def test_retry_after_floor_capped_at_max_cooldown():
    clock = FakeClock()
    breaker = make(clock, cap=300)
    breaker.record_failure(probe=False, cooldown_floor=7200)
    assert breaker._until - clock() <= 300 * 1.25


def test_cooldown_capped_after_many_opens():
    clock = FakeClock()
    breaker = make(clock, cap=300)
    for _ in range(8):
        fail_until_open(breaker, 3)
        cooldown = breaker._until - clock()
        assert cooldown <= 300 + 1e-6  # float slack from the _until arithmetic
        clock.advance(cooldown + 1)
        probe = breaker.acquire()
        breaker.record_failure(probe)  # probe fails: reopen, escalating
    assert breaker.state == "open"


def test_success_after_probe_resets_backoff():
    clock = FakeClock()
    breaker = make(clock)
    for _ in range(3):
        fail_until_open(breaker, 3)
        cooldown = breaker._until - clock()
        clock.advance(cooldown + 1)
        probe = breaker.acquire()
        breaker.record_failure(probe)
    cooldown = breaker._until - clock()
    clock.advance(cooldown + 1)
    breaker.record_success(breaker.acquire())
    assert breaker.state == "closed"
    fail_until_open(breaker, 3)
    # streak was reset, so the new cooldown is back near the base
    assert breaker._until - clock() <= 30 * 1.25


def test_force_open_blocks_for_given_cooldown():
    clock = FakeClock()
    breaker = make(clock)
    breaker.force_open(30)
    with pytest.raises(UpstreamUnavailable):
        breaker.acquire()
    clock.advance(31)
    assert breaker.acquire() is True  # half-open probe
