@echo off
chcp 65001 >nul
setlocal
cd /d %~dp0

echo ============================================
echo   Agent Service 打包脚本
echo ============================================

echo.
echo [1/3] 打包 Python 后端...
python -m PyInstaller --noconfirm --clean --onefile --name agent-backend --distpath dist --workpath build_pyinstaller --specpath . desktop\backend\agent-backend.spec
if errorlevel 1 (
    echo [错误] 后端打包失败
    pause
    exit /b 1
)
echo [OK] 后端打包完成

echo.
echo [2/3] 复制配置文件...
if exist .env (
    copy /y .env dist\.env >nul
    echo [OK] .env 已复制
) else (
    echo [提示] 未找到 .env 文件
)

echo.
echo [3/3] 打包完成!
echo.
echo 输出文件: dist\agent-backend.exe
echo.
echo 使用方法:
echo   1. 复制 dist\agent-backend.exe 到目标目录
echo   2. 复制 .env 文件到同目录(如需要)
echo   3. 运行 agent-backend.exe
echo.
pause
