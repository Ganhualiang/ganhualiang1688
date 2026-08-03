# httpload — 命令行 HTTP 压测工具（Python asyncio 实现方案）

## 修改记录

**修改时间：2026-08-03 21:10**（依据 Codex 审查结论 [plan review.md](plan%20review.md) 修订，v2）

| # | 修改项 | 对应审查意见 |
|---|---|---|
| 1 | 统计模型从 `Stats` 扩展为完整 `RunResult`（含 planned/concurrency/scheduled/completed/四类计数/latencies/elapsed/interrupted），并写明正常与中断两种守恒约束 | 必改 1 |
| 2 | 重写 Ctrl+C 控制流：由 `LoadRunner.request_stop()` 统一收尾（stop event → cancel workers → gather 清理 → 关 session/进度任务/移除 handler → 输出部分统计 → 退出码 130）；删除"二次 Ctrl+C 强杀"设计；退出码约定 0/1/2/130；Windows 信号不做回退实现，改为 README 声明保证范围为 macOS/Linux | 必改 2 |
| 3 | 新增零完成请求兜底：延迟列表为空时文本报告 Avg/Min/Max/Pxx 输出 `N/A`、JSON 输出 `null`、RPS 输出 `0` | 必改 3 |
| 4 | 连接池由 `TCPConnector(limit=0)` 改为 `limit=actual_concurrency, limit_per_host=actual_concurrency` 显式上限 | 必改 4 |
| 5 | 新增两个端到端测试：`python -m httpload` 子进程 CLI 测试（报告/退出码/守恒）、POSIX 真实 SIGINT 中断测试（限时退出、返回码 130、`Interrupted: true`） | 必改 5 |
| 6 | 测试稳定性修正：网络错误测试动态获取未监听端口（不再硬编码 65530）；混合分类改为单一路由按序号轮换 200/404/慢响应；测试 server `in_flight -= 1` 放入 `finally`；所有超时/中断测试加外层时限 | 必改 6 |
| 7 | URL 校验加强：仅 http/https、hostname 非空、主动读取 port 并捕获非法端口异常、拒绝含空白字符的 URL；校验失败在发请求前以退出码 2 拒绝 | 必改 7 |
| 8 | 依赖统一到 `pyproject.toml`（Python 版本、aiohttp、console_script 入口、test extras），README 主推 `pip install -e .`，删除独立 `requirements.txt` | 必改 8 |
| 9 | 功能范围重排为两阶段：第一阶段 30 分钟内交付全部必做项；第二阶段按优先级补 P50/P90/P99 → JSON 输出 → 更多测试。**POST/自定义 Header/body/实时进度本次不实现**，在 README 未完成清单中说明后续方案 | 范围调整 |
| 10 | 实现步骤按 Codex 推荐的 10 步顺序重排，验收以其"最终验收标准"为准 | 执行顺序 |

---

## Context（背景与目标）

面试题要求实现一个类似 `hey`/`ab` 迷你版的命令行 HTTP 压测工具：向指定 URL 发起固定总数的并发 GET 请求，输出统计结果。题目原文见 [实习生能力考核题目.md](实习生能力考核题目.md)。当前目录为空白项目，从零搭建。

技术选型：**Python 3.10+ / asyncio / aiohttp**。

**时间策略（硬约束）**：实际条件为 1 小时硬性截止，题目建议 30 分钟。采用"先在 30 分钟内完成必做项，剩余时间补测试和加分项"的策略，附加功能不得影响核心功能交付。

## 一、题目拆解（考点 → 技术方案映射）

| 考点 | 题目要求 | 本方案对应手段 |
|---|---|---|
| 并发控制 | 任意时刻在途请求 ≤ concurrency；禁止无界任务；结束后无遗留 | **固定 worker pool**：只创建 `min(concurrency, requests)` 个 worker 协程，从共享计数器领取任务序号。天然有界，不预建 N 个 task |
| HTTP 资源管理 | 复用连接、正确关闭响应体、不长期持有正文 | 全程复用一个 `aiohttp.ClientSession`，连接池设显式上限 `TCPConnector(limit=actual_concurrency, limit_per_host=actual_concurrency)`；`async with session.get()` 保证释放；响应体用 `iter_chunked` 分块读完即弃 |
| 超时与取消 | 单请求超时；Ctrl+C 后停止调度、取消在途、不挂起、输出部分统计 | `aiohttp.ClientTimeout(total=t)` 做单请求超时；SIGINT → `LoadRunner.request_stop()` 统一收尾（见 Step 5），退出码 130 |
| 统计正确性 | Completed = Succeeded + Non-2xx + Errors + Timeouts | 四类互斥分类 + 完整 `RunResult` 模型，正常/中断两种守恒约束均有测试断言 |
| 参数校验 | URL 缺失/非法、requests≤0、concurrency≤0、timeout 非法 | argparse + 强化校验（见 Step 2）；timeout 支持 Go 风格 `2s`/`500ms`/`1m`；**concurrency > requests 时自动降为 requests 并向 stderr 打印提示**（题目允许，README 声明） |
| 测试习惯 | ≥2 个自动化测试，不依赖公网 | pytest + pytest-asyncio：runner 层直调测试 + CLI 子进程端到端测试 + 真实 SIGINT 测试 |

