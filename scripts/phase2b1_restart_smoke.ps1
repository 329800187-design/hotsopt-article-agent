$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location -LiteralPath $Root
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$EvidenceDir = Join-Path $Root "evidence\phase2b1-live"
New-Item -ItemType Directory -Force -Path $EvidenceDir | Out-Null
$before = & $Python -c "from modules.database import get_store; import json; values=get_store().list_batches(); print(json.dumps(next(({'batch_id':b['batch_id'],'status':b['status']} for b in values if b.get('status')=='completed'), {}), ensure_ascii=False))"
$beforeValue = $before | ConvertFrom-Json
if (-not $beforeValue.batch_id) { throw "No completed batch exists before restart smoke." }
$oldNonInteractive = $env:HOTSPOT_NONINTERACTIVE
$oldNoBrowser = $env:HOTSPOT_NO_BROWSER
$env:HOTSPOT_NONINTERACTIVE = "1"
$env:HOTSPOT_NO_BROWSER = "1"
try {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root "launcher.ps1")
    if ($LASTEXITCODE -ne 0) { throw "launcher failed" }
    $response = Invoke-RestMethod -Uri ("http://127.0.0.1:8506/api/batches/" + $beforeValue.batch_id) -Method Get
    if (-not $response.success -or $response.data.status -ne "completed") { throw "completed batch was not restored" }
    $evidence = @{ status = "BATCH_RESTART_RECOVERY_PASS"; batch_id = $beforeValue.batch_id; status_before = $beforeValue.status; status_after = $response.data.status; completed_count = $response.data.completed_count; checked_at = (Get-Date).ToUniversalTime().ToString("o") } | ConvertTo-Json -Depth 4
    [IO.File]::WriteAllText((Join-Path $EvidenceDir "batch_restart_evidence.json"), $evidence + "`n", [Text.UTF8Encoding]::new($false))
} finally {
    & cmd.exe /c (Join-Path $Root "stop.bat") | Out-Host
    if ($null -eq $oldNonInteractive) { Remove-Item Env:HOTSPOT_NONINTERACTIVE -ErrorAction SilentlyContinue } else { $env:HOTSPOT_NONINTERACTIVE = $oldNonInteractive }
    if ($null -eq $oldNoBrowser) { Remove-Item Env:HOTSPOT_NO_BROWSER -ErrorAction SilentlyContinue } else { $env:HOTSPOT_NO_BROWSER = $oldNoBrowser }
}
Write-Host "phase2b1 restart smoke: PASS"
