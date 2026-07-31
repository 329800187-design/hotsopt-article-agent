$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Split-Path -Parent $MyInvocation.MyCommand.Path)).Path
$env:HOTSPOT_DATA_ROOT = Join-Path $env:LOCALAPPDATA "热点图文批量生产工作台"
$env:HOTSPOT_LAUNCH_MODE = "source"
$env:HOTSPOT_DESKTOP = "1"
$env:HOTSPOT_NO_BROWSER = "1"
$env:PYTHONDONTWRITEBYTECODE = "1"

$Python = $null
if (Test-Path (Join-Path $Root ".venv\Scripts\python.exe")) {
    $Python = Join-Path $Root ".venv\Scripts\python.exe"
} elseif (Get-Command py.exe -ErrorAction SilentlyContinue) {
    $Python = (Get-Command py.exe).Source
} elseif (Get-Command python.exe -ErrorAction SilentlyContinue) {
    $Python = (Get-Command python.exe).Source
}
if (-not $Python) {
    throw "PYTHON_ENVIRONMENT_MISSING"
}

Write-Host ("launch_mode=source data_root={0}" -f $env:HOTSPOT_DATA_ROOT)
if ([IO.Path]::GetFileName($Python) -ieq "py.exe") {
    & $Python -3 (Join-Path $Root "desktop_host.py")
} else {
    & $Python (Join-Path $Root "desktop_host.py")
}
exit $LASTEXITCODE
