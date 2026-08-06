param(
    [string]$InstallRoot = "",
    [string]$DataRoot = "",
    [switch]$ClearUserData
)

$ErrorActionPreference = "SilentlyContinue"
$currentPid = $PID

function Decode-Utf8Base64([string]$value) {
    return [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($value))
}

$dataDirName = Decode-Utf8Base64 "54Ot54K55Zu+5paH5bel5L2c5Y+w"
$productExeName = Decode-Utf8Base64 "54Ot54K55Zu+5paH5bel5L2c5Y+wLmV4ZQ=="

$installFull = ""
if ($InstallRoot) {
    try { $installFull = [System.IO.Path]::GetFullPath($InstallRoot).TrimEnd('\') } catch { $installFull = "" }
}
if (-not $DataRoot) {
    $DataRoot = Join-Path $env:LOCALAPPDATA $dataDirName
}
$runtimeRoot = Join-Path $DataRoot "runtime"

function Test-UnderInstallRoot([string]$value) {
    if (-not $installFull -or -not $value) { return $false }
    try {
        $full = [System.IO.Path]::GetFullPath($value)
        return $full.StartsWith($installFull, [System.StringComparison]::OrdinalIgnoreCase)
    } catch {
        return $false
    }
}

function Stop-ProductPid([int]$processId) {
    if ($processId -le 0 -or $processId -eq $currentPid) { return }
    try {
        $proc = Get-Process -Id $processId -ErrorAction SilentlyContinue
        if ($proc) {
            Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
        }
    } catch {}
}

foreach ($file in @("desktop.lock", "api.json", "web.json", "api.pid", "web.pid")) {
    $path = Join-Path $runtimeRoot $file
    if ([System.IO.File]::Exists($path)) {
        try {
            $json = Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json
            foreach ($key in @("pid", "main_pid", "api_pid", "web_pid")) {
                if ($json.$key) { Stop-ProductPid ([int]$json.$key) }
            }
        } catch {}
    }
}

$candidates = Get-CimInstance Win32_Process | Where-Object {
    $_.ProcessId -ne $currentPid -and
    ($_.Name -in @($productExeName, "python.exe", "pythonw.exe")) -and
    (
        (Test-UnderInstallRoot $_.ExecutablePath) -or
        ($installFull -and $_.CommandLine -and $_.CommandLine.IndexOf($installFull, [System.StringComparison]::OrdinalIgnoreCase) -ge 0)
    )
}

foreach ($item in $candidates) {
    Stop-ProductPid ([int]$item.ProcessId)
}

Start-Sleep -Milliseconds 500

# Remove the legacy self-made installer entry that pointed Windows Settings to
# an obsolete custom uninstaller. This exact key is owned by this product.
$legacyUninstallKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\HotspotArticleAgent"
if (Test-Path -LiteralPath $legacyUninstallKey) {
    try { Remove-Item -LiteralPath $legacyUninstallKey -Recurse -Force } catch {}
}

if ($ClearUserData -and $DataRoot -and [System.IO.Directory]::Exists($DataRoot)) {
    try {
        $fullData = [System.IO.Path]::GetFullPath($DataRoot)
        $local = [System.IO.Path]::GetFullPath($env:LOCALAPPDATA)
        if ($fullData.StartsWith($local, [System.StringComparison]::OrdinalIgnoreCase) -and $fullData.EndsWith($dataDirName, [System.StringComparison]::OrdinalIgnoreCase)) {
            [System.IO.Directory]::Delete($fullData, $true)
        }
    } catch {}
}