**关键设计决策（需在 README 中声明）：**
- 延迟统计口径：对所有"已完成"请求记录耗时（含超时/错误的实际耗时）；被取消的在途请求不计入 completed 与延迟。
- 并发 > 请求数：降级而非报错，启动时向 stderr 打印 `Note: concurrency reduced to N`。
- 提示/告警信息写 **stderr**，统计报告写 stdout（为后续 `--json` 管道消费预留）。
- 退出码约定：`0` 正常完成、`2` 参数错误、`130` 用户中断、`1` 未预期内部错误。
- 优雅中断保证范围：**macOS/Linux**（`loop.add_signal_handler`）；不声称未验证的 Windows 回退方案，README 注明平台限制。

## 二、运行结果模型（RunResult）

```python
@dataclass
class RunResult:
    planned: int            # --requests 指定的计划总数
    concurrency: int        # 实际生效并发数（可能已降级）
    scheduled: int = 0      # 已被 worker 领取的请求数
    completed: int = 0      # 已得到结果（含超时/错误）的请求数
    succeeded: int = 0      # 2xx
    non_2xx: int = 0        # 其他状态码
    errors: int = 0         # 连接类失败
    timeouts: int = 0       # 超过单请求超时
    latencies: list[float]  # 仅已完成请求的耗时
    elapsed: float = 0.0
    interrupted: bool = False
```

**守恒约束（测试断言）：**

- 正常完成：`scheduled == completed == planned` 且 `completed == succeeded + non_2xx + errors + timeouts`
- 中断时：`completed <= scheduled <= planned` 且 `completed == succeeded + non_2xx + errors + timeouts`
- 已领取但被取消的在途请求计入 `scheduled`，**不计入** `completed` 和延迟统计。

**零完成兜底**：`latencies` 为空时，文本报告 Avg/Min/Max/Pxx 输出 `N/A`，JSON 对应字段为 `null`，Requests/sec 输出 `0`——报告层不得触发 `min()/max()` 空序列异常。

## 三、项目结构

```
1688实习/httpload/
├── httpload/
│   ├── __init__.py
│   ├── __main__.py      # python -m httpload 入口
│   ├── cli.py           # argparse、duration 解析、参数校验、信号接线、退出码
│   ├── runner.py        # LoadRunner：worker pool、请求执行与分类、request_stop()
│   ├── stats.py         # RunResult、守恒检查、百分位计算
│   └── report.py        # 文本报告（+二阶段 JSON），空统计兜底
├── tests/
│   ├── conftest.py      # 本地 aiohttp.web server fixture + 动态空闲端口 fixture
│   ├── test_cli.py      # duration/URL 校验单元测试 + CLI 子进程端到端 + SIGINT 测试
│   ├── test_runner.py   # 并发上限、分类、超时、错误、守恒、request_stop 取消
│   └── test_stats.py    # 百分位、守恒公式、空统计
├── README.md            # 安装/运行/测试说明 + 设计决策 + 平台限制 + 已完成/未完成清单
└── pyproject.toml       # requires-python、aiohttp 依赖、httpload 入口、test extras
```

依赖统一在 `pyproject.toml` 声明（不再维护 `requirements.txt`）：
- `requires-python = ">=3.10"`
- `dependencies = ["aiohttp>=3.9"]`
- `[project.scripts] httpload = "httpload.cli:main"`
- `[project.optional-dependencies] test = ["pytest", "pytest-asyncio"]`

README 主要安装方式：`pip install -e ".[test]"`。

## 四、实现步骤（按 Codex 推荐顺序）

