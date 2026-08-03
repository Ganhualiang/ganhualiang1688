# httpload：命令行 HTTP 接口压测工具

这是我针对一道并发编程考核题完成的命令行 HTTP 压测工具。它能够向指定地址发起固定数量的并发请求，并对成功、非 2xx、网络错误、超时、吞吐量和请求延迟进行统计。

这个项目的重点不只是“让代码跑起来”，而是展示我面对一道有明确时间限制的工程题时，如何完成需求拆解、方案设计、独立审查、编码实现、自动化验证，以及对替代技术路线的进一步思考。

- 实现语言：Python 3.10+
- 并发与 HTTP：asyncio + aiohttp
- 当前验证结果：55 项自动化测试全部通过
- 实现目录：[httpload](./httpload)
- 题目原文：[实习生能力考核题目.md](./实习生能力考核题目.md)

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

收到 SIGINT 后，runner 会停止领取新请求并取消 worker。主流程等待取消完成、关闭 HTTP session，然后输出已经完成部分的统计结果，并以 130 退出。该信号处理方案面向 macOS 和 Linux；本次自动化验证在 macOS 环境完成。

### 输出与扩展能力

在题目要求的 GET、基础计数和平均延迟之外，最终实现还支持：

- P50、P90、P99 延迟
- JSON 报告，且提示与进度信息保持在 stderr
- POST、PUT、DELETE、PATCH、HEAD、OPTIONS
- 自定义 Header 和请求 Body
- 实时请求进度
- 参数错误、正常结束、内部错误和用户中断的明确退出码

## 测试策略

测试全部使用本地 `aiohttp.web` 服务，不依赖公网。当前 55 项测试覆盖：

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

```bash
cd httpload
source .venv/bin/activate
pytest tests/ -v
```

最近一次完整验证结果：

```text
55 passed
```

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

这次实现中，我重点验证了三件事：

1. 在有限时间内，先定义正确性和交付边界，比一开始堆功能更重要。
2. AI 生成的方案需要独立审查，尤其要检查中断、超时、资源清理和统计守恒等异常路径。
3. 完成一种实现后再推演另一种技术路线，能够帮助我区分“偶然可用的写法”和“由语言模型与工程约束共同决定的设计”。

最终交付的不只是一段压测代码，也是一套从方案到验证、从实现到复盘的完整解题过程。
