"""测试夹具：本地 aiohttp.web server + 动态空闲端口。"""

from __future__ import annotations

import asyncio
import socket
from dataclasses import dataclass, field

import pytest
from aiohttp import web


@dataclass
class ServerState:
    hits: int = 0
    in_flight: int = 0
    max_in_flight: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class LocalServer:
    """本地测试 HTTP server，记录命中次数与在途请求峰值。

    路由：
      /ok                 -> 200
      /status/{code}      -> 指定状态码
      /slow?delay=秒       -> 延迟后 200
      /mixed              -> 按请求序号轮换 200 / 404 / 慢 200
      /echo（任意方法）    -> 200，并记录最近一次请求的 method/body/headers
    """

    def __init__(self) -> None:
        self.state = ServerState()
        self.port: int = 0
        self._runner: web.AppRunner | None = None
        self.last_method: str | None = None
        self.last_body: bytes | None = None
        self.last_headers: dict[str, str] = {}

    def url(self, path: str = "/ok") -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    async def _track(self, handler):
        async with self.state.lock:
            self.state.hits += 1
            seq = self.state.hits
            self.state.in_flight += 1
            self.state.max_in_flight = max(
                self.state.max_in_flight, self.state.in_flight
            )
        try:
            return await handler(seq)
        finally:
            async with self.state.lock:
                self.state.in_flight -= 1

    async def _ok(self, request: web.Request) -> web.Response:
        return await self._track(lambda seq: _resp(200))

    async def _status(self, request: web.Request) -> web.Response:
        code = int(request.match_info["code"])
        return await self._track(lambda seq: _resp(code))

    async def _slow(self, request: web.Request) -> web.Response:
        delay = float(request.query.get("delay", "1"))

        async def handler(seq: int) -> web.Response:
            await asyncio.sleep(delay)
            return web.Response(status=200, text="slow")

        return await self._track(handler)

    async def _mixed(self, request: web.Request) -> web.Response:
        async def handler(seq: int) -> web.Response:
            mode = seq % 3
            if mode == 0:
                return web.Response(status=404, text="not found")
            if mode == 1:
                await asyncio.sleep(0.5)  # 配合 timeout<0.5s 触发超时
                return web.Response(status=200, text="slow")
            return web.Response(status=200, text="ok")

        return await self._track(handler)

    async def _echo(self, request: web.Request) -> web.Response:
        self.last_method = request.method
        self.last_body = await request.read()
        self.last_headers = dict(request.headers)
        return await self._track(lambda seq: _resp(200))

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/ok", self._ok)
        app.router.add_get("/status/{code}", self._status)
        app.router.add_get("/slow", self._slow)
        app.router.add_get("/mixed", self._mixed)
        app.router.add_route("*", "/echo", self._echo)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()


async def _resp(code: int) -> web.Response:
    return web.Response(status=code, text=str(code))


@pytest.fixture
async def server():
    srv = LocalServer()
    await srv.start()
    yield srv
    await srv.stop()


@pytest.fixture
def free_port() -> int:
    """动态获取一个当前未被监听的本地端口。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
