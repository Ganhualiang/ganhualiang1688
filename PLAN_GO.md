# httpload — 命令行 HTTP 压测工具（Go 实现方案）

## Context（背景与目标）

面试题要求实现一个类似 `hey`/`ab` 迷你版的命令行 HTTP 压测工具：向指定 URL 发起固定总数的并发 GET 请求，输出统计结果。题目原文见 [实习生能力考核题目.md](实习生能力考核题目.md)。

技术选型：**Go 1.21+，仅用标准库**（`net/http`、`context`、`sync`、`sync/atomic`、`flag`、`os/signal`、`time`、`net/url`、`sort`、`net/http/httptest`）。不引入第三方依赖，`go build` 直接产出单一可执行文件 `httpload`。

**为什么用 Go 而不是 Python asyncio**：这道题的五个考点（并发控制、HTTP 资源管理、超时取消、统计守恒、优雅退出）几乎都是 Go 的原生强项——

| 考点 | Python asyncio 的做法 | Go 的做法（更简洁） |
|---|---|---|
| 并发控制 | 固定数量 worker 协程 + 计数器 | 固定数量 goroutine + jobs channel，天然有界 |
| 超时 | `aiohttp.ClientTimeout` | `context.WithTimeout` 每请求一个 ctx |
| Ctrl+C 取消 | `loop.add_signal_handler` + stop event + cancel | `signal.NotifyContext`，取消沿 context 树自动传播到在途请求 |
| duration 解析 | 自己写 `parse_duration` | `time.ParseDuration` 原生支持 `2s`/`500ms`/`1m`/`1.5s` |
| 无数据竞争的计数 | asyncio 单线程无需锁 | 每 worker 本地统计 + 末尾合并，或 `sync/atomic` |
| 分发 | 需要 venv/pip | 单一静态二进制，`httpload` 命令天然存在 |

**时间策略（硬约束）**：题目建议 30 分钟，实际按 1 小时硬性截止规划。先在 30 分钟内交付全部必做项，剩余时间补测试和加分项，附加功能不得影响核心功能交付。

---

## 一、题目拆解（考点 → Go 技术方案映射）

| 考点 | 题目要求 | 本方案对应手段 |
|---|---|---|
| 并发控制 | 任意时刻在途请求 ≤ concurrency；禁止无界任务；结束后无遗留 | 启动 `k = min(concurrency, requests)` 个 worker goroutine，从一个带缓冲/无缓冲的 `jobs chan struct{}` 领任务。goroutine 数量固定，天然有界；`sync.WaitGroup` 保证全部退出后主流程才继续，无遗留 |
| HTTP 资源管理 | 复用连接、正确关闭响应体、不长期持有正文 | 全程复用一个 `*http.Client`，自定义 `http.Transport` 设 `MaxConnsPerHost=k`、`MaxIdleConnsPerHost=k`；每请求 `defer resp.Body.Close()`；`io.Copy(io.Discard, resp.Body)` 排空正文即弃，不驻留内存 |
| 超时与取消 | 单请求超时；Ctrl+C 后停止调度、取消在途、不挂起、输出部分统计 | 每请求 `ctx, cancel := context.WithTimeout(rootCtx, timeout)` + `req.WithContext(ctx)`；`rootCtx` 来自 `signal.NotifyContext(ctx, os.Interrupt)`，SIGINT 触发后取消自动传播到所有在途请求 |
| 统计正确性 | Completed = Succeeded + Non-2xx + Errors + Timeouts | 四类互斥分类；每 worker 持有本地 `workerStat`，run 结束后合并成 `RunResult`；正常/中断两种守恒约束都有测试断言 |
| 参数校验 | URL 缺失/非法、requests≤0、concurrency≤0、timeout 非法 | `flag` 包解析 + 显式校验；`time.ParseDuration` 解析 timeout；concurrency > requests 时降为 requests 并向 stderr 打印提示 |
| 测试习惯 | ≥2 个自动化测试，不依赖公网 | `net/http/httptest` 起本地 server；runner 层直测 + 编译二进制的 CLI/SIGINT 端到端测试 |

