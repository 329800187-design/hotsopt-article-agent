$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location -LiteralPath $Root
$Python = Join-Path $Root ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $Python)) { throw "Missing .venv Python: $Python" }

Write-Host "[1/5] compileall"
& $Python -m compileall -q .
if ($LASTEXITCODE -ne 0) { throw "compileall failed" }

Write-Host "[1b/5] Python selector"
. (Join-Path $Root "scripts\python_runtime.ps1")
$pythonSpec = Find-CompatiblePython
if ([version]$pythonSpec.Version -lt [version]"3.11") { throw "Selected Python is too old: $($pythonSpec.Version)" }
Write-Host ("Selected Python " + $pythonSpec.Version + " at " + $pythonSpec.Executable)
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root "scripts\python_runtime_312_smoke.ps1")
if ($LASTEXITCODE -ne 0) { throw "Python 3.12-only selector smoke failed" }

Write-Host "[2/5] pytest"
& $Python -m pytest -q -p no:cacheprovider
if ($LASTEXITCODE -ne 0) { throw "pytest failed" }

Write-Host "[3/5] API route smoke"
& $Python (Join-Path $Root "scripts\phase1_smoke_test.py")
if ($LASTEXITCODE -ne 0) { throw "phase1_smoke_test.py failed" }

Write-Host "[4/5] launcher startup"
$oldNonInteractive = $env:HOTSPOT_NONINTERACTIVE
$oldNoBrowser = $env:HOTSPOT_NO_BROWSER
$env:HOTSPOT_NONINTERACTIVE = "1"
$env:HOTSPOT_NO_BROWSER = "1"
try {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root "launcher.ps1")
    if ($LASTEXITCODE -ne 0) { throw "launcher.ps1 failed" }
    $health = Invoke-WebRequest -Uri "http://127.0.0.1:8506/api/health" -UseBasicParsing -TimeoutSec 10
    if ($health.StatusCode -ne 200) { throw "health check returned $($health.StatusCode)" }
    $web = Invoke-WebRequest -Uri "http://127.0.0.1:8505" -UseBasicParsing -TimeoutSec 10
    if ($web.StatusCode -ne 200) { throw "web check returned $($web.StatusCode)" }
} finally {
    & cmd.exe /c (Join-Path $Root "stop.bat") | Out-Host
    if ($null -eq $oldNonInteractive) { Remove-Item Env:HOTSPOT_NONINTERACTIVE -ErrorAction SilentlyContinue } else { $env:HOTSPOT_NONINTERACTIVE = $oldNonInteractive }
    if ($null -eq $oldNoBrowser) { Remove-Item Env:HOTSPOT_NO_BROWSER -ErrorAction SilentlyContinue } else { $env:HOTSPOT_NO_BROWSER = $oldNoBrowser }
}

Write-Host "[5/5] stale PID safety"
$runtime = Join-Path $Root "data\runtime"
New-Item -ItemType Directory -Force -Path $runtime | Out-Null
@{ pid = $PID; project_root = $Root; started_at = (Get-Date -Format o); command_line = "unrelated process"; port = 8506 } | ConvertTo-Json -Compress | Set-Content -LiteralPath (Join-Path $runtime "api.pid") -Encoding UTF8
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root "scripts\stop_project.ps1") | Out-Host
if (-not (Get-Process -Id $PID -ErrorAction SilentlyContinue)) { throw "stale PID safety check terminated the smoke process" }

Write-Host "phase1 windows smoke: PASS"
