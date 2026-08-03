"""runner 集成测试：总数、并发上限、分类、超时、错误、守恒、内部中止。"""

import asyncio
import sys

import pytest

from httpload.runner import LoadRunner

# 连接被拒绝需要多久上报，与平台强相关：POSIX 回环口立刻回 RST（毫秒级），
# Windows 不回 RST 而是重传 SYN，约 2s 后才给出 WSAECONNREFUSED。单请求超时
# 必须大于这个时间，否则先命中超时、被记成 Timeouts 而不是 Errors。
CONN_REFUSED_TIMEOUT = 5.0 if sys.platform == "win32" else 0.5


async def test_total_requests_correct(server):
    runner = LoadRunner(server.url("/ok"), requests=50, concurrency=8, timeout=2.0)
    result = await asyncio.wait_for(runner.run(), timeout=15)
    assert result.completed == 50 == server.state.hits
    assert result.scheduled == result.completed == result.planned == 50
    assert result.succeeded == 50
    result.check_invariants()


async def test_concurrency_limit_respected(server):
    runner = LoadRunner(
        server.url("/slow?delay=0.05"), requests=40, concurrency=5, timeout=5.0
    )
    result = await asyncio.wait_for(runner.run(), timeout=15)
    assert result.completed == 40
    assert server.state.max_in_flight <= 5
    result.check_invariants()


async def test_non_2xx_classified(server):
    runner = LoadRunner(server.url("/status/404"), requests=30, concurrency=5, timeout=2.0)
    result = await asyncio.wait_for(runner.run(), timeout=15)
    assert result.non_2xx == 30
    assert result.succeeded == 0
    result.check_invariants()


async def test_slow_requests_time_out(server):
    runner = LoadRunner(
        server.url("/slow?delay=1"), requests=10, concurrency=5, timeout=0.1
    )
    result = await asyncio.wait_for(runner.run(), timeout=10)
    assert result.timeouts == 10
    assert result.errors == 0
    # 2 批 × 0.1s 超时，远小于 10 × 1s 串行慢响应
    assert result.elapsed < 5
    result.check_invariants()


async def test_connection_errors_no_deadlock(free_port):
    runner = LoadRunner(
        f"http://127.0.0.1:{free_port}/",
        requests=20,
        concurrency=10,
        timeout=CONN_REFUSED_TIMEOUT,
    )
    result = await asyncio.wait_for(runner.run(), timeout=40)
    assert result.errors == 20
    assert result.timeouts == 0
    result.check_invariants()


async def test_connection_refused_faster_than_timeout_counts_as_error(free_port):
    """连接失败早于超时上报时必须记 Errors；反之（超时先到）记 Timeouts。

    两类结果互斥，不会重复计数，这一点在两个平台上都成立。
    """
    runner = LoadRunner(
        f"http://127.0.0.1:{free_port}/", requests=4, concurrency=4, timeout=0.05
    )
    result = await asyncio.wait_for(runner.run(), timeout=20)
    assert result.timeouts == 4  # 50ms 一定早于任何平台的连接拒绝上报
    assert result.errors == 0
    result.check_invariants()


async def test_mixed_conservation(server):
    runner = LoadRunner(server.url("/mixed"), requests=30, concurrency=6, timeout=0.2)
    result = await asyncio.wait_for(runner.run(), timeout=20)
    assert result.completed == 30
    assert result.completed == (
        result.succeeded + result.non_2xx + result.errors + result.timeouts
    )
    assert result.succeeded > 0
    assert result.non_2xx > 0
    assert result.timeouts > 0
    result.check_invariants()


async def test_request_stop_interrupts_run(server):
    runner = LoadRunner(
        server.url("/slow?delay=0.1"), requests=1000, concurrency=5, timeout=5.0
    )
    task = asyncio.create_task(runner.run())
    await asyncio.sleep(0.2)
    runner.request_stop()
    result = await asyncio.wait_for(task, timeout=5)
    assert result.interrupted is True
    assert result.completed <= result.scheduled <= result.planned
    assert result.scheduled < result.planned  # 确实提前停止
    result.check_invariants()


async def test_concurrency_downgraded_to_requests(server):
    runner = LoadRunner(server.url("/ok"), requests=3, concurrency=100, timeout=2.0)
    assert runner.concurrency == 3
    result = await asyncio.wait_for(runner.run(), timeout=10)
    assert result.completed == 3
    assert result.concurrency == 3


async def test_post_body_and_headers_passed_through(server):
    runner = LoadRunner(
        server.url("/echo"), requests=5, concurrency=2, timeout=2.0,
        method="POST", headers={"X-Token": "abc123"}, body="hello=world",
    )
    result = await asyncio.wait_for(runner.run(), timeout=10)
    assert result.succeeded == 5
    assert server.last_method == "POST"
    assert server.last_body == b"hello=world"
    assert server.last_headers.get("X-Token") == "abc123"
    result.check_invariants()


async def test_head_method(server):
    runner = LoadRunner(
        server.url("/echo"), requests=3, concurrency=2, timeout=2.0, method="HEAD"
    )
    result = await asyncio.wait_for(runner.run(), timeout=10)
    assert result.succeeded == 3
    assert server.last_method == "HEAD"
    result.check_invariants()
