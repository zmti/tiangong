# Agent 桌面应用打包脚本
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  Agent 桌面应用打包" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan

# 1. 打包 Python 后端
# 注意:必须用项目 .venv(3.13)自身跑 PyInstaller——依赖也来自同一 .venv;
# 用其他解释器(如 hermes venv 3.11)会导致收集到的 3.13 pyd 与运行时 ABI 不匹配,
# 冻结后报 "DLL load failed while importing _ctypes" 等错误。
Write-Host "`n[1/3] 打包 Python 后端..." -ForegroundColor Yellow
Set-Location ..
& ".\.venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean desktop\backend\agent-backend.spec
if ($LASTEXITCODE -ne 0) {
    Write-Host "[错误] 后端打包失败" -ForegroundColor Red
    exit 1
}
Write-Host "[OK] 后端打包完成" -ForegroundColor Green

# 2. 复制配置
Write-Host "`n[2/3] 复制配置..." -ForegroundColor Yellow
if (Test-Path .env) {
    Copy-Item .env desktop\backend\dist\.env -Force
    Write-Host "[OK] .env 已复制" -ForegroundColor Green
}

# 3. 组装 Electron 绿色版
Write-Host "`n[3/3] 组装 Electron 绿色版..." -ForegroundColor Yellow
Set-Location desktop

$DIST = "dist\win-unpacked"

# 清理旧产物
if (Test-Path $DIST) { Remove-Item $DIST -Recurse -Force }
if (Test-Path "build_app") { Remove-Item "build_app" -Recurse -Force }
if (Test-Path "dist\AgentDesktop-win-x64.zip") { Remove-Item "dist\AgentDesktop-win-x64.zip" -Force }

# 复制 electron 二进制
if (-not (Test-Path "node_modules\electron\dist")) {
    Write-Host "[错误] 缺少 electron 二进制" -ForegroundColor Red
    exit 1
}
Copy-Item "node_modules\electron\dist" $DIST -Recurse -Force

# 重命名主程序
Rename-Item "$DIST\electron.exe" "AgentDesktop.exe" -ErrorAction SilentlyContinue

# 组装 app.asar
New-Item -ItemType Directory -Path "build_app" -Force | Out-Null
Copy-Item "src\*" "build_app" -Recurse -Force
Copy-Item "package.json" "build_app\package.json" -Force
node "node_modules\@electron\asar\bin\asar.js" pack build_app "$DIST\resources\app.asar"
Remove-Item "$DIST\resources\default_app.asar" -ErrorAction SilentlyContinue

# 复制后端 (onedir 布局: backend/agent-backend/agent-backend.exe)
New-Item -ItemType Directory -Path "$DIST\resources\backend" -Force | Out-Null
if (Test-Path "backend\dist\agent-backend\agent-backend.exe") {
    if (Test-Path "$DIST\resources\backend\agent-backend") { Remove-Item "$DIST\resources\backend\agent-backend" -Recurse -Force }
    Copy-Item "backend\dist\agent-backend" "$DIST\resources\backend\agent-backend" -Recurse -Force
} else {
    Copy-Item "backend\dist\agent-backend.exe" "$DIST\resources\backend\agent-backend.exe" -Force
}
if (Test-Path "backend\dist\.env") {
    Copy-Item "backend\dist\.env" "$DIST\resources\backend\.env" -Force
}

# 压缩
Set-Location ..
Compress-Archive -Path "desktop\dist\win-unpacked\*" -DestinationPath "desktop\dist\AgentDesktop-win-x64.zip" -Force

# 清理
Remove-Item "desktop\build_app" -Recurse -Force -ErrorAction SilentlyContinue

Write-Host "`n============================================" -ForegroundColor Cyan
Write-Host "  打包完成!" -ForegroundColor Green
Write-Host "  绿色版: desktop\dist\AgentDesktop-win-x64.zip" -ForegroundColor Green
Write-Host "  解压后运行 AgentDesktop.exe 即可" -ForegroundColor Green
Write-Host "============================================" -ForegroundColor Cyan
