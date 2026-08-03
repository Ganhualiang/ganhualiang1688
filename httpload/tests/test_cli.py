"""cli 测试：duration/URL 校验单元测试 + 子进程端到端 + 信号中断（POSIX / Windows）。

注意：fixture server 跑在 pytest 进程的事件循环上，子进程调用必须放到
线程（asyncio.to_thread）里执行，否则会阻塞事件循环导致 server 无法响应。
"""

import asyncio
import json
import os
import signal
import subprocess
import sys

import pytest

from httpload import cli
from httpload.cli import (
    interrupt_guard,
    interrupt_signals,
    parse_args,
    parse_duration,
    parse_header,
    validate_url,
)

PYTHON = sys.executable
IS_WINDOWS = sys.platform == "win32"


# ---------- parse_duration ----------

@pytest.mark.parametrize(
    "raw,expected",
    [("2s", 2.0), ("500ms", 0.5), ("1m", 60.0), ("1.5s", 1.5), ("3", 3.0), ("0.25", 0.25)],
)
def test_parse_duration_valid(raw, expected):
    assert parse_duration(raw) == pytest.approx(expected)


@pytest.mark.parametrize("raw", ["", "abc", "2h", "-1s", "0s", "0", "1 s"])
def test_parse_duration_invalid(raw):
    with pytest.raises(ValueError):
        parse_duration(raw)


# ---------- validate_url ----------

@pytest.mark.parametrize(
    "url",
    ["http://localhost:8080/", "https://example.com/path?q=1", "http://127.0.0.1"],
)
def test_validate_url_valid(url):
    assert validate_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/",      # 非 http/https
        "localhost:8080",          # 无 scheme
        "http://",                 # 无 hostname
        "http://host:99999/",      # 非法端口
        "http://exa mple.com/",    # 含空白
    ],
)
def test_validate_url_invalid(url):
    with pytest.raises(ValueError):
        validate_url(url)


# ---------- parse_args ----------

def test_parse_args_rejects_bad_counts():
    with pytest.raises(ValueError):
        parse_args(["-u", "http://localhost/", "-n", "0"])
    with pytest.raises(ValueError):
        parse_args(["-u", "http://localhost/", "-c", "-1"])


def test_parse_args_downgrades_concurrency(capsys):
    args = parse_args(["-u", "http://localhost/", "-n", "5", "-c", "50"])
    assert args.concurrency == 5
    assert "concurrency reduced" in capsys.readouterr().err


# ---------- parse_header / --method ----------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("X-Token: abc", ("X-Token", "abc")),
        ("Content-Type:application/json", ("Content-Type", "application/json")),
        ("X-Empty:", ("X-Empty", "")),
    ],
)
def test_parse_header_valid(raw, expected):
    assert parse_header(raw) == expected


@pytest.mark.parametrize("raw", ["no-colon", ": no-name", ""])
def test_parse_header_invalid(raw):
    with pytest.raises(ValueError):
        parse_header(raw)


def test_parse_args_method_headers_body():
    args = parse_args(
        ["-u", "http://localhost/", "-X", "post", "-d", "a=1",
         "-H", "X-A: 1", "-H", "X-B: 2"]
    )
    assert args.method == "POST"  # 小写自动归一化
    assert args.body == "a=1"
    assert args.headers == {"X-A": "1", "X-B": "2"}


def test_parse_args_rejects_bad_method():
    with pytest.raises(ValueError):
        parse_args(["-u", "http://localhost/", "-X", "FROB"])


def test_parse_args_rejects_bad_header():
    with pytest.raises(ValueError):
        parse_args(["-u", "http://localhost/", "-H", "no-colon-here"])


# ---------- CLI 子进程端到端 ----------

async def _run_cli(argv: list[str]) -> subprocess.CompletedProcess:
    return await asyncio.to_thread(
        subprocess.run,
        [PYTHON, "-m", "httpload", *argv],
        capture_output=True, text=True, timeout=30,
    )


