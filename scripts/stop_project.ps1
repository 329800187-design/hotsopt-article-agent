$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path))).Path
$ProductName = -join ([char[]](0x70ed,0x70b9,0x56fe,0x6587,0x6279,0x91cf,0x751f,0x4ea7,0x5de5,0x4f5c,0x53f0))
$localData = Join-Path $env:LOCALAPPDATA $ProductName
$dataRoot = if ($env:HOTSPOT_DATA_ROOT) { [IO.Path]::GetFullPath($env:HOTSPOT_DATA_ROOT) } else { $localData }
$RuntimeDir = Join-Path $dataRoot "runtime"
$BundledPython = Join-Path $Root "runtime\python.exe"
$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
$ExpectedPython = if (Test-Path $BundledPython) { (Resolve-Path $BundledPython).Path } elseif (Test-Path $VenvPython) { (Resolve-Path $VenvPython).Path } else { "" }
$TokenFile = Join-Path $RuntimeDir "local-api-token.dat"

function Read-Metadata([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    try { return Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json } catch { return $null }
}

function Get-Snapshot([int]$ProcessId) {
    $cim = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $ProcessId) -ErrorAction SilentlyContinue
    if (-not $cim) { return $null }
    $runtime = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    [pscustomobject]@{
        Cim = $cim
        Runtime = $runtime
        ExecutablePath = [string]$cim.ExecutablePath
        CommandLine = [string]$cim.CommandLine
        StartTime = if ($runtime) { $runtime.StartTime.ToUniversalTime() } else { $null }
    }
}

function Test-StartTime([object]$Snapshot, [object]$Metadata) {
    if (-not $Snapshot -or -not $Snapshot.StartTime -or -not $Metadata.process_start_time) { return $false }
    try {
        $expected = ([datetime]::Parse([string]$Metadata.process_start_time)).ToUniversalTime()
        return [math]::Abs(($Snapshot.StartTime - $expected).TotalSeconds) -le 2
    } catch { return $false }
}

function Test-Owned([string]$Kind, [object]$Metadata, [int]$Port) {
    if (-not $Metadata -or [string]$Metadata.project_root -ne $Root -or [int]$Metadata.port -ne $Port) { return $false }
    if ([string]$Metadata.python_path -ne $ExpectedPython -or [string]$Metadata.token_file -ne $TokenFile) { return $false }
    $snapshot = Get-Snapshot ([int]$Metadata.pid)
    if (-not $snapshot -or $snapshot.ExecutablePath -ne $ExpectedPython -or -not (Test-StartTime $snapshot $Metadata)) { return $false }
    if ($snapshot.CommandLine.IndexOf($Root, [StringComparison]::OrdinalIgnoreCase) -lt 0) { return $false }
    if ($Kind -eq "api") {
        if ($snapshot.CommandLine -notmatch "uvicorn.*api:app" -or $snapshot.CommandLine -notmatch ("--port\s+" + $Port)) { return $false }
    } elseif ($snapshot.CommandLine -notmatch "streamlit.*app\.py" -or $snapshot.CommandLine -notmatch ("--server\.port\s+" + $Port)) {
        return $false
    }
    return $true
}

foreach ($item in @(@{Name="api"; Port=8506}, @{Name="web"; Port=8505})) {
    $metadataPath = Join-Path $RuntimeDir ($item.Name + ".pid")
    $metadata = Read-Metadata $metadataPath
    $actualPort = if ($metadata -and [int]$metadata.port -gt 0) { [int]$metadata.port } else { [int]$item.Port }
    if (Test-Owned $item.Name $metadata $actualPort) {
        Stop-Process -Id ([int]$metadata.pid) -Force -ErrorAction SilentlyContinue
        Write-Host ('Stopped owned {0} PID {1}.' -f $item.Name, $metadata.pid)
    } elseif ($metadata) {
        Write-Host ('{0} PID metadata did not match this project; refused to stop it.' -f $item.Name)
    }
    Remove-Item -LiteralPath $metadataPath -Force -ErrorAction SilentlyContinue
}
Remove-Item -LiteralPath $TokenFile -Force -ErrorAction SilentlyContinue
