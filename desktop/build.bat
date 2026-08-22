@echo off
chcp 65001 >nul
setlocal
cd /d %~dp0

echo ============================================
echo   Agent 桌面应用一键打包(绿色版 ZIP)
echo ============================================

echo.
echo [1/4] 打包 Python 后端...
cd ..\..
call .venv\Scripts\activate.bat
python -m PyInstaller --noconfirm --clean --distpath dist --workpath build_pyinstaller desktop\backend\agent-backend.spec
if errorlevel 1 (
    echo [错误] 后端打包失败
    pause
    exit /b 1
)
copy /y dist\agent-backend\agent-backend.exe desktop\backend\dist\agent-backend\agent-backend.exe >nul 2>&1
if not exist "desktop\backend\dist\agent-backend\agent-backend.exe" (
    if exist "dist\agent-backend" (
        if exist "desktop\backend\dist\agent-backend" rmdir /s /q "desktop\backend\dist\agent-backend"
        xcopy /e /i /y "dist\agent-backend" "desktop\backend\dist\agent-backend" >nul
    )
)
echo [OK] 后端打包完成

echo.
echo [2/4] 复制配置...
copy /y .env desktop\backend\dist\.env >nul 2>&1
if not exist desktop\backend\dist\.env (
    echo [提示] 未找到 .env,请先复制 .env.example 为 .env 并填写模型 Key
)
echo [OK] 配置复制完成

echo.
echo [3/4] 安装前端依赖...
cd desktop
call npm install --ignore-scripts
if errorlevel 1 (
    echo [警告] npm install 失败,请检查网络
)

echo.
echo [4/4] 组装 Electron 绿色版...
set DIST=dist\win-unpacked

REM 清理旧产物,避免残留文件冲突
if exist "%DIST%" rmdir /s /q "%DIST%"
if exist "build_app" rmdir /s /q "build_app"
if exist "dist\AgentDesktop-win-x64.zip" del /q "dist\AgentDesktop-win-x64.zip"

REM 复制 electron 二进制
xcopy /e /i /y "node_modules\electron\dist" "%DIST%" >nul
if errorlevel 1 (
    echo [错误] 缺少 electron 二进制,请确认已手动解压 electron-v28.3.3-win32-x64.zip 到 node_modules\electron\dist
    pause
    exit /b 1
)
REM 重命名主程序(electron 自身不依赖文件名)
ren "%DIST%\electron.exe" AgentDesktop.exe >nul
if errorlevel 1 (
    echo [错误] 重命名 electron.exe 失败
    pause
    exit /b 1
)

REM 组装 app.asar(src + package.json)
mkdir build_app >nul
xcopy /e /i /y "src" "build_app\src" >nul
copy /y "package.json" "build_app\package.json" >nul
node "node_modules\@electron\asar\bin\asar.js" pack build_app "%DIST%\resources\app.asar"
if errorlevel 1 (
    echo [错误] app.asar 打包失败
    pause
    exit /b 1
)
del /q "%DIST%\resources\default_app.asar" >nul 2>&1

REM 复制后端 exe 与配置 (onedir 布局: backend/agent-backend/agent-backend.exe)
mkdir "%DIST%\resources\backend" >nul
if exist "%DIST%\resources\backend\agent-backend" rmdir /s /q "%DIST%\resources\backend\agent-backend"
if exist "backend\dist\agent-backend\agent-backend.exe" (
    xcopy /e /i /y "backend\dist\agent-backend" "%DIST%\resources\backend\agent-backend" >nul
) else (
    copy /y "backend\dist\agent-backend.exe" "%DIST%\resources\backend\agent-backend.exe" >nul
)
copy /y "backend\dist\.env" "%DIST%\resources\backend\.env" >nul 2>&1

REM 用 Python 压缩(只读共享模式,规避 Defender 锁定)
cd ..
..\.venv\Scripts\python.exe -c "import zipfile,os;zipf=zipfile.ZipFile('dist\AgentDesktop-win-x64.zip','w',zipfile.ZIP_DEFLATED,allowZip64=True);[zipf.write(os.path.join(r,f),os.path.relpath(os.path.join(r,f),'dist\\win-unpacked')) for r,d,fs in os.walk('dist\\win-unpacked') for f in fs];zipf.close()"
if errorlevel 1 (
    echo [错误] 打包 ZIP 失败
    pause
    exit /b 1
)
rmdir /s /q build_app >nul 2>&1

echo.
echo ============================================
echo   打包完成!
echo   绿色版: desktop\dist\AgentDesktop-win-x64.zip
echo   解压后运行 AgentDesktop.exe 即可
echo ============================================
pause