async def test_cli_end_to_end(server):
    proc = await _run_cli(
        ["--url", server.url("/ok"), "--requests", "20",
         "--concurrency", "4", "--timeout", "2s", "--json"]
    )
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert data["completed"] == 20
    assert data["completed"] == (
        data["succeeded"] + data["non_2xx"] + data["errors"] + data["timeouts"]
    )
    assert data["succeeded"] == 20
    assert data["interrupted"] is False


async def test_cli_text_report(server):
    proc = await _run_cli(
        ["-u", server.url("/status/404"), "-n", "10", "-c", "2", "-t", "1s"]
    )
    assert proc.returncode == 0, proc.stderr
    assert "Non-2xx:        10" in proc.stdout
    assert "Completed:      10" in proc.stdout


async def test_cli_usage_error_exit_2():
    proc = await _run_cli(["--url", "not-a-url", "-n", "10"])
    assert proc.returncode == 2
    assert "error" in proc.stderr


async def test_cli_post_with_header_and_body(server):
    proc = await _run_cli(
        ["-u", server.url("/echo"), "-n", "5", "-c", "2", "-t", "2s",
         "-X", "POST", "-d", "ping", "-H", "X-Token: t1", "--json"]
    )
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert data["method"] == "POST"
    assert data["succeeded"] == 5
    assert server.last_method == "POST"
    assert server.last_body == b"ping"
    assert server.last_headers.get("X-Token") == "t1"


async def test_cli_non_get_shows_method_line(server):
    proc = await _run_cli(
        ["-u", server.url("/echo"), "-n", "3", "-c", "2", "-X", "PUT"]
    )
    assert proc.returncode == 0, proc.stderr
    assert "Method:         PUT" in proc.stdout


async def test_cli_progress_written_to_stderr(server):
    proc = await _run_cli(
        ["-u", server.url("/ok"), "-n", "10", "-c", "2", "--progress"]
    )
    assert proc.returncode == 0, proc.stderr
    # 结束时输出最终进度 completed/planned；进度只写 stderr，stdout 仍是纯报告
    assert "progress: 10/10" in proc.stderr
    assert "progress" not in proc.stdout
    assert "Completed:      10" in proc.stdout


