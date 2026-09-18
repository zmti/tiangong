"""验证 run_terminal 命令拦截增强(修复编码/解释器嵌套绕过)。"""
import os

from app.tools.terminal_tool import _blocked_reason


def test_basic_blocked_commands():
    """基础危险命令仍被拦截。"""
    assert _blocked_reason("rm -rf /")
    assert _blocked_reason("shutdown now")
    assert _blocked_reason("mkfs /dev/sda")


def test_interpreter_nested_execution_blocked():
    """python -c / node -e / base64 解码执行等嵌套绕过被拦截。"""
    # python -c 嵌套: 可绕过 blacklist 中 "rm -rf /" 子串检测
    assert _blocked_reason('python -c "import shutil; shutil.rmtree(\'/\')"')
    assert _blocked_reason("python3 -c 'print(1)'")
    # node -e
    assert _blocked_reason("node -e 'require(\"fs\").rmSync(\"/\", {recursive: true})'")
    assert _blocked_reason("node --eval 'console.log(1)'")
    # base64 解码执行
    assert _blocked_reason("echo aWQ= | base64 -d | sh")
    assert _blocked_reason("certutil -decode payload.txt out.exe")
    # powershell 编码命令
    assert _blocked_reason("powershell -enc AAAABAAAAA==")


def test_shell_metachar_injection():
    """shell 元字符组合注入被拦截。"""
    # 多个分隔符 = 注入尝试
    assert _blocked_reason("ls; rm -rf /; echo hi")
    # 重定向到系统文件
    assert _blocked_reason("echo evil > /etc/passwd")


def test_safe_commands_pass():
    """合法命令不被误拦截(注意: 解释器嵌套执行 python -c 一律拦截)。"""
    assert _blocked_reason("echo hello world") == ""
    assert _blocked_reason("ls -la") == ""
    assert _blocked_reason("python main.py") == ""  # 不带 -c 的解释器调用合法(运行脚本)
    assert _blocked_reason("npm test") == ""
    assert _blocked_reason("git status") == ""


def test_pipe_with_echo_allowed():
    """管道用于 echo/printf/grep 时合法。"""
    assert _blocked_reason("echo hi | grep o") == ""
    assert _blocked_reason("printf '%s\\n' a | tr a b") == ""
