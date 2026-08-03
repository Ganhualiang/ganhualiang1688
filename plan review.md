# PLAN 审查结论

## 一、审查结论

Claude 给出的方案整体技术方向正确，以下设计可以保留：

- 使用固定数量的 asyncio worker，避免一次性创建无界任务。
- 全程复用一个 `aiohttp.ClientSession` 和底层连接池。
- 使用 `async with` 管理响应，并分块读取、丢弃正文。
- 将结果互斥地划分为成功、非 2xx、网络错误和超时。
- 使用本地 HTTP Server 完成不依赖公网的自动化测试。
- 并发数超过请求数时，将实际并发数降为请求数并明确提示。

但当前 PLAN 的中断流程、统计模型和测试方案还没有完全闭环，暂不建议原样执行。完成本文列出的必改项后，可以通过方案审查并进入实现。

实际时间条件是 1 小时硬性截止，题目建议 30 分钟完成。因此应采用“先在 30 分钟内完成必做项，剩余时间补测试和加分项”的策略，不能让附加功能影响核心功能交付。

## 二、执行前必须修改

### 1. 补全运行结果模型

当前方案中的 `Stats` 只有请求计数和延迟列表，但报告和测试还需要运行状态。建议定义完整的 `RunResult`，至少包含：

- `planned`
- `concurrency`
- `scheduled`
- `completed`
- `succeeded`
- `non_2xx`
- `errors`
- `timeouts`
- `latencies`
- `elapsed`
- `interrupted`

正常完成时必须满足：

```text
scheduled = completed = planned
completed = succeeded + non_2xx + errors + timeouts
```

中断时允许：

```text
completed <= scheduled <= planned
completed = succeeded + non_2xx + errors + timeouts
```

已经领取但被取消的在途请求计入 `scheduled`，但不计入 `completed` 和延迟统计。

### 2. 明确 Ctrl+C 的完整控制流

建议由 `LoadRunner` 提供统一的 `request_stop()` 方法：

1. 第一次收到 `SIGINT` 时设置 stop event，阻止 worker 领取新请求。
2. 取消当前所有 worker，使在途 HTTP 请求尽快结束。
3. 使用 `await asyncio.gather(*workers, return_exceptions=True)` 等待取消清理完成。
4. 关闭 `ClientSession`，取消并等待进度任务，移除 signal handler。
5. 输出部分统计，标记 `Interrupted: true`。
6. 进程退出码返回 `130`。

参数错误返回 `2`，正常完成返回 `0`，未预期的内部错误返回 `1`。不要用第二次 Ctrl+C 的设计代替第一次中断后的正确收尾。

如果 1 小时内无法可靠支持 Windows 信号处理，可以在 README 中明确第一版的优雅中断保证范围为 macOS/Linux，避免声称一个没有实际验证的 Windows 回退方案。

### 3. 处理零完成请求

用户可能在第一个请求完成前中断，此时延迟列表为空。必须提前定义输出行为，避免 `min()`、`max()` 或平均值计算报错：

- 文本报告中的 Avg/Min/Max/Pxx 输出 `N/A`。
- JSON 中对应字段输出 `null`。
- Requests/sec 输出 `0`。

### 4. 给连接池设置显式上限

不要使用 `TCPConnector(limit=0)`。虽然 worker 数量已经限制请求并发，但无限连接池不利于证明工具正确控制资源。

建议使用：

```python
TCPConnector(
    limit=actual_concurrency,
    limit_per_host=actual_concurrency,
)
```

这样 worker pool 和连接池共同保证资源上限。

### 5. 增加真实命令行和信号测试

直接调用 `LoadRunner.run()` 只能验证核心组件，不能验证命令行入口、退出码、stdout/stderr 分流和真实信号处理。

至少增加：

- 一个命令行端到端测试：运行 `python -m httpload`，验证报告、退出码和数量守恒。
- 一个 POSIX 中断测试：启动长压测子进程，发送 `SIGINT`，验证其在限定时间内退出、返回码为 `130`，并输出 `Interrupted: true`。

内部模拟 `request_stop()` 的测试仍可保留，用来快速验证 runner 的取消逻辑。

### 6. 修正测试稳定性

- 网络错误测试不要假定 `65530` 永远空闲，应在测试中动态取得一个未监听的本地端口。
- 混合分类测试需要一个按请求序号稳定轮换 200、404 和延迟响应的单一路由。
- 测试服务端的 `in_flight -= 1` 必须放在 `finally` 中，避免客户端超时或断开后峰值统计失真。
- 超时和中断测试必须加外层时间限制，防止测试套件死锁。

### 7. 加强 URL 校验

不能只检查 `scheme` 和 `netloc`。还应：

- 仅允许 `http` 和 `https`。
- 确认 hostname 非空。
- 主动读取并校验 port，捕获非法端口异常。
- 拒绝包含空白字符的 URL。

校验失败应在发请求前返回退出码 `2`，而不是在压测过程中统计为网络错误。

### 8. 统一安装和依赖说明

如果需要提供真正的 `httpload` 命令，`pyproject.toml` 就不是可选项。建议在其中统一声明：

- Python 版本要求。
- aiohttp 运行依赖。
- `httpload = "httpload.cli:main"` 命令入口。
- pytest 和 pytest-asyncio 测试依赖。

README 使用 `pip install -e .` 作为主要安装方式，避免 `requirements.txt` 和 `pyproject.toml` 维护两套不一致的依赖。

## 三、功能范围调整

### 第一阶段：必做项，目标 30 分钟内完成

- GET 请求。
- 四个必需参数及校验。
- 固定 worker pool。
- 共享 ClientSession 和有界连接池。
- 单请求超时。
- 四类结果及基础延迟统计。
- Ctrl+C 部分统计和退出码 130。
- 文本报告。
- 至少两个自动化测试。
- README 运行说明和示例命令。

### 第二阶段：核心测试通过后再增加

优先级从高到低：

1. P50/P90/P99。
2. JSON 输出，并保持 stdout 只有 JSON、提示信息写入 stderr。
3. 更多边界和端到端测试。

POST、自定义 Header、body 和实时进度不是题目必做内容。除非前述功能已经完成并验证，否则本次提交不建议实现。实时进度尤其会增加异步任务清理和输出测试复杂度。

## 四、推荐执行顺序

1. 建立最小项目、`pyproject.toml` 和命令入口。
2. 完成参数解析、duration 解析和 URL 校验。
3. 定义 `RunResult`、数量守恒检查和空统计行为。
4. 实现 worker pool、共享 ClientSession、超时和结果分类。
5. 实现 `request_stop()`、信号处理、资源清理和退出码。
6. 实现文本报告。
7. 完成本地测试服务和核心自动化测试。
8. 跑三种端到端场景：200、404、网络错误。
9. 手动和自动验证 Ctrl+C。
10. 核心功能全部通过后，再按剩余时间增加分位数和 JSON。

## 五、最终验收标准

满足以下条件后，方案审查通过：

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

完成上述修改后，可以按修订后的 PLAN 开始实现。
