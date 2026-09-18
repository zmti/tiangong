"""验证代码沙箱子进程不继承宿主机敏感环境变量(修复密钥泄露 P0)。

旧实现: subprocess.run(..., env={**os.environ, ...}) 继承全部环境变量,
恶意/误执行代码可 os.environ 读到 DEEPSEEK_API_KEY 等密钥并外传。
新实现: _safe_env() 仅保留最小运行变量, 剥离所有凭据。
"""
import os
import tempfile
import textwrap

from app.tools import code_sandbox


def test_safe_env_strips_secrets():
    """_safe_env 不包含任何可能含凭据的变量。"""
    env = code_sandbox._safe_env()
    # 不应包含常见凭据变量
    for key in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "QWEN_API_KEY",
                "API_KEY", "SECRET", "PASSWORD", "AGENT_API_KEY",
                "GITHUB_TOKEN", "GITLAB_TOKEN"):
        assert key not in env, f"_safe_env 不应包含 {key}"
    # 仅保留白名单中的必要变量
    for key in ("PATH", "PYTHONPATH"):
        assert key in env or key == "PATH"  # PATH 必须存在
    # PYTHONPATH 必须为空(防导入业务代码)
    assert env.get("PYTHONPATH") == ""


def test_subprocess_cannot_read_host_secrets():
    """沙箱 Python 子进程读不到宿主机 DEEPSEEK_API_KEY。"""
    tmp = tempfile.mkdtemp()
    # 临时设置一个宿主机变量
    os.environ["DEEPSEEK_API_KEY"] = "sk-test-secret-12345"
    try:
        code = textwrap.dedent('''
            import os
            v = os.environ.get("DEEPSEEK_API_KEY", "NOT_FOUND")
            print("KEY_IS_" + v)
        ''')
        result = code_sandbox.run_in_subprocess(code, timeout=15, language="python")
        assert result.success
        # 沙箱内读不到宿主机的密钥(因为 _safe_env 已剥离)
        assert "NOT_FOUND" in result.output or "NOT_FOUND" in result.stdout, \
            f"沙箱读到了宿主机环境变量: {result.output[:200]}"
    finally:
        os.environ.pop("DEEPSEEK_API_KEY", None)


def test_subprocess_cannot_read_arbitrary_env():
    """宿主机任意变量都不会泄漏进沙箱。"""
    os.environ["MY_TEST_HOST_VAR"] = "should-not-leak"
    try:
        code = textwrap.dedent('''
            import os
            print("LEAK_CHECK=" + os.environ.get("MY_TEST_HOST_VAR", "ISOLATED"))
        ''')
        result = code_sandbox.run_in_subprocess(code, timeout=15, language="python")
        assert result.success
        assert "ISOLATED" in result.output
    finally:
        os.environ.pop("MY_TEST_HOST_VAR", None)
