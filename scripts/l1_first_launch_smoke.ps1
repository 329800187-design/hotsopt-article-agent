$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$zip = Join-Path $root "hotspot-article-agent-l1-rc1-2-3-windows.zip"
if (-not (Test-Path -LiteralPath $zip)) { throw "RC1.2.3 Windows package not found" }
$temp = Join-Path ([System.IO.Path]::GetTempPath()) ("l1-first-launch-" + [guid]::NewGuid().ToString("N"))
$dataRoot = Join-Path $temp "LocalAppData\HotspotArticleAgent"
New-Item -ItemType Directory -Force -Path $temp | Out-Null
$apiProcess = $null
$webProcess = $null
try {
    Expand-Archive -LiteralPath $zip -DestinationPath $temp
    $python = Join-Path $temp "runtime\python.exe"
    if (-not (Test-Path -LiteralPath $python)) { throw "Bundled runtime not found" }
    $env:HOTSPOT_DATA_ROOT = $dataRoot
    $env:PYTHONHOME = Join-Path $temp "runtime"
    $sitePackages = Join-Path $temp "runtime\Lib\site-packages"
    $env:PYTHONPATH = "$temp;$sitePackages"
    $env:PYTHONDONTWRITEBYTECODE = "1"
    $token = (& $python -c 'from modules.local_api_token import get_or_create_token; print(get_or_create_token())').Trim()
    if ([string]::IsNullOrWhiteSpace($token)) { throw "Local API token initialization failed" }
    $env:HOTSPOT_LOCAL_API_TOKEN = $token
    $apiLog = Join-Path $temp "api.log"
    $apiError = Join-Path $temp "api.error.log"
    $webLog = Join-Path $temp "web.log"
    $webError = Join-Path $temp "web.error.log"

    function Start-WorkbenchProcesses {
        $script:apiProcess = Start-Process -FilePath $python -ArgumentList @("-m", "uvicorn", "api:app", "--host", "127.0.0.1", "--port", "8506") -WorkingDirectory $temp -WindowStyle Hidden -RedirectStandardOutput $apiLog -RedirectStandardError $apiError -PassThru
        $script:webProcess = Start-Process -FilePath $python -ArgumentList @("-m", "streamlit", "run", (Join-Path $temp "app.py"), "--server.address", "127.0.0.1", "--server.headless", "true", "--server.port", "8505", "--browser.gatherUsageStats", "false") -WorkingDirectory $temp -WindowStyle Hidden -RedirectStandardOutput $webLog -RedirectStandardError $webError -PassThru
    }

    function Stop-WorkbenchProcesses {
        if ($script:webProcess -and -not $script:webProcess.HasExited) { Stop-Process -Id $script:webProcess.Id -Force -ErrorAction SilentlyContinue }
        if ($script:apiProcess -and -not $script:apiProcess.HasExited) { Stop-Process -Id $script:apiProcess.Id -Force -ErrorAction SilentlyContinue }
        $script:webProcess = $null
        $script:apiProcess = $null
    }

    function Get-LicenseStatus {
        for ($attempt = 0; $attempt -lt 30; $attempt++) {
            try {
                $response = Invoke-WebRequest -Uri "http://127.0.0.1:8506/api/license/status" -UseBasicParsing -TimeoutSec 2 -Headers @{"X-Hotspot-Token"=$env:HOTSPOT_LOCAL_API_TOKEN}
                if ($response.StatusCode -eq 200) { return ($response.Content | ConvertFrom-Json) }
            } catch { Start-Sleep -Milliseconds 500 }
        }
        throw "license status API did not start"
    }

    function Wait-Web {
        for ($attempt = 0; $attempt -lt 40; $attempt++) {
            try {
                $response = Invoke-WebRequest -Uri "http://127.0.0.1:8505/" -UseBasicParsing -TimeoutSec 2
                if ($response.StatusCode -eq 200) { return }
            } catch { Start-Sleep -Milliseconds 500 }
        }
        throw "Streamlit UI did not start"
    }

    Start-WorkbenchProcesses
    $first = Get-LicenseStatus
    Wait-Web
    if ($first.data.code -ne "LICENSE_REQUIRED") { throw "first launch status was $($first.data.code)" }
    $firstCode = [string]$first.data.device_code
    if ($firstCode -notmatch '^[A-Z2-7]{4}(-[A-Z2-7]{4}){4}$') { throw "invalid first device code" }
    if (-not (Test-Path -LiteralPath (Join-Path $dataRoot "license\installation.json"))) { throw "installation.json was not created" }
    if (-not (Test-Path -LiteralPath (Join-Path $dataRoot "license\installation.dat"))) { throw "installation.dat was not created" }
    if ($first.data.code -eq "INSTALLATION_ID_MISSING") { throw "first launch was incorrectly marked missing" }
    Stop-WorkbenchProcesses

    Start-WorkbenchProcesses
    $restart = Get-LicenseStatus
    Wait-Web
    if ([string]$restart.data.device_code -ne $firstCode) { throw "device code changed after restart" }
    Stop-WorkbenchProcesses

    [IO.Directory]::Delete($dataRoot, $true)
    Start-WorkbenchProcesses
    $clean = Get-LicenseStatus
    Wait-Web
    if ($clean.data.code -ne "LICENSE_REQUIRED") { throw "clean relaunch status was $($clean.data.code)" }
    if ([string]$clean.data.device_code -notmatch '^[A-Z2-7]{4}(-[A-Z2-7]{4}){4}$') { throw "clean relaunch device code invalid" }
    if ($clean.data.code -eq "INSTALLATION_ID_MISSING") { throw "clean relaunch was incorrectly marked missing" }
    Write-Output "FIRST_LAUNCH_DEVICE_CODE_PASS"
} finally {
    try { Stop-WorkbenchProcesses } catch { }
    Start-Sleep -Milliseconds 500
    if (Test-Path -LiteralPath $temp) {
        try { [IO.Directory]::Delete($temp, $true) } catch { }
    }
}
