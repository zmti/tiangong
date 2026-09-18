@echo off
rem chcp 65001: 让控制台按 UTF-8 显示(配合 cli.py 的 _force_utf8_stdio, 解决 GBK 中文乱码)。
rem FORCE_COLOR=1: 启用 cli.py Color.use_color() 的 ANSI 上色, 使 Windows 终端也能彩色。
chcp 65001 >nul
set FORCE_COLOR=1
python "%~dp0run_cli.py" %*
