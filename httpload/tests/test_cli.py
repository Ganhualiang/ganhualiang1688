"""cli 测试：duration/URL 校验单元测试 + 子进程端到端 + POSIX SIGINT。

注意：fixture server 跑在 pytest 进程的事件循环上，子进程调用必须放到
线程（asyncio.to_thread）里执行，否则会阻塞事件循环导致 server 无法响应。
"""

import asyncio
import json
import signal
import subprocess
import sys

import pytest

from httpload.cli import parse_args, parse_duration, parse_header, validate_url

PYTHON = sys.executable


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


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal test")
async def test_cli_sigint_graceful_exit(server):
    proc = subprocess.Popen(
        [PYTHON, "-m", "httpload",
         "--url", server.url("/slow?delay=0.1"),
         "--requests", "100000", "--concurrency", "5", "--timeout", "5s"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    await asyncio.sleep(1.0)  # 等压测真正开始
    proc.send_signal(signal.SIGINT)
    try:
        stdout, stderr = await asyncio.to_thread(proc.communicate, timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        pytest.fail("process did not exit within 10s after SIGINT")
    assert proc.returncode == 130, stderr
    assert "Interrupted:    true" in stdout
    assert "Scheduled:" in stdout
