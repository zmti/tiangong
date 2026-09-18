"""天工智能体 CLI 入口(统一转发到根目录 cli.py)。

用法:
    python run_cli.py "你好"
    python run_cli.py --interactive

说明: 本文件仅作"薄壳转发", 实际逻辑全部在根目录 cli.py (功能完整)。
Makefile / run.sh / run_cli.bat 都走这里, 因此它们获得的是同一套 CLI。
- 旧版 app/cli.py 仍可通过 `python -m app.cli` 直接调用(保留兼容),
  但不再作为本转发链的目标。
"""
import sys
from pathlib import Path

# 确保项目根目录在 sys.path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

import asyncio

from cli import parse_args, main as cli_main

if __name__ == "__main__":
    args = parse_args()
    from cli import TGCli
    cli = TGCli(args)
    asyncio.run(cli.run())
