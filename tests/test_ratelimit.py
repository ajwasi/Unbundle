from app.ratelimit import RateLimiter, reset_all


def test_allows_up_to_max_calls_within_period():
    limiter = RateLimiter(max_calls=3, period_seconds=60)
    assert limiter.allow("k", 0.0)
    assert limiter.allow("k", 1.0)
    assert limiter.allow("k", 2.0)


def test_rejects_the_call_over_the_limit():
    limiter = RateLimiter(max_calls=3, period_seconds=60)
    limiter.allow("k", 0.0)
    limiter.allow("k", 1.0)
    limiter.allow("k", 2.0)
    assert not limiter.allow("k", 3.0)


def test_allows_again_once_the_oldest_hit_ages_out_of_the_window():
    limiter = RateLimiter(max_calls=2, period_seconds=60)
    limiter.allow("k", 0.0)
    limiter.allow("k", 1.0)
    assert not limiter.allow("k", 2.0)
    assert limiter.allow("k", 61.0)  # the hit at t=0.0 is now outside the 60s window


def test_different_keys_are_independent():
    limiter = RateLimiter(max_calls=1, period_seconds=60)
    assert limiter.allow("a", 0.0)
    assert limiter.allow("b", 0.0)
    assert not limiter.allow("a", 1.0)


def test_reset_clears_state():
    limiter = RateLimiter(max_calls=1, period_seconds=60)
    limiter.allow("k", 0.0)
    assert not limiter.allow("k", 1.0)
    limiter.reset()
    assert limiter.allow("k", 2.0)


def test_reset_all_clears_every_registered_limiter():
    a = RateLimiter(max_calls=1, period_seconds=60)
    b = RateLimiter(max_calls=1, period_seconds=60)
    a.allow("k", 0.0)
    b.allow("k", 0.0)
    reset_all()
    assert a.allow("k", 1.0)
    assert b.allow("k", 1.0)
