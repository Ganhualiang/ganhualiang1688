# httpload：命令行 HTTP 接口压测工具

这是我针对一道并发编程考核题完成的命令行 HTTP 压测工具。它能够向指定地址发起固定数量的并发请求，并对成功、非 2xx、网络错误、超时、吞吐量和请求延迟进行统计。

这个项目的重点不只是“让代码跑起来”，而是展示我面对一道有明确时间限制的工程题时，如何完成需求拆解、方案设计、独立审查、编码实现、自动化验证，以及对替代技术路线的进一步思考。

- 实现语言：Python 3.10+
- 并发与 HTTP：asyncio + aiohttp
- 当前验证结果：63 项自动化测试，Windows 11 实测 62 passed / 1 平台跳过（macOS 见「测试策略」说明）
- 实现目录：[httpload](./httpload)
- 题目原文：[实习生能力考核题目.md](./实习生能力考核题目.md)

> **说明**：截止提交时我手上只有 macOS 环境，Windows 分支按方案主动划在了保证范围之外
> （见 [PLAN.md](./PLAN.md) 必改项 2）。Windows 支持是**截止之后**补做的，改动与实测
> 结论见下方「[截止后补充：Windows 支持](#截止后补充windows-支持)」。

## 我的解题思路

我没有让 AI 直接从题目生成一份代码，而是先建立一条可审查、可验证的工程流程：

| 阶段 | 我的做法 | 产出 |
|---|---|---|
| 1. 拆解题目 | 先提取并发上限、固定请求总数、资源释放、超时分类、Ctrl+C 和统计守恒等硬约束 | 明确实现边界与验收标准 |
| 2. 先写方案 | 让 Claude 在编码前给出模块划分、数据模型、控制流和测试计划 | [PLAN.md](./PLAN.md) |
| 3. 独立审查 | 让 Codex 不参与第一版方案生成，专门从正确性、异常路径和可测试性角度审查 | [plan review.md](./plan%20review.md) |
| 4. 修订后实现 | 将审查结论反馈给 Claude，先修订方案，再按修订后的方案完成代码 | [httpload](./httpload) |
| 5. 端到端验证 | 不只测试函数，还运行真实 CLI 子进程、发送真实 SIGINT，并检查退出码与部分统计 | 55 项自动化测试 |
| 6. 思考替代解 | 核心实现完成后，再用 Go 标准库重新推演同一道题的解法与权衡 | [PLAN_GO.md](./PLAN_GO.md) |
| 7. 补齐平台短板（**截止后**） | 拿到 Windows 环境后先跑原有测试让平台差异自己暴露，再逐个定位修复 | 63 项自动化测试 + [下方专节](#截止后补充windows-支持) |

这套流程的核心是把“生成”和“审查”分开。Claude 负责提出方案和实现，Codex 负责寻找方案中的漏洞，而我用题目要求和测试结果决定哪些建议应该进入最终实现。AI 在这里是工程协作工具，验收标准和最终判断仍由我掌握。

## 为什么要先审查方案

第一版计划的技术方向是正确的，但仍有一些只在异常路径中才会暴露的问题。如果直接进入编码，这些问题很容易被正常请求场景掩盖。

Codex 审查后，方案重点补强了以下内容：

- 用完整的 `RunResult` 描述 planned、scheduled、completed、四类结果、延迟、耗时与中断状态。
- 明确定义正常与中断场景的数量守恒关系，防止请求被重复计数或漏计。
- 将 Ctrl+C 设计成完整控制流：停止调度、取消在途请求、等待 worker 清理、关闭 session、输出部分统计并返回退出码 130。
- 处理“第一个请求完成前就被中断”的空统计场景，避免平均值、最小值和分位数计算崩溃。
- 同时限制 worker 数量和连接池上限，使并发与资源占用都可证明地有界。
- 增加真实 CLI 与 SIGINT 端到端测试，验证入口、stdout/stderr、退出码和信号处理，而不只验证内部函数。
- 使用动态本地端口和带外层时限的测试，减少端口占用与异步死锁造成的不稳定。

这一步让我把注意力从“正常情况下能不能运行”，推进到“超时、断网、中断和零结果时是否仍然正确”。

## 最终实现

### 有界并发

工具只创建 `min(concurrency, requests)` 个 worker。每个 worker 从共享计数器领取下一个请求，不会为全部请求一次性创建协程，因此请求总量增大时，后台任务数量仍然由并发参数控制。

连接层复用同一个 `aiohttp.ClientSession`，并将连接池的全局上限和单主机上限都设置为实际并发数。响应正文采用分块读取后丢弃的方式，既能够释放连接供后续请求复用，也不会长期保存完整响应内容。

### 互斥统计

每个已完成请求只会进入以下一种结果：

- `Succeeded`：HTTP 200-299
- `Non-2xx`：其他 HTTP 状态码
- `Errors`：连接失败等网络错误
- `Timeouts`：超过单请求超时时间

正常完成时必须满足：

```text
Scheduled = Completed = Planned
Completed = Succeeded + Non-2xx + Errors + Timeouts
```

用户中断时，已经领取但被取消的请求可以计入 `Scheduled`，但不会进入 `Completed` 或延迟统计，因此仍满足：

```text
Completed <= Scheduled <= Planned
Completed = Succeeded + Non-2xx + Errors + Timeouts
```

### 优雅中断

收到中断信号后，runner 会停止领取新请求并取消 worker。主流程等待取消完成、关闭 HTTP session，然后输出已经完成部分的统计结果，并以 130 退出。信号接线按平台分支：POSIX 用 `loop.add_signal_handler`，Windows 用 `signal.signal` 注册 SIGINT 与 SIGBREAK。两条分支都已实机验证（详见「[截止后补充：Windows 支持](#截止后补充windows-支持)」）。

### 输出与扩展能力

在题目要求的 GET、基础计数和平均延迟之外，最终实现还支持：

- P50、P90、P99 延迟
- JSON 报告，且提示与进度信息保持在 stderr
- POST、PUT、DELETE、PATCH、HEAD、OPTIONS
- 自定义 Header 和请求 Body
- 实时请求进度
- 参数错误、正常结束、内部错误和用户中断的明确退出码

## 测试策略

测试全部使用本地 `aiohttp.web` 服务，不依赖公网。当前 63 项测试覆盖：

- 参数、URL、时长和 Header 解析
- 实际请求总数与最大并发上限
- 2xx、非 2xx、网络错误和超时分类
- 统计守恒与空延迟数据
- 网络错误与超时场景不死锁
- 中断后停止调度并输出部分统计
- POST、Header、Body 和 HEAD 请求
- 文本、JSON 与实时进度输出
- CLI 子进程端到端执行
- 真实 SIGINT、退出码 130 与限时退出
- Windows 控制台事件（Ctrl+C / Ctrl+Break）端到端与信号接线（截止后补充）
- 非 UTF-8 stdout 编码下的帮助输出（截止后补充）

```bash
cd httpload
source .venv/bin/activate          # Windows PowerShell: .\.venv\Scripts\Activate.ps1
pytest tests/ -v
```

最近一次完整验证结果（信号用例按平台跳过，不适用的不算通过）：

```text
Windows 11 + Python 3.13:  62 passed, 1 skipped     # 本次实测；跳过 POSIX SIGINT 用例
macOS:                     55 passed                # 截止前实测（当时共 55 项）
```

补齐 Windows 之后我手上没有 macOS 环境，**没有在 macOS 上重跑**。新增的 8 个用例里，
2 个 Windows 控制台事件用例在 POSIX 上会被 `skipif` 跳过，其余 6 个都是跨平台的（其中
2 个专门用假事件循环从 Windows 上验证 POSIX 分支的接线与降级路径）；对 macOS 原有行为的
唯一改动是把信号接线包进 `interrupt_guard`，POSIX 分支仍走 `loop.add_signal_handler`。
按此推断 macOS 应为 61 passed / 2 skipped，但这是推断，不是实测。

## 截止后补充：Windows 支持

**这一节的全部改动都发生在提交截止之后。** 截止前我只有 macOS 环境，所以方案里主动把
Windows 划在保证范围之外（[PLAN.md](./PLAN.md) 必改项 2：「Windows 信号不做回退实现，
改为 README 声明保证范围为 macOS/Linux」）。我认为写「未验证」比写一段没跑过的兼容代码
更诚实，但这毕竟是一块短板。拿到 Windows 环境（Windows 11 + Python 3.13）后，我把这块补齐了。

补做的第一件事不是写代码，而是**先在 Windows 上跑一遍原有测试**，让平台差异自己暴露出来。
结果是 55 个测试里 6 个失败，且失败原因不是一个而是两个：

```text
6 failed, 48 passed, 1 skipped
```

### 缺陷 1：Windows 上工具完全不可用（不只是 Ctrl+C 失效）

全部 5 个 CLI 子进程测试都以退出码 1 失败，stderr 只有一句 `httpload: unexpected error:`
——冒号后面是空的。定位后是 `loop.add_signal_handler(signal.SIGINT, ...)`：这个 API 在
Windows 的 `ProactorEventLoop` 上未实现，抛出的 `NotImplementedError` 消息为空字符串，被
顶层兜底吞成了一句没有信息量的报错。

这比我原先以为的严重：它不是「Ctrl+C 不优雅」，而是**信号接线在压测开始前就抛异常，
任何一次压测都跑不起来**。原来的 README 只声明了「优雅中断不保证 Windows」，实际上
Windows 上整个工具都用不了——声明的范围比真实的破坏面小，这本身就是个问题。

修法（[httpload/cli.py](./httpload/httpload/cli.py)）：

- 把信号接线抽成 `interrupt_guard` 上下文管理器，按平台分两条实现。
- POSIX 保持 `loop.add_signal_handler`。
- Windows 用 `signal.signal` 注册 SIGINT **和 SIGBREAK**（Ctrl+Break，以及发给新进程组的
  `CTRL_BREAK_EVENT`）。处理器里不直接改事件循环状态，只做一次
  `loop.call_soon_threadsafe(runner.request_stop)` 转交——Windows 的 Python 信号处理器在
  主线程的字节码边界执行，直接操作 Event 和 Task 是在信号上下文里碰循环内部状态。
- 接线失败（例如在非主线程里运行）不再让整次压测崩掉，只打印 warning 并降级为不可优雅
  中断；`main()` 另外兜住 `KeyboardInterrupt`，保证这条降级路径仍以 130 退出。
- 顶层报错补上异常类型名，`NotImplementedError` 这类空消息异常不再无从定位。

这里我特意**没有**加一件事：Windows 上常见的写法是再挂一个 `asyncio.sleep(0.1)` 的定时
任务，用来周期性唤醒事件循环，否则挂起的信号处理器要等 IOCP 阻塞结束才会执行。我先写了
它，然后去验证它到底有没有用——写了一个空闲循环（`await ev.wait()`，无任何定时器）的对照
实验，发送 `CTRL_BREAK_EVENT`：

```text
noticker : exit=0 observed=0.02s 'WOKE after 0.50s'
ticker   : exit=0 observed=0.02s 'WOKE after 0.50s'
```

两者都是 0.02s 唤醒，说明这个定时任务是多余的。原因在标准库里：`ProactorEventLoop` 构造时
已经调用了 `signal.set_wakeup_fd(self._csock.fileno())`（`asyncio/proactor_events.py`），
信号会写入自管道并立刻打断 IOCP 等待。于是我把已经写好的 ticker 删掉，只在注释里留下
「为什么不需要」。**照抄社区常见写法本来也能过测试，但那会留下一段没人说得清用途的代码。**

### 缺陷 2：连接被拒绝的分类在 Windows 上被平台语义改写

`test_connection_errors_no_deadlock` 期望 20 个 Errors，Windows 上实测 20 个 Timeouts。
这个不能靠改代码「修」，得先搞清楚是谁的问题——我在三层分别量了一次连接未监听回环端口的耗时：

```text
raw socket    : 2.052s -> ConnectionRefusedError: [WinError 10061]
asyncio       : 2.046s -> ConnectionRefusedError: [WinError 1225]
aiohttp t=10s : 2.037s -> ClientConnectorError
```

三层一致，说明这是平台的连接语义差异，不是分类逻辑的 bug：POSIX 回环口对未监听端口立刻
回 RST，毫秒级就报 `ECONNREFUSED`；Windows 不回 RST，而是重传 SYN，约 2s 后才给出
`WSAECONNREFUSED`。在 `--timeout 500ms` 下超时一定先到，按超时归类——这恰好符合题目
「超过单请求超时时间 → 超时」的口径。

所以**错的是测试的隐含假设，不是被测代码**。我改了测试而不是实现：把该用例的单请求超时
按平台取值（Windows 5s / POSIX 0.5s），保留 `errors == 20` 这个强断言；另外补了一个反向
用例，用 50ms 超时断言「连接拒绝晚于超时时必须记 Timeouts 且 errors == 0」，把两类结果
互斥、不重复计数这一点在两个平台上都钉住。题目第五节的第三条命令在 Windows 上会显示
Timeouts，我在 [httpload/README.md](./httpload/README.md) 里专门写了这个差异和复现方法，
免得看起来像统计错了。

### 缺陷 3：英文 Windows 控制台下 `--help` 直接失败

顺手发现的第三个问题：argparse 的帮助文本含中文，而 Windows 控制台默认代码页在英文环境
是 cp1252，编不出中文。更隐蔽的是 `UnicodeEncodeError` 是 `ValueError` 的子类，正好被参数
校验的 `except ValueError` 捕获，于是伪装成一条「参数错误」以退出码 2 结束：

```text
httpload: error: 'charmap' codec can't encode characters in position 234-236
```

我的中文 Windows 是 cp936，编得出中文，所以本机跑不出来——用 `PYTHONIOENCODING=cp1252`
复现的。修法是启动时 `harden_stdio()` 把 stdout/stderr 的错误处理策略降为 `errors="replace"`：
代码页编得出就正常显示，编不出就退化成替代字符，而不是让命令失败。对应加了一个跨平台
测试（`PYTHONIOENCODING=ascii` 下 `--help` 必须仍以 0 退出）。

### 补充的测试

从 55 个增加到 63 个，按平台跳过不适用的信号用例：

| 新增测试 | 覆盖内容 |
|---|---|
| `test_cli_ctrl_break_graceful_exit` | Windows 端到端：`CREATE_NEW_PROCESS_GROUP` + `CTRL_BREAK_EVENT` → 退出码 130 + 部分统计 |
| `test_cli_ctrl_c_graceful_exit` | Windows 端到端：真实 `CTRL_C_EVENT`（子进程先 `SetConsoleCtrlHandler(None, False)` 恢复被进程组屏蔽的 Ctrl+C） |
| `test_interrupt_guard_routes_sigint_to_callback` | 进程内验证两条平台分支：真实 `raise_signal(SIGINT)` 触发回调，且退出上下文后恢复原处理器 |
| `test_interrupt_signals_covers_platform` | SIGBREAK 只在 Windows 纳入中断信号集合 |
| `test_install_interrupt_posix_branch_uses_loop_api` | POSIX 分支的接线与撤销逻辑，用假事件循环在任意平台验证（含 Windows） |
| `test_install_interrupt_degrades_when_platform_lacks_api` | 接线失败只打印 warning 并降级，不让整次压测失败——即缺陷 1 的回归防线 |
| `test_cli_help_survives_non_utf8_stdout` | 非 UTF-8 stdout 编码下 `--help` 仍以 0 退出 |
| `test_connection_refused_faster_than_timeout_counts_as_error` | 连接拒绝与超时两类结果互斥、不重复计数 |

第二个用例值得单独说一句：Windows 无法向单个子进程投递控制台事件，只能发给整个进程组，
所以子进程必须用 `CREATE_NEW_PROCESS_GROUP` 独立成组，否则会把 pytest 自己一起中断；而
`CREATE_NEW_PROCESS_GROUP` 又会顺带屏蔽新组的 Ctrl+C。绕过这两条限制之后，测到的就是
用户在 Windows 终端里按 Ctrl+C 的真实路径，而不是一个近似替代。

### 补齐后的实测结果

```text
62 passed, 1 skipped in 13.25s      # 跳过的 1 个是 POSIX SIGINT 用例
```

题目第五节的全部验证命令也在 Windows 上实跑通过（10000 req / 50 并发全部成功、404 全部计入
Non-2xx、未监听端口按超时口径分类），Ctrl+C 与 Ctrl+Break 均在 0.05s 内退出、返回 130 并输出
部分统计，`Completed <= Scheduled <= Planned` 守恒成立。逐条数据见
[httpload/README.md](./httpload/README.md) 的「平台验证」。

[PLAN.md](./PLAN.md) 保持原样，不回填这次改动——它是截止前那份方案的记录，包括「Windows
不做回退实现」这个当时的决定。把后来的结论倒写进计划里，会让人看不出哪些判断是当时做的、
哪些是后来补的。

### 这次补齐让我更确认的两件事

1. **「未验证」和「已知不支持」是两种完全不同的状态。** 我以为自己声明的是后者，实际上是
   前者：真跑一遍才发现破坏面是「整个工具不可用」，比 README 里写的「优雅中断不保证」大得多。
   没跑过的平台，只能算不知道。
2. **测试失败先问「是实现错了还是测试的假设错了」。** 这次两个失败恰好一边一个：信号那个
   是实现真的坏了；连接拒绝那个是测试把 POSIX 的连接语义当成了普适前提。如果一律改实现去
   迁就测试，第二个就会变成一处为了让测试变绿而扭曲的分类逻辑。

## 为什么还要设计 Go 方案

Python 版本完成后，我没有停留在已有实现上，而是重新思考：如果更强调并发语义、取消传播与单文件交付，是否存在更贴合题目的技术路线？

因此我又设计了 [PLAN_GO.md](./PLAN_GO.md)。它不是对 Python 代码的机械翻译，而是利用 Go 的原生模型重新组织问题：

| 问题 | Python 实现 | Go 方案 |
|---|---|---|
| 并发控制 | 固定 asyncio worker pool | 固定 goroutine + jobs channel |
| 单请求超时 | `aiohttp.ClientTimeout` | `context.WithTimeout` |
| Ctrl+C 传播 | signal handler + stop event + task cancel | `signal.NotifyContext` + context 取消树 |
| 结果合并 | asyncio 单线程共享统计 | 每 worker 本地统计，结束后合并 |
| 交付方式 | Python 环境与依赖安装 | 标准库构建单一二进制 |

这个替代方案帮助我确认：技术选型不是比较语言优劣，而是比较它与题目约束、时间成本和交付环境的匹配程度。Python 方案借助现有环境能够快速实现和验证；Go 方案则在 context 取消、goroutine 管理和二进制分发上更直接。

## 快速运行

macOS / Linux：

```bash
cd httpload
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[test]"

httpload \
  --url http://localhost:8080/ \
  --requests 1000 \
  --concurrency 20 \
  --timeout 2s
```

Windows（PowerShell）：

```powershell
cd httpload
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[test]"

httpload --url http://localhost:8080/ --requests 1000 --concurrency 20 --timeout 2s
```

更多参数、POST 示例和输出格式见 [httpload/README.md](./httpload/README.md)。

## 仓库结构

```text
.
├── 实习生能力考核题目.md   # 原始题目
├── PLAN.md                 # Claude 生成并根据审查修订的 Python 实现方案
├── plan review.md          # Codex 对第一版方案的独立审查
├── PLAN_GO.md              # 完成主方案后的 Go 替代方案推演
├── httpload/               # Python 源代码、测试、配置与使用说明
└── README.md               # 项目过程与工程思路说明
```

## 总结

这次实现中，我重点验证了四件事：

1. 在有限时间内，先定义正确性和交付边界，比一开始堆功能更重要。
2. AI 生成的方案需要独立审查，尤其要检查中断、超时、资源清理和统计守恒等异常路径。
3. 完成一种实现后再推演另一种技术路线，能够帮助我区分“偶然可用的写法”和“由语言模型与工程约束共同决定的设计”。
4. 声明「未验证」只是把风险记下来，不等于风险变小了：真到 Windows 上跑一遍，才发现问题不是「Ctrl+C 不优雅」而是「整个工具跑不起来」。所以我在截止后把这块补齐并重新实测，而不是留着那句声明（见「[截止后补充：Windows 支持](#截止后补充windows-支持)」）。

最终交付的不只是一段压测代码，也是一套从方案到验证、从实现到复盘的完整解题过程。
