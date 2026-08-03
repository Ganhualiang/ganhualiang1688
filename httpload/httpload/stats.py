"""运行结果模型与统计计算。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RunResult:
    planned: int                 # --requests 指定的计划总数
    concurrency: int             # 实际生效并发数（可能已降级）
    scheduled: int = 0           # 已被 worker 领取的请求数
    completed: int = 0           # 已得到结果（含超时/错误）的请求数
    succeeded: int = 0           # 2xx
    non_2xx: int = 0             # 其他状态码
    errors: int = 0              # 连接类失败
    timeouts: int = 0            # 超过单请求超时
    latencies: list[float] = field(default_factory=list)  # 仅已完成请求的耗时（秒）
    elapsed: float = 0.0
    interrupted: bool = False

    def check_invariants(self) -> None:
        """守恒约束，违反时抛 AssertionError（测试断言用）。"""
        assert self.completed == (
            self.succeeded + self.non_2xx + self.errors + self.timeouts
        ), "completed != succeeded + non_2xx + errors + timeouts"
        assert self.completed <= self.scheduled <= self.planned
        if not self.interrupted:
            assert self.scheduled == self.completed == self.planned

    @property
    def requests_per_sec(self) -> float:
        if self.elapsed <= 0:
            return 0.0
        return self.completed / self.elapsed

    @property
    def avg_latency(self) -> float | None:
        if not self.latencies:
            return None
        return sum(self.latencies) / len(self.latencies)

    @property
    def min_latency(self) -> float | None:
        return min(self.latencies) if self.latencies else None

    @property
    def max_latency(self) -> float | None:
        return max(self.latencies) if self.latencies else None

    def percentile(self, p: float) -> float | None:
        """排序线性插值分位数，p 取 0~100。空数据返回 None。"""
        if not self.latencies:
            return None
        data = sorted(self.latencies)
        if len(data) == 1:
            return data[0]
        rank = (p / 100.0) * (len(data) - 1)
        lo = int(rank)
        hi = min(lo + 1, len(data) - 1)
        frac = rank - lo
        return data[lo] + (data[hi] - data[lo]) * frac