async def test_cli_help_survives_non_utf8_stdout():
    """帮助文本含中文，stdout 编码编不出时也不能失败（英文 Windows 控制台）。"""
    env = {**os.environ, "PYTHONIOENCODING": "ascii"}
    proc = await asyncio.to_thread(
        subprocess.run,
        [PYTHON, "-m", "httpload", "--help"],
        capture_output=True, text=True, timeout=30, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert "--concurrency" in proc.stdout


# ---------- 信号中断：进程内接线 ----------

def test_interrupt_signals_covers_platform():
    sigs = interrupt_signals()
    assert signal.SIGINT in sigs
    # Windows 上 Ctrl+Break（以及发给新进程组的 CTRL_BREAK_EVENT）走 SIGBREAK；
    # SIGBREAK 在 POSIX 上不存在，所以用 getattr 取。
    sigbreak = getattr(signal, "SIGBREAK", None)
    assert (sigbreak is not None and sigbreak in sigs) is IS_WINDOWS


async def test_interrupt_guard_routes_sigint_to_callback():
    """在真实事件循环上验证：SIGINT 触发回调，且退出后不残留自己的处理器。

    覆盖两条平台分支——POSIX 的 loop.add_signal_handler 与 Windows 的
    signal.signal + call_soon_threadsafe。
    """
    original = signal.getsignal(signal.SIGINT)
    fired = asyncio.Event()
    async with interrupt_guard(fired.set):
        signal.raise_signal(signal.SIGINT)
        await asyncio.wait_for(fired.wait(), timeout=5)
    # Windows 分支恢复成进入前的处理器；POSIX 的 remove_signal_handler 固定恢复成
    # default_int_handler。两者都不应留下 httpload 自己的处理器。
    assert signal.getsignal(signal.SIGINT) in (original, signal.default_int_handler)


class _FakeLoop:
    """只记录调用的假事件循环；raises 非空时模拟平台不支持该 API。"""

    def __init__(self, raises: Exception | None = None):
        self.raises = raises
        self.added: list[tuple[int, object]] = []
        self.removed: list[int] = []

    def add_signal_handler(self, sig, cb):
        if self.raises is not None:
            raise self.raises
        self.added.append((sig, cb))

    def remove_signal_handler(self, sig):
        self.removed.append(sig)


def test_install_interrupt_posix_branch_uses_loop_api(monkeypatch):
    """POSIX 分支的接线与撤销逻辑，用假循环在任意平台上验证。"""
    monkeypatch.setattr(cli, "IS_WINDOWS", False)
    loop, cb = _FakeLoop(), lambda: None
    undo = cli._install_interrupt(loop, cb)
    assert loop.added == [(sig, cb) for sig in interrupt_signals()]
    undo()
    assert loop.removed == list(interrupt_signals())


def test_install_interrupt_degrades_when_platform_lacks_api(monkeypatch, capsys):
    """接线失败只警告并降级，不能让整次压测失败——这正是原先 Windows 上的缺陷。

    `add_signal_handler` 在 Windows 抛出的 NotImplementedError 消息为空字符串，
    所以这里用 repr 输出，避免又变成一句没有信息量的报错。
    """
    monkeypatch.setattr(cli, "IS_WINDOWS", False)
    undo = cli._install_interrupt(_FakeLoop(raises=NotImplementedError()), lambda: None)
    undo()  # 撤销空接线也必须安全
    err = capsys.readouterr().err
    assert "warning" in err
    assert "SIGINT" in err
    assert "NotImplementedError()" in err


# ---------- 信号中断：子进程端到端 ----------

async def _interrupt_cli_and_assert(popen_kwargs: dict, send: "signal.Signals", server):
    """启动长压测子进程 → 发送中断信号 → 断言 130 与部分统计。"""
    proc = subprocess.Popen(
        [*popen_kwargs.pop("argv"),
         "--url", server.url("/slow?delay=0.1"),
         "--requests", "100000", "--concurrency", "5", "--timeout", "5s"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **popen_kwargs,
    )
    await asyncio.sleep(1.0)  # 等压测真正开始
    proc.send_signal(send)
    try:
        stdout, stderr = await asyncio.to_thread(proc.communicate, timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        pytest.fail(f"process did not exit within 10s after {send!r}")
    assert proc.returncode == 130, stderr
    assert "Interrupted:    true" in stdout
    assert "Scheduled:" in stdout
    return stdout


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX signal test")
async def test_cli_sigint_graceful_exit(server):
    await _interrupt_cli_and_assert(
        {"argv": [PYTHON, "-m", "httpload"]}, signal.SIGINT, server
    )


# Windows 无法向单个子进程投递控制台事件，只能发给整个进程组，因此子进程必须
# 用 CREATE_NEW_PROCESS_GROUP 独立成组，否则会把 pytest 自己一起中断。
@pytest.mark.skipif(not IS_WINDOWS, reason="Windows console control event test")
async def test_cli_ctrl_break_graceful_exit(server):
    await _interrupt_cli_and_assert(
        {"argv": [PYTHON, "-m", "httpload"],
         "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP},
        signal.CTRL_BREAK_EVENT,
        server,
    )


# CREATE_NEW_PROCESS_GROUP 会顺带屏蔽新组的 Ctrl+C，所以子进程先用
# SetConsoleCtrlHandler(None, False) 把它恢复，才能测到真实的 Ctrl+C 路径。
_REENABLE_CTRL_C_AND_RUN = (
    "import ctypes, runpy; "
    "ctypes.windll.kernel32.SetConsoleCtrlHandler(None, False); "
    "runpy.run_module('httpload', run_name='__main__')"
)


@pytest.mark.skipif(not IS_WINDOWS, reason="Windows console control event test")
async def test_cli_ctrl_c_graceful_exit(server):
    await _interrupt_cli_and_assert(
        {"argv": [PYTHON, "-c", _REENABLE_CTRL_C_AND_RUN],
         "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP},
        signal.CTRL_C_EVENT,
        server,
    )
