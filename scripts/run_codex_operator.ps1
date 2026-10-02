# Codex content operator runner.
#
# Invoked by the scheduler (app/services/scheduler.py):
#   powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\run_codex_operator.ps1 [-AllowUnrestrictedCodex]
#
# One round per wake-up. A lock file guards against overlapping 30-minute wake-ups.
# The operator is disabled while data\codex-operator.disabled exists.
#
# Rebuilt 2026-10-03: the original scripts/run_codex_operator.ps1 was deleted by mistake
# and restored from the documented contract in docs/codex-operator.md plus the scheduler
# call site. ASCII only on purpose, so PowerShell 5.1 always reads it correctly.

param(
    [switch]$AllowUnrestrictedCodex
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

$LockFile = Join-Path $ProjectRoot 'data\codex-operator.lock'
$DisabledMarker = Join-Path $ProjectRoot 'data\codex-operator.disabled'
$LogDir = Join-Path $ProjectRoot 'data\logs'
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }
$LogFile = Join-Path $LogDir ('codex-operator-{0}.log' -f (Get-Date -Format 'yyyyMMdd'))

function Write-Log($message) {
    $line = '[{0}] {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $message
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
}

if (Test-Path $DisabledMarker) {
    Write-Log 'operator disabled marker present, skip this round.'
    exit 0
}

if (Test-Path $LockFile) {
    $age = (Get-Date) - (Get-Item $LockFile).LastWriteTime
    if ($age.TotalHours -lt 6) {
        Write-Log 'another operator round is still running, skip.'
        exit 0
    }
    Write-Log 'stale lock removed.'
    Remove-Item $LockFile -Force
}
New-Item -ItemType File -Path $LockFile -Force | Out-Null

try {
    $env:CODEX_HOME = Join-Path $ProjectRoot 'data\codex-home'
    $prompt = 'Read docs/codex-operator.md and run exactly one operator round. Use scripts/codex_operator_bridge.py for every local HTTP action.'

    $codexArgs = @(
        'exec',
        '--config', (Join-Path $ProjectRoot 'config\codex-operator.toml'),
        '--cd', $ProjectRoot
    )
    if ($AllowUnrestrictedCodex) {
        $codexArgs += @('--dangerously-bypass-approvals-and-sandbox', $prompt)
    } else {
        $codexArgs += @('--approve-for-me', $prompt)
    }

    Write-Log ('running: codex ' + ($codexArgs -join ' '))
    & codex @codexArgs 2>&1 | ForEach-Object { Add-Content -Path $LogFile -Value $_ -Encoding UTF8 }
    Write-Log ('operator round finished, exit code ' + $LASTEXITCODE)
}
catch {
    Write-Log ('operator round failed: ' + $_.Exception.Message)
}
finally {
    if (Test-Path $LockFile) { Remove-Item $LockFile -Force }
}
