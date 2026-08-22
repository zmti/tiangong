@echo off
chcp 65001 >nul
setlocal
cd /d %~dp0\..

echo ============================================
echo   Agent 后端打包到 desktop
echo ============================================

echo.
echo [1/2] 打包 Python 后端...
python -m PyInstaller --noconfirm --clean --onefile --name agent-backend --distpath desktop\backend\dist --workpath build --specpath desktop\backend desktop\backend\agent-backend.spec
if errorlevel 1 (
    echo [错误] 后端打包失败
    pause
    exit /b 1
)
echo [OK] 后端打包完成

echo.
echo [2/2] 复制配置文件...
if exist .env (
    copy /y .env desktop\backend\dist\.env >nul
    echo [OK] .env 已复制
) else if exist .env.example (
    copy /y .env.example desktop\backend\dist\.env >nul
    echo [OK] .env.example 已复制为 .env
)

echo.
echo ============================================
echo   打包完成!
echo   输出: desktop\backend\dist\agent-backend.exe
echo ============================================
pause