**关键设计决策（需在 README 声明）：**
- 延迟统计口径：对所有"已完成"请求记录耗时（含超时/非 2xx 的实际耗时）；被取消、未拿到结果的在途请求不计入 `completed` 与延迟。
- 并发 > 请求数：降级而非报错，启动时向 **stderr** 打印 `Note: concurrency reduced to N`。
- 提示/告警写 **stderr**，统计报告写 **stdout**（为后续 `--json` 管道消费预留）。
- 退出码约定：`0` 正常完成、`2` 参数错误、`130` 用户中断、`1` 未预期内部错误。
- 优雅中断保证范围：**macOS/Linux**（`os.Interrupt`）；Go 的 `signal.NotifyContext` 在 Windows 上也能捕获 Ctrl+C，但本次只在 POSIX 平台编写端到端信号测试，README 注明验证范围。

---

## 二、运行结果模型（RunResult）

```go
// stats.go
type RunResult struct {
    Target      string        // 被压测 URL
    Planned     int           // --requests 指定的计划总数
    Concurrency int           // 实际生效并发数（可能已降级）
    Scheduled   int64         // 已被 worker 领取的请求数
    Completed   int64         // 已得到结果（含超时/非2xx）的请求数
    Succeeded   int64         // 2xx
    Non2xx      int64         // 其他状态码
    Errors      int64         // 连接类失败
    Timeouts    int64         // 超过单请求超时
    Latencies   []time.Duration // 仅已完成请求的耗时
    Elapsed     time.Duration
    Interrupted bool
}
```

**守恒约束（测试断言）：**
- 正常完成：`Scheduled == Completed == Planned` 且 `Completed == Succeeded + Non2xx + Errors + Timeouts`
- 中断时：`Completed <= Scheduled <= Planned` 且 `Completed == Succeeded + Non2xx + Errors + Timeouts`
- 已领取但被取消、未拿到结果的在途请求计入 `Scheduled`，**不计入** `Completed` 和延迟统计。

**零完成兜底**：`Latencies` 为空时，文本报告 Avg/Min/Max/Pxx 输出 `N/A`，JSON 对应字段为 `null`，Requests/sec 输出 `0`——报告层不得对空 slice 求 min/max。

**统计并发策略**：每个 worker 维护本地 `workerStat`（四类计数 + 本地 latency slice），互不共享，全程无锁；`run()` 在 `wg.Wait()` 之后单线程合并所有 `workerStat` → `RunResult`。`Scheduled`/`Completed` 用 `atomic.Int64`（供中断时读取部分进度），或同样走本地累加合并。这样彻底规避数据竞争，`go test -race` 干净。

---

## 三、项目结构

```
1688实习/httpload-go/
├── go.mod                    // module httpload，go 1.21，无第三方依赖
├── cmd/httpload/
│   └── main.go               // 入口：flag 解析、参数校验、signal.NotifyContext 接线、退出码
├── internal/
│   ├── config/
│   │   ├── config.go         // Config 结构、ParseFlags、Validate（URL/requests/concurrency/timeout）
│   │   └── config_test.go    // 参数校验各分支单测
│   ├── runner/
│   │   ├── runner.go         // Runner：worker pool、请求执行与分类
│   │   └── runner_test.go    // 并发上限、分类、超时、错误、守恒、中断取消
│   └── report/
│       ├── stats.go          // RunResult、守恒检查、百分位、合并
│       ├── report.go         // 文本报告（+二阶段 JSON），空统计兜底
│       └── report_test.go    // 百分位、守恒公式、空统计渲染
├── e2e_test.go               // 编译二进制后的 CLI 端到端 + POSIX SIGINT 测试
└── README.md                 // 安装/运行/测试说明 + 设计决策 + 平台限制 + 已完成/未完成清单
```

> 也可用扁平单包结构（所有 `.go` 放一个 `main` 包）以省时间；上面的分层是推荐版，便于测试隔离。若时间紧张，先扁平，测试全绿后再拆包。

`go.mod`：
```
module httpload
go 1.21
```
无 `require` 段——纯标准库。`go build -o httpload ./cmd/httpload` 即可产出命令。

---

## 四、实现步骤

### 第一阶段 — 必做项（目标 30 分钟内完成）

**Step 1 — 项目骨架**：`go mod init httpload`；建目录；`main.go` 空壳能编译。

