"""命令行入口：参数解析与校验、信号接线、退出码。

退出码约定：0 正常完成、2 参数错误、130 用户中断、1 未预期内部错误。
优雅中断保证范围：macOS / Linux（SIGINT）、Windows（Ctrl+C / Ctrl+Break）。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import functools
import re
import signal
import sys
from collections.abc import Callable
from urllib.parse import urlparse

from httpload.report import render_json, render_text
from httpload.runner import LoadRunner

_DURATION_RE = re.compile(r"^(?P<num>\d+(?:\.\d+)?)(?P<unit>ms|s|m)?$")
_UNIT_SECONDS = {"ms": 0.001, "s": 1.0, "m": 60.0, None: 1.0}

ALLOWED_METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}

EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130

IS_WINDOWS = sys.platform == "win32"

# 信号接线可能失败的情形：平台未实现该 API、非主线程调用、信号不可捕获
_SIGNAL_SETUP_ERRORS = (NotImplementedError, ValueError, RuntimeError, OSError)


def parse_duration(s: str) -> float:
    """解析 Go 风格时长：2s / 500ms / 1m / 1.5s / 纯数字（秒）。返回秒。"""
    m = _DURATION_RE.match(s.strip())
    if not m:
        raise ValueError(f"invalid duration: {s!r}")
    value = float(m.group("num")) * _UNIT_SECONDS[m.group("unit")]
    if value <= 0:
        raise ValueError(f"duration must be > 0: {s!r}")
    return value


def validate_url(url: str) -> str:
    """校验 URL：仅 http/https、hostname 非空、端口合法、不含空白字符。"""
    if any(ch.isspace() for ch in url):
        raise ValueError(f"URL contains whitespace: {url!r}")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"URL scheme must be http or https: {url!r}")
    if not parsed.hostname:
        raise ValueError(f"URL has no hostname: {url!r}")
    try:
        parsed.port  # 非法端口在此抛 ValueError
    except ValueError:
        raise ValueError(f"URL has invalid port: {url!r}") from None
    return url


def parse_header(raw: str) -> tuple[str, str]:
    """解析 'Name: value' 形式的请求头，失败抛 ValueError。"""
    name, sep, value = raw.partition(":")
    if not sep or not name.strip():
        raise ValueError(f"invalid header (expected 'Name: value'): {raw!r}")
    return name.strip(), value.strip()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="httpload", description="命令行 HTTP 接口压测工具"
    )
    parser.add_argument("--url", "-u", required=True, help="被压测的 HTTP 地址")
    parser.add_argument("--requests", "-n", type=int, default=200, help="总请求数")
    parser.add_argument("--concurrency", "-c", type=int, default=10, help="最大并发请求数")
    parser.add_argument("--timeout", "-t", default="2s", help="单个请求超时，如 2s / 500ms")
    parser.add_argument("--method", "-X", default="GET", help="HTTP 方法，默认 GET")
    parser.add_argument("--body", "-d", default=None, help="请求体（字符串）")
    parser.add_argument(
        "--header", "-H", action="append", default=[], metavar="'Name: value'",
        help="自定义请求头，可重复指定",
    )
    parser.add_argument(
        "--progress", action="store_true", help="向 stderr 实时输出进度 completed/planned"
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 输出统计结果")
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析并校验参数。校验失败抛 ValueError（发请求前拒绝）。"""
    args = build_parser().parse_args(argv)
    validate_url(args.url)
    if args.requests <= 0:
        raise ValueError(f"--requests must be > 0, got {args.requests}")
    if args.concurrency <= 0:
        raise ValueError(f"--concurrency must be > 0, got {args.concurrency}")
    args.timeout_seconds = parse_duration(args.timeout)
    args.method = args.method.upper()
    if args.method not in ALLOWED_METHODS:
        raise ValueError(
            f"--method must be one of {sorted(ALLOWED_METHODS)}, got {args.method!r}"
        )
    args.headers = dict(parse_header(h) for h in args.header)
    if args.concurrency > args.requests:
        print(
            f"Note: concurrency reduced to {args.requests} (requests < concurrency)",
            file=sys.stderr,
        )
        args.concurrency = args.requests
    return args


