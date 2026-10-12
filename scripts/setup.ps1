<#
.SYNOPSIS
    preflight 开发环境一键安装。

.DESCRIPTION
    约定：**不在 C 盘安装任何文件。**

    具体做法：
      - 虚拟环境建在项目目录下（.venv），跟随项目所在盘
      - pip 缓存固定指向 e:\pip_cache（复用已有的本地缓存）
      - pip 的临时构建目录指向 <项目父目录>\.tmp，避开 C:\...\Temp
      - 包索引走清华镜像，避免 files.pythonhosted.org 连接不稳

    幂等：可以重复运行。

.EXAMPLE
    pwsh -File scripts/setup.ps1
#>

[CmdletBinding()]
param(
    # pip 缓存目录
    [string]$CacheDir = 'e:\pip_cache',

    # 包索引。想用官方源就传 https://pypi.org/simple
    [string]$IndexUrl = 'https://pypi.tuna.tsinghua.edu.cn/simple',

    # 只装运行时依赖，不装 dev 依赖
    [switch]$NoDev
)

$ErrorActionPreference = 'Stop'

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot
Write-Host "项目根目录: $ProjectRoot" -ForegroundColor Cyan

# ---- 0. 强制 UTF-8 ----
# Windows 上 Python 输出被重定向（管道 / 写文件）时会退回 GBK，中文变乱码。
$env:PYTHONUTF8 = '1'

# ---- 1. 临时目录挪出 C 盘 ----
$TempRoot = Join-Path (Split-Path -Parent $ProjectRoot) '.tmp'
New-Item -ItemType Directory -Force -Path $TempRoot | Out-Null
$env:TEMP = $TempRoot
$env:TMP = $TempRoot
Write-Host "TEMP -> $TempRoot" -ForegroundColor Cyan

# ---- 2. pip 缓存 ----
if (-not (Test-Path $CacheDir)) {
    New-Item -ItemType Directory -Force -Path $CacheDir | Out-Null
}
$env:PIP_CACHE_DIR = $CacheDir
Write-Host "PIP_CACHE_DIR -> $CacheDir" -ForegroundColor Cyan

# ---- 3. 虚拟环境 ----
$VenvPy = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $VenvPy)) {
    Write-Host "创建虚拟环境 .venv ..." -ForegroundColor Yellow
    python -m venv (Join-Path $ProjectRoot '.venv')
} else {
    Write-Host "复用已存在的 .venv" -ForegroundColor Yellow
}

# ---- 4. 把镜像配置写进 venv（site-level 配置，不写 C 盘）----
$TrustedHost = ([Uri]$IndexUrl).Host
$PipIni = @"
[global]
index-url = $IndexUrl
trusted-host = $TrustedHost
cache-dir = $CacheDir
timeout = 60
retries = 10

[install]
upgrade-strategy = only-if-needed
"@
Set-Content -Path (Join-Path $ProjectRoot '.venv\pip.ini') -Value $PipIni -Encoding utf8
Write-Host "已写入 .venv\pip.ini（镜像: $IndexUrl）" -ForegroundColor Cyan

# ---- 5. 安装 ----
Write-Host "`n升级 pip ..." -ForegroundColor Yellow
& $VenvPy -m pip install --upgrade pip

$Spec = if ($NoDev) { '.' } else { '.[dev]' }
Write-Host "`n安装 $Spec ..." -ForegroundColor Yellow
& $VenvPy -m pip install -e $Spec

# 镜像偶尔会对**构建依赖**（hatchling）返回 403 Forbidden —— 那时 pip install -e .
# 直接失败，而报错是一大段 pip 内部输出，用户看不出「换个源就好」。
# 实测这条失败会发生在陌生人敲下的**第一条命令**上，所以自动换官方源重试一次。
if ($LASTEXITCODE -ne 0 -and $IndexUrl -ne 'https://pypi.org/simple') {
    Write-Host "`n镜像安装失败，自动改用官方源重试一次 ..." -ForegroundColor Yellow
    & $VenvPy -m pip install -e $Spec --index-url https://pypi.org/simple
}

if ($LASTEXITCODE -ne 0) {
    Write-Host "`n安装失败（exit $LASTEXITCODE）" -ForegroundColor Red
    Write-Host ""
    Write-Host "可以手动排查：" -ForegroundColor Yellow
    Write-Host "  .\.venv\Scripts\python.exe -m pip install -e `"$Spec`" -v" -ForegroundColor Cyan
    Write-Host "  # 或换源：pwsh -File scripts/setup.ps1 -IndexUrl https://pypi.org/simple" -ForegroundColor Cyan
    exit $LASTEXITCODE
}

# ---- 6. 验证 ----
# 验证块整个包一层 try：以后哪个库改接口，也不要在"装好了"的时候甩一段 traceback
# 出来吓人一跳。
Write-Host "`n=== 验证 ===" -ForegroundColor Green
& $VenvPy -c @"
import importlib.metadata as md

try:
    import preflight
    print('preflight ', preflight.__version__)
except Exception as exc:                      # noqa: BLE001
    print('preflight  导入失败:', exc)

for name in ('pydantic', 'pyyaml', 'typer', 'rich'):
    try:
        print(f'{name:10s}', md.version(name))
    except md.PackageNotFoundError:
        print(f'{name:10s} (未安装)')
"@

Write-Host "`n安装完成。下一步：" -ForegroundColor Green
Write-Host "  .\.venv\Scripts\Activate.ps1     # 激活环境"
Write-Host "  preflight-check list-rules             # 看有哪些规则"
Write-Host "  pwsh -File scripts/check.ps1     # 跑全部检查"
