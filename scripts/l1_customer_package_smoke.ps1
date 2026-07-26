$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$zip = Join-Path $root "hotspot-article-agent-l1-rc1-2-3-windows.zip"
if (-not (Test-Path -LiteralPath $zip)) { throw "Windows package not found" }
$temp = Join-Path ([System.IO.Path]::GetTempPath()) ("l1-customer-smoke-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $temp | Out-Null
try {
    Expand-Archive -LiteralPath $zip -DestinationPath $temp
    $python = Join-Path $temp "runtime\python.exe"
    if (-not (Test-Path -LiteralPath $python)) { throw "Bundled runtime not found" }
    $env:HOTSPOT_DATA_ROOT = Join-Path $temp "user-data"
    $env:PYTHONHOME = Join-Path $temp "runtime"
    $env:PYTHONPATH = "$temp;$temp\runtime\Lib\site-packages"
    $code = @'
import cryptography
import multipart
from modules import license_service
status = license_service.check_license()
assert isinstance(status, dict) and "valid" in status
print("WINDOWS_RUNTIME_LICENSE_IMPORT_PASS")
'@
    $script = Join-Path $temp "customer_smoke.py"
    [System.IO.File]::WriteAllText($script, $code, (New-Object System.Text.UTF8Encoding($false)))
    & $python $script
    if ($LASTEXITCODE -ne 0) { throw "customer package smoke failed" }
} finally {
    Remove-Item -LiteralPath $temp -Recurse -Force -ErrorAction SilentlyContinue
}