**Step 2 — config.go：参数解析与强化校验**
- `flag` 包：`-url`/`-u`、`-requests`/`-n`、`-concurrency`/`-c`、`-timeout`/`-t`。长短参数各注册一次，指向同一变量（`flag.StringVar(&url, "url", ...)` 与 `flag.StringVar(&url, "u", ...)`）。
- timeout 用 `flag.Duration` 或 `flag.String` + `time.ParseDuration`；`time.ParseDuration` 原生支持 `2s`/`500ms`/`1m`/`1.5s`，非法返回 error。
- 校验（全部在发请求前完成，任一失败 `fmt.Fprintln(os.Stderr, ...)` 并 `os.Exit(2)`）：
  - `url != ""`；`u, err := url.Parse(raw)`；`err == nil`；
  - scheme 仅允许 `http`/`https`；
  - `u.Hostname() != ""`；
  - 主动读取 `u.Port()`（非空时校验是数字且在 1–65535）；
  - 拒绝含空白字符的 URL（`strings.ContainsAny(raw, " \t\r\n")`）。
  - `requests > 0`、`concurrency > 0`、`timeout > 0`；
  - `concurrency > requests` → 降为 `requests`，向 stderr 打印 `Note: concurrency reduced to N`。

**Step 3 — stats.go：RunResult 与守恒**：按第二节定义；`Percentile(p float64) time.Duration` 排序后线性插值；空 `Latencies` 时各派生指标返回哨兵值（如 `-1` 或单独的 `ok bool`），由报告层渲染为 `N/A`。`Merge(workerStats []workerStat)` 合并计数与 latency。

**Step 4 — runner.go：worker pool 并发引擎**
```go
type Runner struct {
    client *http.Client
    cfg    config.Config
}

func New(cfg config.Config) *Runner {
    tr := &http.Transport{
        MaxConnsPerHost:     cfg.Concurrency, // 连接池显式上限
        MaxIdleConnsPerHost: cfg.Concurrency,
        MaxIdleConns:        cfg.Concurrency,
    }
    return &Runner{
        client: &http.Client{Transport: tr}, // 注意：不在 Client 上设 Timeout，超时走每请求 context
        cfg:    cfg,
    }
}

func (r *Runner) Run(ctx context.Context) report.RunResult {
    k := r.cfg.Concurrency // 已在校验阶段降级为 min(concurrency, requests)
    jobs := make(chan struct{})
    stats := make([]workerStat, k)
    var scheduled, completed atomic.Int64
    var wg sync.WaitGroup

    start := time.Now()
    // 生产者：发 planned 个任务；ctx 取消则停止发送
    go func() {
        defer close(jobs)
        for i := 0; i < r.cfg.Requests; i++ {
            select {
            case <-ctx.Done():
                return
            case jobs <- struct{}{}:
            }
        }
    }()
    // 固定 k 个 worker
    for w := 0; w < k; w++ {
        wg.Add(1)
        go func(id int) {
            defer wg.Done()
            for range jobs {
                scheduled.Add(1)
                r.doOne(ctx, &stats[id])
                completed.Add(1)
            }
        }(w)
    }
    wg.Wait()
    elapsed := time.Since(start)
    res := report.Merge(stats)
    res.Elapsed = elapsed
    res.Interrupted = ctx.Err() != nil
    // Scheduled/Completed 以合并结果为准（合并计数 == completed）
    return res
}
```
- `doOne`：
  ```go
  reqCtx, cancel := context.WithTimeout(ctx, r.cfg.Timeout)
  defer cancel()
  req, _ := http.NewRequestWithContext(reqCtx, http.MethodGet, r.cfg.URL, nil)
  t0 := time.Now()
  resp, err := r.client.Do(req)
  lat := time.Since(t0)
  ```
- 分类顺序（互斥，先后有讲究）：
  1. `err != nil` 时先判是否超时：`errors.Is(err, context.DeadlineExceeded)` → **timeout**；
  2. 否则若 `ctx.Err() == context.Canceled`（根 ctx 被 SIGINT 取消导致的错误）→ **不计入 completed**，直接 return（该请求属被取消的在途请求）；
  3. 其余 `err != nil` → **error**（连接失败等）；
  4. `err == nil`：`io.Copy(io.Discard, resp.Body)` 排空 → `resp.Body.Close()` → 按 `resp.StatusCode` 归为 2xx（**succeeded**）或其他（**non2xx**）；记录 `lat`。
- 关键陷阱：**用户超时**（`context.DeadlineExceeded`）与**用户中断取消**（`context.Canceled`）都表现为 `client.Do` 返回错误，必须用 `errors.Is` 区分，且中断取消的请求不计入 completed（否则守恒被破坏）。超时请求计入 completed 与 timeouts。

