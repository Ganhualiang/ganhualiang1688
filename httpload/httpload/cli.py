"""命令行入口：参数解析与校验、信号接线、退出码。

退出码约定：0 正常完成、2 参数错误、130 用户中断、1 未预期内部错误。
优雅中断（SIGINT）保证范围：macOS / Linux。
"""

from __future__ import annotations

import argparse
import asyncio
import re
import signal
import sys
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
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, runner.request_stop)
    try:
        result = await runner.run()
    finally:
        loop.remove_signal_handler(signal.SIGINT)

    if args.json:
        print(render_json(result, args.url, method=args.method))
    else:
        print(render_text(result, args.url, method=args.method))
    if result.interrupted:
        print("Load test interrupted by user.", file=sys.stderr)
        return EXIT_INTERRUPTED
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
    except ValueError as e:
        print(f"httpload: error: {e}", file=sys.stderr)
        return EXIT_USAGE
    try:
        return asyncio.run(_run(args))
    except Exception as e:  # noqa: BLE001 — 顶层兜底
        print(f"httpload: unexpected error: {e}", file=sys.stderr)
        return EXIT_INTERNAL


if __name__ == "__main__":
    sys.exit(main())