def interrupt_signals() -> tuple[signal.Signals, ...]:
    """按“用户中断”处理的信号。Windows 额外含 SIGBREAK（Ctrl+Break）。"""
    sigs = [signal.SIGINT]
    sigbreak = getattr(signal, "SIGBREAK", None)  # 仅 Windows 存在
    if sigbreak is not None:
        sigs.append(sigbreak)
    return tuple(sigs)


def _install_interrupt(
    loop: asyncio.AbstractEventLoop, on_interrupt: Callable[[], None]
) -> Callable[[], None]:
    """安装中断信号处理器，返回撤销函数。单个信号安装失败只警告并跳过。

    POSIX 用 `loop.add_signal_handler`；Windows 上该 API 未实现（抛
    NotImplementedError），改用 `signal.signal` 把中断转交给事件循环。
    """
    undo: list[Callable[[], None]] = []

    def _win_handler(signum: int, frame: object) -> None:
        # Windows 的 Python 信号处理器在主线程的字节码边界执行，这里不直接碰
        # 事件循环状态，只做一次线程安全的转交（同时唤醒循环）。
        loop.call_soon_threadsafe(on_interrupt)

    for sig in interrupt_signals():
        try:
            if IS_WINDOWS:
                previous = signal.signal(sig, _win_handler)
                undo.append(functools.partial(signal.signal, sig, previous))
            else:
                loop.add_signal_handler(sig, on_interrupt)
                undo.append(functools.partial(loop.remove_signal_handler, sig))
        except _SIGNAL_SETUP_ERRORS as e:
            # 例如非主线程运行，或平台不支持该信号：降级为不可优雅中断，
            # 但绝不因此让整次压测失败。
            print(
                f"httpload: warning: cannot handle {sig.name} gracefully: {e!r}",
                file=sys.stderr,
            )

    def _undo() -> None:
        for fn in undo:
            with contextlib.suppress(*_SIGNAL_SETUP_ERRORS):
                fn()

    return _undo


@contextlib.asynccontextmanager
async def interrupt_guard(on_interrupt: Callable[[], None]):
    """在上下文内把用户中断信号接到 on_interrupt，退出时恢复原处理器。

    Windows 上不需要额外的定时唤醒任务：ProactorEventLoop 构造时已经调用
    `signal.set_wakeup_fd(self._csock.fileno())`（见 asyncio/proactor_events.py），
    信号会写自管道并立即打断 IOCP 等待，处理器随下一次循环迭代执行。
    """
    loop = asyncio.get_running_loop()
    undo = _install_interrupt(loop, on_interrupt)
    try:
        yield
    finally:
        undo()


def harden_stdio() -> None:
    """让无法编码的字符降级为替代字符，而不是抛 UnicodeEncodeError。

    Windows 控制台默认代码页（如英文环境 cp1252）编不出帮助文本里的中文，
    原本会导致 `--help` 直接失败。
    """
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError, OSError):
            stream.reconfigure(errors="replace")


async def _run(args: argparse.Namespace) -> int:
    runner = LoadRunner(
        url=args.url,
        requests=args.requests,
        concurrency=args.concurrency,
        timeout=args.timeout_seconds,
        method=args.method,
        headers=args.headers,
        body=args.body,
        show_progress=args.progress,
    )
    async with interrupt_guard(runner.request_stop):
        result = await runner.run()

    if args.json:
        print(render_json(result, args.url, method=args.method))
    else:
        print(render_text(result, args.url, method=args.method))
    if result.interrupted:
        print("Load test interrupted by user.", file=sys.stderr)
        return EXIT_INTERRUPTED
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    harden_stdio()
    try:
        args = parse_args(argv)
    except ValueError as e:
        print(f"httpload: error: {e}", file=sys.stderr)
        return EXIT_USAGE
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        # 信号处理器未接上时（见 _install_interrupt 的降级分支）的兜底路径
        print("httpload: interrupted before results were collected.", file=sys.stderr)
        return EXIT_INTERRUPTED
    except Exception as e:  # noqa: BLE001 — 顶层兜底
        # 带上异常类型：NotImplementedError 这类空消息异常否则无从定位
        print(f"httpload: unexpected error: {type(e).__name__}: {e}", file=sys.stderr)
        return EXIT_INTERNAL


if __name__ == "__main__":
    sys.exit(main())
