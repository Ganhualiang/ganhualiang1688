# httpload — 命令行 HTTP 接口压测工具

向指定 URL 发起固定总数的并发 HTTP 请求（默认 GET，支持 POST 等方法与自定义
Header / Body），输出压测统计结果。基于 **Python 3.10+ / asyncio / aiohttp** 实现。

## 安装

```bash
cd httpload
python3 -m venv .venv          # 需要 Python >= 3.10
source .venv/bin/activate
pip install -e ".[test]"
```

## 运行

```bash
httpload \
  --url http://localhost:8080/ \
  --requests 1000 \
  --concurrency 20 \
  --timeout 2s
```

也支持短参数与 `python -m httpload` 方式：

```bash
python -m httpload -u http://localhost:8080/ -n 1000 -c 20 -t 2s
```

| 参数 | 短参数 | 说明 | 默认值 |
|---|---|---|---|
| `--url` | `-u` | 被压测的 HTTP 地址（必填，仅 http/https） | — |
| `--requests` | `-n` | 总请求数 | 200 |
| `--concurrency` | `-c` | 最大并发请求数 | 10 |
| `--timeout` | `-t` | 单请求超时，支持 `2s` / `500ms` / `1m` / 纯数字秒 | 2s |
| `--method` | `-X` | HTTP 方法（GET/POST/PUT/DELETE/PATCH/HEAD/OPTIONS，大小写不敏感） | GET |
| `--body` | `-d` | 请求体字符串 | 无 |
| `--header` | `-H` | 自定义请求头 `'Name: value'`，可重复指定 | 无 |
| `--progress` | | 向 stderr 实时输出进度 `completed/planned`（stdout 仍为纯报告） | 关 |
| `--json` | | 以 JSON 输出统计结果（提示信息仍写 stderr） | 关 |

POST 示例：

```bash
httpload -u http://localhost:8080/api -n 500 -c 10 -t 2s \
  -X POST -d '{"k":"v"}' -H 'Content-Type: application/json' --progress
```

### 示例输出

```text
Target:         http://localhost:8080/
Requests:       1000
Concurrency:    20

Completed:      1000
Succeeded:      970
Non-2xx:        15
Errors:         5
Timeouts:       10

Elapsed:        2.35s
Requests/sec:   425.53
Avg latency:    43.21ms
Min latency:    10.14ms
Max latency:    201.45ms
P50 latency:    40.05ms
P90 latency:    88.12ms
P99 latency:    180.33ms
```

按 `Ctrl+C` 中断时，输出已完成部分的统计并追加：

```text
Interrupted:    true
Scheduled:      420
```

### 本地验证命令（对应题目第五节）

```bash
npx serve -l 8080          # 或 python3 -m http.server 8080

# 正常压测
httpload --url http://localhost:8080/ --requests 10000 --concurrency 50 --timeout 2s
# 非 2xx 统计（404）
httpload --url http://localhost:8080/not-found --requests 1000 --concurrency 20 --timeout 2s
# 网络错误（未监听端口）
httpload --url http://localhost:65530/ --requests 100 --concurrency 10 --timeout 500ms
```

## 测试

```bash
pytest tests/ -v
```

测试不依赖公网：在测试内启动本地 `aiohttp.web` server（含 200/404/慢响应/混合路由），
覆盖：请求总数正确、服务端观测最大并发 ≤ 配置、2xx/非 2xx 分类、慢请求超时、
网络错误不死锁、统计守恒、内部中止、CLI 子进程端到端、真实 SIGINT 优雅退出。

## 设计决策

- **并发模型**：固定 worker pool——只创建 `min(concurrency, requests)` 个 worker 协程，
  从共享计数器领取任务序号，天然有界，不会为全部请求预建任务；结束后无遗留任务。
- **HTTP 资源**：全程复用一个 `ClientSession`，连接池上限
  `TCPConnector(limit=并发数, limit_per_host=并发数)`；响应体用 `iter_chunked`
  分块排空即弃，不在内存保留完整正文。
- **分类口径**（互斥）：2xx → Succeeded；其他状态码 → Non-2xx；连接类失败
  （`ClientError`/`OSError`）→ Errors；超过单请求超时 → Timeouts。
  守恒：`Completed = Succeeded + Non-2xx + Errors + Timeouts`。
- **延迟口径**：对所有"已完成"的请求记录耗时（含超时/错误的实际耗时）；
  被取消的在途请求计入 Scheduled，但不计入 Completed 与延迟统计。
- **并发 > 请求数**：自动降为请求数，并向 stderr 打印
  `Note: concurrency reduced to N`（题目允许两种行为之一）。
- **零完成兜底**：无任何完成请求时 Avg/Min/Max/Pxx 显示 `N/A`（JSON 为 `null`），RPS 为 0。
- **退出码**：`0` 正常完成、`2` 参数错误（发请求前拒绝）、`130` 用户中断、`1` 未预期内部错误。
- **优雅退出**：SIGINT → 停止调度新请求 → 取消在途请求 → 等待清理 → 关闭 session →
  输出部分统计（`Interrupted: true` + `Scheduled`）→ 退出码 130。
- **平台限制**：优雅中断基于 `loop.add_signal_handler`，保证范围为 **macOS / Linux**；
  Windows 未验证。

## 已完成 / 未完成

**已完成**：
- 全部必做项：GET 压测、并发控制（worker pool）、参数校验、四类统计与守恒、
  文本报告、优雅退出（SIGINT → 部分统计 + 退出码 130）。
- 加分项：P50/P90/P99 分位延迟、`--json` 输出、`--method`/`--body`（POST 等任意方法）、
  自定义 Header（`--header 'K: V'` 可重复）、`--progress` 实时进度（写 stderr，
  中断时随 run() 一并取消清理）。
- 55 个自动化测试（含 CLI 子进程端到端与真实 SIGINT 端到端），不依赖公网。

**未完成**（后续实现思路）：
- `--duration` 按时长压测模式：以截止时间代替固定总数，worker 领取任务的判断从
  `next_index >= planned` 换为 `now >= deadline`，统计模型不变。
- 请求速率限制（`--qps`）：在 worker 领取任务后按令牌桶 `asyncio.sleep` 补偿，
  控制全局发压速率。
- 延迟直方图 / 更细分位输出：`latencies` 已完整保留，仅需增加报告渲染。
