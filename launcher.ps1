$ErrorActionPreference = "Stop"

# Compatibility entry point for shortcuts and existing portable packages.  The
# desktop host owns all backend/window lifecycle and never opens an external URL.
$Root = (Resolve-Path (Split-Path -Parent $MyInvocation.MyCommand.Path)).Path
$ProductName = "热点图文工作台"
$env:HOTSPOT_DATA_ROOT = Join-Path ($env:LOCALAPPDATA) $ProductName
$env:HOTSPOT_NO_BROWSER = "1"
$env:HOTSPOT_DESKTOP = "1"
$env:PYTHONDONTWRITEBYTECODE = "1"

$candidates = @(
    (Join-Path $Root "runtime\pythonw.exe"),
    (Join-Path $Root "runtime\python.exe"),
    (Join-Path $Root ".venv\Scripts\pythonw.exe"),
    (Join-Path $Root ".venv\Scripts\python.exe"),
    (Join-Path $Root "pythonw.exe"),
    (Join-Path $Root "python.exe")
)
$Python = $candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $Python) {
    $Python = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
}
if (-not $Python) {
    $Python = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
}

function Show-StartupFailure([string]$Code) {
    $message = "软件启动失败，请重新启动。`n错误编号：$Code"
    try {
        Add-Type -AssemblyName PresentationFramework
        [System.Windows.MessageBox]::Show($message, "热点图文批量生产工作台", "OK", "Error") | Out-Null
    } catch {
        Write-Error $message
    }
}

try {
    if (-not $Python) { throw "runtime" }
    & $Python (Join-Path $Root "desktop_host.py")
    if ($LASTEXITCODE -ne 0) { throw "desktop host exited" }
} catch {
    Show-StartupFailure "START-001"
    exit 1
}