**Step 5 — 优雅退出（main.go 接线）**
```go
func main() {
    cfg, err := config.ParseFlags(os.Args[1:])
    if err != nil { fmt.Fprintln(os.Stderr, err); os.Exit(2) }

    ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt) // SIGINT
    defer stop()

    res := runner.New(cfg).Run(ctx)
    report.WriteText(os.Stdout, res)

    if res.Interrupted { os.Exit(130) }
    os.Exit(0)
}
```
- SIGINT 到达 → `ctx` 被取消 → 生产者停止往 `jobs` 发送并 `close(jobs)` → worker `for range jobs` 自然退出 → 在途请求因 `reqCtx` 继承被取消的 `ctx` 而立即返回错误（归为"被取消"，不计 completed）。
- `wg.Wait()` 保证所有 worker 退出后才合并统计与打印——**无遗留 goroutine**。
- 不做二次 Ctrl+C 强杀设计：第一次中断即自身收尾干净、快速退出。
- `res.Interrupted = ctx.Err() != nil` 标记中断，报告追加 `Interrupted: true` + `Scheduled`，退出码 `130`。
- Go 无需手动移除 signal handler（`defer stop()` 恢复默认行为），比 Python 少一层清理负担。

**Step 6 — report.go：文本报告**：对齐题目示例（Target/Requests/Concurrency、Completed/Succeeded/Non-2xx/Errors/Timeouts、Elapsed/RPS/Avg/Min/Max）；中断时追加 `Interrupted: true` + `Scheduled`；空统计按第二节兜底渲染（Avg/Min/Max = `N/A`，RPS = `0`）。RPS = `Completed / Elapsed.Seconds()`。用 `text/tabwriter` 或 `fmt.Fprintf` 定宽对齐。

**Step 7 — 核心自动化测试**（`httptest.NewServer` 起本地 server，路由：`/ok` → 200；`/status/404` → 404；`/slow?delay=` → sleep 后 200；`/mixed` → 按 `atomic` 请求序号稳定轮换 200/404/慢响应；用 `atomic.Int64` 记 `hits` 和 `inFlight` 峰值，**`inFlight.Add(-1)` 必须在 `defer` 中**。`httptest.Server` 自动分配空闲端口，天然规避硬编码端口问题。网络错误测试：`httptest.NewServer` 起一个再 `.Close()`，用它释放后的地址即得一个确定未监听的端口）
1. **总数正确**：n=50 → `Completed == 50 == server.hits`，`Scheduled == Completed == Planned`。
2. **并发上限**：n=40, c=5, 每请求 delay 50ms → server 观测 `inFlight` 峰值 ≤ 5。
3. **分类正确**：打 `/status/404` → `Non2xx == n`，`Succeeded == 0`。
4. **超时**：timeout=100ms 打 `/slow?delay=1s` → `Timeouts == n`，整体耗时远小于 n×1s。
5. **网络错误不死锁**：打已关闭 server 的地址 → `Errors == n`；用 `context`/`time.AfterFunc` 或测试超时保证 10s 内返回。
6. **守恒**：打 `/mixed` → `Completed == Succeeded + Non2xx + Errors + Timeouts`。
7. **内部中断**：起长压测，200ms 后 `cancel()` 根 ctx → `Run` 限时返回，`Interrupted == true`，`Completed <= Scheduled <= Planned`，守恒成立。
8. **单元**：URL 校验各分支、`time.ParseDuration` 边界、`Percentile` 边界、空统计报告渲染。
- 所有测试用 `go test -race` 跑，确保无数据竞争；超时/中断类测试用 `context.WithTimeout` 兜底防死锁。

**Step 8 — README.md**：`go build -o httpload ./cmd/httpload`、运行示例（题目第五节三条命令）、`go test ./... -race` 说明、设计决策（并发模型、降级行为、延迟口径、退出码、平台限制）、已完成/未完成清单（未完成项写明后续实现方案）。

### 第二阶段 — 核心测试全绿后按优先级追加

1. **P50/P90/P99** 分位延迟（`Percentile` 已实现，仅加报告行）。
2. **`-json` 输出**：`encoding/json` 序列化 `RunResult`，stdout 仅含 JSON，一切提示写 stderr；空统计字段为 `null`（用 `*float64`/`omitempty` 或自定义 marshal）。
3. **CLI/SIGINT 端到端测试**（`e2e_test.go`）：
   - CLI 端到端：`go build` 出临时二进制 → `exec.Command` 打本地 httptest server → 校验 stdout 报告内容、退出码 0、数量守恒。
   - POSIX SIGINT：起长压测子进程 → `cmd.Process.Signal(os.Interrupt)` → 限定时间内退出、`ExitCode() == 130`、stdout 含 `Interrupted: true`。用 `//go:build !windows` 约束仅 POSIX 编译。