### 第一阶段 — 必做项（目标 30 分钟内完成）

**Step 1 — 最小项目骨架**：目录结构、`pyproject.toml`（含命令入口）、`__main__.py`。

**Step 2 — cli.py：参数解析与强化校验**
- argparse：`--url/-u`（必填）、`--requests/-n`、`--concurrency/-c`、`--timeout/-t`。
- `parse_duration(s)`：支持 `2s`/`500ms`/`1m`/`1.5s`/纯数字（秒），返回 float 秒；非法或 ≤0 抛 `ValueError`。
- URL 校验（全部在发请求前完成，失败退出码 2）：
  - scheme 仅允许 `http`/`https`；
  - `hostname` 非空；
  - 主动读取 `.port` 并捕获非法端口的 `ValueError`；
  - 拒绝包含空白字符的 URL。
- requests > 0、concurrency > 0；concurrency > requests → 降为 requests 并向 stderr 打印 `Note: concurrency reduced to N`。

**Step 3 — stats.py：RunResult 与守恒**：按第二节模型实现；`percentile(p)` 排序线性插值；空 latencies 各派生指标返回 `None` 由报告层渲染为 `N/A`。

**Step 4 — runner.py：worker pool 并发引擎**
```
class LoadRunner:
    async def run(self) -> RunResult:
        # 1) 单个 ClientSession(timeout=ClientTimeout(total=t),
        #        connector=TCPConnector(limit=k, limit_per_host=k))  # k = 实际并发
        # 2) 启动 k = min(concurrency, requests) 个 worker task
        # 3) worker 循环：stop_event 已置位或 next_index >= planned → 退出；
        #    领取序号（同步递增，无 await 间隙）→ scheduled += 1；
        #    t0 = perf_counter(); 发请求并分类；completed += 1 并记录 latency
        # 4) await asyncio.gather(*workers, return_exceptions=True)
        # 5) finally: await session.close()
```
- 单请求：`async with session.get(url) as resp:`，`async for _ in resp.content.iter_chunked(65536): pass` 排空正文。
- 分类顺序（互斥，先后有讲究）：`asyncio.CancelledError` → 重新抛出（不计 completed）；`asyncio.TimeoutError` → timeout；`aiohttp.ClientError | OSError` → error；否则按状态码 2xx / Non-2xx。超时与普通错误不得重复计数。

**Step 5 — 优雅退出：`request_stop()` 统一控制流**
1. `cli.main()` 中 `loop.add_signal_handler(signal.SIGINT, runner.request_stop)`。
2. `request_stop()`：置 stop event（worker 不再领取新请求）→ `cancel()` 所有 worker（在途 HTTP 请求尽快结束）。
3. `run()` 内 `await asyncio.gather(*workers, return_exceptions=True)` 等待取消清理完成。
4. finally 中关闭 `ClientSession`、取消并等待辅助任务、由 main 移除 signal handler。
5. 输出部分统计，`RunResult.interrupted = True`，报告含 `Interrupted: true` 与 `Scheduled`。
6. 进程退出码 `130`。
- 不做二次 Ctrl+C 强杀设计——第一次中断必须自身收尾干净。
- Windows：不实现回退，README 声明优雅中断保证范围为 macOS/Linux。

**Step 6 — report.py：文本报告**：对齐题目示例（Target/Requests/Concurrency、五个计数、Elapsed/RPS/Avg/Min/Max）；中断时追加 `Interrupted: true` + `Scheduled`；空统计按第二节兜底渲染。

**Step 7 — 核心自动化测试**（conftest：`aiohttp.web` server fixture，路由 `/ok`、`/status/404`、`/slow?delay=...`、`/mixed`（按请求序号稳定轮换 200/404/慢响应）；`hits` 计数与 `in_flight` 峰值统计，且 **`in_flight -= 1` 必须在 `finally` 中**；另提供"动态获取未监听本地端口"的 fixture：bind 到端口 0 取得端口号后关闭 socket）
1. **总数正确**：n=50 → `completed == 50 == server.hits`，且 `scheduled == completed == planned`。
2. **并发上限**：n=40, c=5, 每请求 delay 50ms → `in_flight` 峰值 ≤ 5。
3. **分类正确**：打 `/status/404` → `non_2xx == n`，`succeeded == 0`。
4. **超时**：timeout=100ms 打 `/slow?delay=1` → `timeouts == n`，整体耗时远小于 n×1s。
5. **网络错误不死锁**：打动态获取的空闲端口 → `errors == n`，外层 `asyncio.wait_for(..., 10)` 内正常返回。
6. **守恒**：打 `/mixed` 后断言 `completed == succeeded + non_2xx + errors + timeouts`。
7. **内部中止**：长压测 200ms 后调用 `request_stop()` → `run()` 限时返回，`interrupted == True`，`completed <= scheduled <= planned`，守恒成立。
8. **单元**：`parse_duration`（合法/非法/≤0）、URL 校验各分支、percentile 边界、空统计报告。
9. **CLI 端到端**：`subprocess` 运行 `python -m httpload` 打本地 server，校验 stdout 报告内容、退出码 0、数量守恒。
10. **POSIX SIGINT 端到端**：启动长压测子进程 → `proc.send_signal(SIGINT)` → 限定时间内退出、返回码 130、输出含 `Interrupted: true`。
- 所有超时/中断类测试外层加时间限制，防止套件死锁。

