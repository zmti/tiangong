"""PyInstaller 打包入口:带 --port/--host 参数的 uvicorn 启动器。

配置加载顺序:
1. exe 同目录的 .env(优先,用户可编辑)
2. 默认配置
"""
from __future__ import annotations

import argparse
import io
import os
import sys


class _NullWriter:
    """哑对象:替代无控制台窗口时为 None 的 stdout/stderr。"""

    def __init__(self):
        self._buf = io.StringIO()

    def isatty(self) -> bool:
        return False

    def write(self, s) -> int:
        return self._buf.write(str(s))

    def flush(self) -> None:
        pass

    def fileno(self):
        return -1

    def writelines(self, lines):
        for line in lines:
            self.write(line)


def _ensure_streams():
    """无控制台窗口打包时 sys.stdout/stderr 为 None,替换为哑对象避免 uvicorn 崩溃。"""
    if sys.stdout is None:
        sys.stdout = _NullWriter()
    if sys.stderr is None:
        sys.stderr = _NullWriter()


def main():
    _ensure_streams()

    parser = argparse.ArgumentParser(description="Agent 后端服务")
    parser.add_argument("--port", type=int, default=8517)
    parser.add_argument("--host", type=str, default="127.0.0.1")
    args = parser.parse_args()

    # onedir:exe 所在目录;onefile:原始 exe 所在目录(env 均放在 exe 同级)
    if getattr(sys, "frozen", False):
        base_dir = os.path.dirname(os.path.abspath(sys.executable))
    else:
        base_dir = os.path.dirname(os.path.abspath(__file__))

    # 让 pydantic-settings 找到 exe 旁的 .env(onedir 回退到父目录找)
    env_file = None
    for p in (os.path.join(base_dir, ".env"), os.path.join(os.path.dirname(base_dir), ".env")):
        if os.path.exists(p):
            env_file = p
            break
    if env_file:
        os.environ["AGENT_ENV_FILE"] = env_file
        os.chdir(base_dir)

    # 让图片工具等拼本地 URL 时使用实际监听地址(env 优先级高于 .env)
    os.environ["AGENT_HOST"] = args.host
    os.environ["AGENT_PORT"] = str(args.port)

    import uvicorn
    from app.main import app

    sys.path.insert(0, base_dir)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