**本次不实现**（README 未完成清单说明思路）：POST/`-method`、自定义 Header/`-header`、`-body`、实时进度条。实时进度会增加输出与测试复杂度，性价比低。

---

## 五、验证方式（端到端）

1. `go test ./... -race -v` 全绿（含 CLI 与 SIGINT 端到端）。
2. `go build -o httpload ./cmd/httpload`；本地起 server（`npx serve -l 8080` 或 `python3 -m http.server 8080`），跑题目原始命令：
   `./httpload --url http://localhost:8080/ --requests 10000 --concurrency 50 --timeout 2s`，检查守恒公式与 RPS 合理性。
3. 404 场景：`--url http://localhost:8080/not-found-xyz -n 1000 -c 20 -t 2s` → Non-2xx == 1000。
4. 网络错误：指向本机未监听端口 `--url http://localhost:65530/ -n 100 -c 10 -t 500ms` → Errors == 100，快速结束。
5. 手动 Ctrl+C：长压测中按一次 → 快速退出、输出 `Interrupted: true` + 部分统计，`echo $?` 为 130，无遗留 goroutine（可加 `-race` 或运行时观察）。
6. 短参数形式 `-u/-n/-c/-t` 跑一次；二阶段完成后验证 `--json | jq`。

---

## 六、最终验收标准

- 任意时刻服务端观测到的最大并发不超过配置值（固定 k 个 goroutine + `MaxConnsPerHost=k` 双重保证）。
- 正常完成时实际请求数等于 `--requests`。
- 正常和中断场景均满足结果数量守恒。
- 超时（`context.DeadlineExceeded`）与中断取消（`context.Canceled`）与普通网络错误三者互斥、不重复计数。
- 响应体 `Close`、连接池、所有 goroutine 均正确清理（`wg.Wait()` 保证无遗留）。
- Ctrl+C 后能快速输出部分统计并以 `130` 退出。
- 零完成请求不会导致统计报告崩溃。
- 参数错误在请求发出前被拒绝并返回 `2`。
- `go test -race` 无数据竞争；测试不依赖公网且可稳定重复运行。
- README 明确并发降级、延迟口径、退出码和平台限制。

---

## 七、风险与注意点

- **超时 vs 中断取消的区分是本方案最大陷阱**：两者都让 `client.Do` 返回 error。必须 `errors.Is(err, context.DeadlineExceeded)` 判超时（计入 timeout + completed），`errors.Is(err, context.Canceled)`（或 `ctx.Err() == context.Canceled`）判中断（不计入 completed）。搞反会破坏守恒。
- **不要在 `http.Client` 上设 `Timeout`**：那是整个请求（含读 body）的硬超时，与 per-request `context.WithTimeout` 语义重叠且难区分超时来源。统一用 context 控制超时，错误类型清晰（`DeadlineExceeded`）。
- **响应体必须排空再关闭**：只 `Close()` 不读完会导致连接无法复用（Go 会丢弃该连接），`io.Copy(io.Discard, resp.Body)` 之后再 `Close()` 才能归还连接池，这正是"尽可能复用底层连接"的要求。
- **`defer cancel()` 不可省**：`context.WithTimeout` 每次都返回 `cancel`，不调用会泄漏 context 计时器，`go vet` 会告警。
- **生产者用 `select { case <-ctx.Done(): case jobs<-... }`**：否则 SIGINT 后生产者可能阻塞在满 channel 上，无法及时 `close(jobs)`，worker 收不到关闭信号而挂起。
- **统计避免共享可变状态**：每 worker 本地累加、末尾合并；`Scheduled`/`Completed` 若需中途读取用 `atomic`。全程跑 `-race` 验证。
- **`httptest` 天然解决端口问题**：无需硬编码 65530；关闭一个 `httptest.Server` 即得确定未监听地址用于网络错误测试。
- 本机环境需安装 Go（`go version` 确认 ≥1.21）；若无，`brew install go` 或从 go.dev 下载。全程无第三方依赖，无需配置代理拉包。
