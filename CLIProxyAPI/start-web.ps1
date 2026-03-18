param(
    [switch]$SkipUpdate,
    [switch]$NoBrowser
)

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
chcp 65001 | Out-Null

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$exeDir = Join-Path $root "dist"
$exePath = Join-Path $exeDir "CLIProxyAPI-web.exe"
$configPath = Join-Path $root "config.yaml"

function Write-Step($step, $msg) { Write-Host "`n[$step] $msg" -ForegroundColor Yellow }
function Write-Ok($msg) { Write-Host "  $msg" -ForegroundColor Green }
function Write-Info($msg) { Write-Host "  $msg" -ForegroundColor Gray }

function Ensure-Tool($name) {
    if (-not (Get-Command $name -ErrorAction SilentlyContinue)) {
        Write-Host "错误: 未找到 $name，请先安装后重试" -ForegroundColor Red
        exit 1
    }
}

function Get-PortFromConfig($path) {
    if (-not (Test-Path $path)) { return 8317 }
    $content = Get-Content $path -Raw
    if (
        $content -match '(?m)^\s*port\s*:\s*"([0-9]+)"(?:\s+#.*)?\s*$' -or
        $content -match "(?m)^\s*port\s*:\s*'([0-9]+)'(?:\s+#.*)?\s*$" -or
        $content -match '(?m)^\s*port\s*:\s*([0-9]+)(?:\s+#.*)?\s*$'
    ) {
        return [int]$Matches[1]
    }
    return 8317
}

Write-Host "=== CLIProxyAPI Web 版一键启动 ===" -ForegroundColor Cyan
Write-Info "项目目录: $root"

Ensure-Tool "git"
Ensure-Tool "go"
Write-Ok "环境检查通过 (git, go)"

Set-Location $root

if (-not $SkipUpdate) {
    Write-Step "1/4" "拉取并合并最新代码"
    $dirty = git status --porcelain
    if ($dirty) {
        Write-Info "检测到本地未提交改动，跳过自动更新"
    } else {
        git fetch origin main --tags --progress
        if ($LASTEXITCODE -ne 0) {
            Write-Host "  git fetch 失败 (exit code: $LASTEXITCODE)" -ForegroundColor Red
            exit 1
        }
        $behind = git rev-list --count HEAD..origin/main
        if ([int]$behind -gt 0) {
            git merge --ff-only origin/main
            if ($LASTEXITCODE -ne 0) {
                Write-Host "  自动更新失败：无法 fast-forward 合并，请先手动处理分支差异" -ForegroundColor Red
                exit 1
            }
            Write-Ok "已更新到最新代码"
        } else {
            Write-Ok "当前已是最新代码"
        }
    }
} else {
    Write-Step "1/4" "跳过更新"
    Write-Info "已启用 -SkipUpdate"
}

Write-Step "2/4" "准备配置文件"
if (-not (Test-Path $configPath)) {
    $examplePath = Join-Path $root "config.example.yaml"
    if (-not (Test-Path $examplePath)) {
        Write-Host "错误: 未找到 config.example.yaml" -ForegroundColor Red
        exit 1
    }
    Copy-Item -Path $examplePath -Destination $configPath -Force
    Write-Ok "已创建 config.yaml，请按需填写 key 后重启"
} else {
    Write-Ok "检测到现有 config.yaml"
}

Write-Step "3/4" "构建 Web 版可执行文件"
if (-not (Test-Path $exeDir)) {
    New-Item -ItemType Directory -Path $exeDir -Force | Out-Null
}

$gitVersion = git describe --tags --always 2>$null
if (-not $gitVersion) { $gitVersion = "dev" }
$gitCommit = git rev-parse --short HEAD 2>$null
if (-not $gitCommit) { $gitCommit = "none" }
$buildDate = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
$ldflags = "-X 'main.Version=$gitVersion' -X 'main.Commit=$gitCommit' -X 'main.BuildDate=$buildDate'"

go build -trimpath -ldflags "$ldflags" -o "$exePath" ./cmd/server
if ($LASTEXITCODE -ne 0) {
    Write-Host "构建失败" -ForegroundColor Red
    exit 1
}
Write-Ok "构建完成: $exePath"

Write-Step "4/4" "启动 Web 服务"
$port = Get-PortFromConfig -path $configPath
$url = "http://127.0.0.1:$port/management.html"
if (-not $NoBrowser) {
    Start-Process $url | Out-Null
    Write-Info "已打开管理页: $url"
} else {
    Write-Info "管理页地址: $url"
}
Write-Info "后端管理页会在运行时自动检查并更新 management.html"
Write-Host ""
Write-Host "按 Ctrl+C 可停止服务" -ForegroundColor Cyan
& $exePath -config "$configPath"
