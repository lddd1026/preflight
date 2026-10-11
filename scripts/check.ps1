<#
.SYNOPSIS
    preflight 提交前全量检查。

.DESCRIPTION
    四步，任何一步失败都继续跑完（好让一次看到全部问题），最后统一报退出码：
      1. 代码风格  ruff check
      2. 格式检查  ruff format --check
      3. 测试      pytest
      4. 类型检查  mypy src

    这是纪律 D1 的执行入口：每个阶段结束都必须跑它，并把真实输出记进
    docs/PROGRESS.md。

.EXAMPLE
    pwsh -File scripts/check.ps1
#>

[CmdletBinding()]
param()

$ErrorActionPreference = 'Continue'

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

$env:PYTHONUTF8 = '1'
$Py = Join-Path $ProjectRoot '.venv\Scripts\python.exe'

if (-not (Test-Path $Py)) {
    Write-Host "找不到 .venv，先跑：pwsh -File scripts/setup.ps1" -ForegroundColor Red
    exit 1
}

$script:Failed = $false

function Step {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][scriptblock]$Body
    )
    Write-Host ""
    Write-Host "=== $Name ===" -ForegroundColor Cyan
    & $Body
    if ($LASTEXITCODE -ne 0) {
        $script:Failed = $true
        Write-Host "  -> 失败 (exit $LASTEXITCODE)" -ForegroundColor Red
    } else {
        Write-Host "  -> 通过" -ForegroundColor Green
    }
}

Step '代码风格 (ruff)'        { & $Py -m ruff check . }
Step '格式检查 (ruff format)' { & $Py -m ruff format --check . }
Step '测试 (pytest)'          { & $Py -m pytest }
Step '类型检查 (mypy)'        { & $Py -m mypy src }

Write-Host ""
if ($script:Failed) {
    Write-Host "验证未通过，先修好再提交。" -ForegroundColor Red
    exit 1
}
Write-Host "全部通过，可以提交了。" -ForegroundColor Green
