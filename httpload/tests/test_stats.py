"""stats 单元测试：percentile 边界、守恒公式、空统计。"""

import pytest

from httpload.stats import RunResult


def test_percentile_single_value():
    r = RunResult(planned=1, concurrency=1, latencies=[0.5])
    assert r.percentile(50) == 0.5
    assert r.percentile(99) == 0.5


def test_percentile_interpolation():
    r = RunResult(planned=5, concurrency=1, latencies=[0.1, 0.2, 0.3, 0.4, 0.5])
    assert r.percentile(0) == pytest.approx(0.1)
    assert r.percentile(50) == pytest.approx(0.3)
    assert r.percentile(100) == pytest.approx(0.5)
    assert r.percentile(25) == pytest.approx(0.2)


def test_empty_latencies_return_none():
    r = RunResult(planned=10, concurrency=2)
    assert r.avg_latency is None
    assert r.min_latency is None
    assert r.max_latency is None
    assert r.percentile(50) is None
    assert r.requests_per_sec == 0.0


def test_invariants_normal_completion():
    r = RunResult(
        planned=10, concurrency=2, scheduled=10, completed=10,
        succeeded=7, non_2xx=1, errors=1, timeouts=1,
    )
    r.check_invariants()


def test_invariants_interrupted():
    r = RunResult(
        planned=100, concurrency=5, scheduled=40, completed=38,
        succeeded=30, non_2xx=3, errors=2, timeouts=3, interrupted=True,
    )
    r.check_invariants()


def test_invariants_violation_detected():
    r = RunResult(
        planned=10, concurrency=2, scheduled=10, completed=10,
        succeeded=5, non_2xx=1, errors=1, timeouts=1,
    )
    with pytest.raises(AssertionError):
        r.check_invariants()
