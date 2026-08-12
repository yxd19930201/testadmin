# -*- coding: utf-8 -*-
<#
.SYNOPSIS
  抓包测效助手 (captrace) 一键打包脚本：安装依赖 → PyInstaller → Inno Setup 安装向导。
.DESCRIPTION
  1. pip 安装运行/打包依赖（含 pywebview 作为 GUI 壳）
  2. PyInstaller 按 build.spec 生成 dist/captrace-gui 单目录包
  3. 调用 ISCC 生成 dist_setup/captrace-setup-<ver>.exe 安装向导
  前置：已安装 Python 3.10+、ISCC (Inno Setup Compiler) 并在 PATH。
#>

$ErrorActionPreference = "Stop"
$ROOT = Split-Path -Parent $MyInvocation.MyCommand.Definition
Push-Location $ROOT

function Step($msg){ Write-Host "`n== $msg ==" -ForegroundColor Cyan }

# 1. 依赖
Step "安装运行/打包依赖"
pip install -e .
pip install pyinstaller pywebview

# 2. PyInstaller
Step "PyInstaller 打包 (build.spec)"
if (Test-Path dist/captrace-gui) { Remove-Item -Recurse -Force dist/captrace-gui }
pyinstaller build.spec --noconfirm --clean

# 3. Inno Setup
Step "生成 Inno Setup 安装向导"
$iscc = Get-Command iscc -ErrorAction SilentlyContinue
if (-not $iscc) {
    # 常见安装路径兜底
    $candidates = @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles}\Inno Setup 6\ISCC.exe",
        "${env:LOCALAPPDATA}\Programs\Inno Setup 6\ISCC.exe"
    )
    foreach ($c in $candidates) { if (Test-Path $c) { $iscc = $c; break } }
}
if (-not $iscc) {
    Write-Host "未找到 ISCC（Inno Setup）。请安装 Inno Setup 6 并将其加入 PATH，再手动执行：" -ForegroundColor Yellow
    Write-Host "  iscc installer.iss" -ForegroundColor Yellow
} else {
    & $iscc installer.iss
    Write-Host "安装包已生成于 dist_setup/" -ForegroundColor Green
}

Pop-Location
Write-Host "`n完成。" -ForegroundColor Green