**Step 8 — README.md**：`pip install -e ".[test]"`、运行示例（题目第五节三条命令）、`pytest` 说明、设计决策（并发模型、降级行为、延迟口径、退出码、macOS/Linux 平台限制）、已完成/未完成清单（未完成项写明后续实现方案）。

### 第二阶段 — 核心测试全绿后按优先级追加

1. **P50/P90/P99** 分位延迟（stats 已预留 percentile，仅加报告行）。
2. **`--json` 输出**：stdout 仅含 JSON，一切提示写 stderr；空统计字段为 `null`。
3. **更多边界与端到端测试**。

**本次不实现**（README 未完成清单中说明思路）：POST/`--method`、自定义 Header/`--header`、`--body`、实时进度条。实时进度尤其会增加异步任务清理与输出测试复杂度，性价比低。

## 五、验证方式（端到端）

1. `pytest tests/ -v` 全绿（含 CLI 与 SIGINT 端到端）。
2. 本地起 server（`python3 -m http.server 8080` 或 `npx serve -l 8080`），跑题目原始命令：
   `python -m httpload --url http://localhost:8080/ --requests 10000 --concurrency 50 --timeout 2s`，检查守恒公式与 RPS 合理性。
3. 404 场景：`--url http://localhost:8080/not-found-xyz -n 1000 -c 20 -t 2s` → Non-2xx == 1000。
4. 网络错误：指向本机未监听端口 `-n 100 -c 10 -t 500ms` → Errors == 100，快速结束。
5. 手动 Ctrl+C：长压测中按一次 → 快速退出、输出 `Interrupted: true` + 部分统计，`echo $?` 为 130，进程无遗留。
6. 短参数形式 `-u/-n/-c/-t` 跑一次；二阶段完成后验证 `--json | python -m json.tool`。

## 六、最终验收标准（Codex 审查通过条件）

- 任意时刻服务端观测到的最大并发不超过配置值。
- 正常完成时实际请求数等于 `--requests`。
- 正常和中断场景均满足结果数量守恒。
- 超时与普通网络错误不会重复计数。
- 响应体、ClientSession、worker 和辅助任务均能正确清理。
- Ctrl+C 后能快速输出部分统计并以 `130` 退出。
- 零完成请求不会导致统计报告崩溃。
- 参数错误在请求发出前被拒绝并返回 `2`。
- 自动化测试不依赖公网且可以稳定重复运行。
- README 明确并发降级、延迟口径、退出码和平台限制。

## 七、风险与注意点

- **asyncio 计数无需锁**的前提是"领取序号→递增"之间没有 await——实现时保持该段为纯同步代码。
- aiohttp 的 `ClientTimeout(total=...)` 覆盖连接+读全程；超时以 `asyncio.TimeoutError` 子类抛出，捕获顺序必须**先于** `ClientError`；`CancelledError` 更须最先放行，否则中断请求会被误计为 error。
- 取消在途请求时 aiohttp 会主动断开连接，属预期行为；`session.close()` 放 finally 保证无遗留连接。
- 本机环境：系统 Python 为 3.9.6 且无 aiohttp/pytest。实现前需定位 Python ≥3.10 解释器（Homebrew/uv/conda），创建 venv 后 `pip install -e ".[test]"`；若确无 3.10+，再评估降级兼容 3.9（需去掉 `X | Y` 类型语法等）。
- SIGINT 端到端测试仅在 POSIX 平台运行（`pytest.mark.skipif(sys.platform == "win32")`）。
