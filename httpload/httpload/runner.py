"""LoadRunner：worker pool 并发引擎、请求执行与分类、优雅停止。"""

from __future__ import annotations

import asyncio
import sys
import time

import aiohttp

from httpload.stats import RunResult

_PROGRESS_INTERVAL = 0.2  # 实时进度刷新间隔（秒）


class LoadRunner:
    def __init__(
        self,
        url: str,
        requests: int,
        concurrency: int,
        timeout: float,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        body: str | None = None,
        show_progress: bool = False,
    ):
        self.url = url
        self.planned = requests
        # 并发数大于请求数时降级为请求数（调用方负责提示）
        self.concurrency = min(concurrency, requests)
        self.timeout = timeout
        self.method = method
        self.headers = headers or {}
        self.body = body
        self.show_progress = show_progress
        self._stop_event = asyncio.Event()
        self._workers: list[asyncio.Task] = []
        self._next_index = 0

    def request_stop(self) -> None:
        """停止调度新请求并取消在途请求。可从信号处理器调用，幂等。"""
        self._stop_event.set()
        for w in self._workers:
            w.cancel()

    async def run(self) -> RunResult:
        result = RunResult(planned=self.planned, concurrency=self.concurrency)
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        connector = aiohttp.TCPConnector(
            limit=self.concurrency, limit_per_host=self.concurrency
        )
        start = time.perf_counter()
        session = aiohttp.ClientSession(
            timeout=timeout, connector=connector, headers=self.headers
        )
        progress_task: asyncio.Task | None = None
        try:
            self._workers = [
                asyncio.create_task(self._worker(session, result))
                for _ in range(self.concurrency)
            ]
            if self.show_progress:
                progress_task = asyncio.create_task(self._report_progress(result))
            await asyncio.gather(*self._workers, return_exceptions=True)
        finally:
            if progress_task is not None:
                progress_task.cancel()
                try:
                    await progress_task
                except asyncio.CancelledError:
                    pass
                # 收尾输出最终进度并换行，避免 \r 覆盖后续 stderr 输出
                print(
                    f"\rprogress: {result.completed}/{self.planned}",
                    file=sys.stderr, flush=True,
                )
            await session.close()
        result.elapsed = time.perf_counter() - start
        result.interrupted = self._stop_event.is_set()
        return result

    async def _report_progress(self, result: RunResult) -> None:
        """实时进度：定期向 stderr 输出 completed/planned（\\r 原地刷新）。"""
        while True:
            print(
                f"\rprogress: {result.completed}/{self.planned}",
                end="", file=sys.stderr, flush=True,
            )
            await asyncio.sleep(_PROGRESS_INTERVAL)

    async def _worker(self, session: aiohttp.ClientSession, result: RunResult) -> None:
        while not self._stop_event.is_set():
            # 领取序号到递增之间无 await，asyncio 单线程下无需加锁
            if self._next_index >= self.planned:
                return
            self._next_index += 1
            result.scheduled += 1

            t0 = time.perf_counter()
            try:
                async with session.request(
                    self.method, self.url, data=self.body
                ) as resp:
                    # 分块排空正文，不在内存中保留完整响应
                    async for _ in resp.content.iter_chunked(65536):
                        pass
                    status = resp.status
            except asyncio.CancelledError:
                # 被取消的在途请求不计入 completed 与延迟
                raise
            except asyncio.TimeoutError:
                result.timeouts += 1
            except (aiohttp.ClientError, OSError):
                result.errors += 1
            else:
                if 200 <= status < 300:
                    result.succeeded += 1
                else:
                    result.non_2xx += 1
            result.completed += 1
            result.latencies.append(time.perf_counter() - t0)
