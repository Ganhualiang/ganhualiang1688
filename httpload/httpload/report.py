"""压测结果文本 / JSON 报告，空统计兜底。"""

from __future__ import annotations

import json

from httpload.stats import RunResult


def _fmt_ms(seconds: float | None) -> str:
    if seconds is None:
        return "N/A"
    return f"{seconds * 1000:.2f}ms"


def render_text(result: RunResult, url: str, method: str = "GET") -> str:
    lines = [f"Target:         {url}"]
    if method != "GET":
        lines.append(f"Method:         {method}")
    lines += [
        f"Requests:       {result.planned}",
        f"Concurrency:    {result.concurrency}",
        "",
    ]
    if result.interrupted:
        lines += [
            "Interrupted:    true",
            f"Scheduled:      {result.scheduled}",
        ]
    lines += [
        f"Completed:      {result.completed}",
        f"Succeeded:      {result.succeeded}",
        f"Non-2xx:        {result.non_2xx}",
        f"Errors:         {result.errors}",
        f"Timeouts:       {result.timeouts}",
        "",
        f"Elapsed:        {result.elapsed:.2f}s",
        f"Requests/sec:   {result.requests_per_sec:.2f}",
        f"Avg latency:    {_fmt_ms(result.avg_latency)}",
        f"Min latency:    {_fmt_ms(result.min_latency)}",
        f"Max latency:    {_fmt_ms(result.max_latency)}",
        f"P50 latency:    {_fmt_ms(result.percentile(50))}",
        f"P90 latency:    {_fmt_ms(result.percentile(90))}",
        f"P99 latency:    {_fmt_ms(result.percentile(99))}",
    ]
    return "\n".join(lines)


def render_json(result: RunResult, url: str, method: str = "GET") -> str:
    def _ms(seconds: float | None) -> float | None:
        return None if seconds is None else round(seconds * 1000, 3)

    payload = {
        "target": url,
        "method": method,
        "requests": result.planned,
        "concurrency": result.concurrency,
        "interrupted": result.interrupted,
        "scheduled": result.scheduled,
        "completed": result.completed,
        "succeeded": result.succeeded,
        "non_2xx": result.non_2xx,
        "errors": result.errors,
        "timeouts": result.timeouts,
        "elapsed_sec": round(result.elapsed, 4),
        "requests_per_sec": round(result.requests_per_sec, 2),
        "avg_latency_ms": _ms(result.avg_latency),
        "min_latency_ms": _ms(result.min_latency),
        "max_latency_ms": _ms(result.max_latency),
        "p50_latency_ms": _ms(result.percentile(50)),
        "p90_latency_ms": _ms(result.percentile(90)),
        "p99_latency_ms": _ms(result.percentile(99)),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)
