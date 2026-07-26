$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Evidence = Join-Path $Root "evidence\phase2a5-live"
$Output = Join-Path $Root "outputs\phase2a5_restart_smoke.log"
$env:HOTSPOT_NO_BROWSER = "1"
$env:HOTSPOT_NONINTERACTIVE = "1"
New-Item -ItemType Directory -Force -Path $Evidence, (Split-Path $Output) | Out-Null

function Invoke-ProjectBatch([string]$Name) {
    $path = Join-Path $Root $Name
    $child = Start-Process -FilePath "cmd.exe" -ArgumentList @("/d", "/c", "`"$path`"") -WorkingDirectory $Root -PassThru -WindowStyle Hidden
    $finished = $child.WaitForExit(20000)
    if (-not $finished) {
        Stop-Process -Id $child.Id -Force -ErrorAction SilentlyContinue
        if ($Name -eq "start.bat" -and (-not (Test-Listening 8505) -or -not (Test-Listening 8506))) { throw "$Name timed out before services were ready" }
        if ($Name -eq "stop.bat" -and ((Test-Listening 8505) -or (Test-Listening 8506))) { throw "$Name timed out and services remain running" }
    } elseif ($child.ExitCode -ne 0) {
        throw "$Name failed with exit code $($child.ExitCode)"
    }
}

function Test-Listening([int]$Port) {
    return [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
}

try {
    if (-not (Test-Listening 8506)) { Invoke-ProjectBatch "start.bat" }
    $initialReady = $false
    for ($i = 0; $i -lt 30; $i++) {
        try { if ((Invoke-RestMethod "http://127.0.0.1:8506/api/health").success) { $initialReady = $true; break } } catch { Start-Sleep -Seconds 1 }
    }
    if (-not $initialReady) { throw "initial API startup failed" }
    $before = Invoke-RestMethod "http://127.0.0.1:8506/api/tasks"
    $completed = @($before.data.items | Where-Object { $_.status -eq "completed" }) | Select-Object -First 1
    if (-not $completed) { throw "no completed task available before restart" }

    Invoke-ProjectBatch "stop.bat"
    Start-Sleep -Seconds 2
    $stopped = (-not (Test-Listening 8505)) -and (-not (Test-Listening 8506)) -and (-not (Test-Path (Join-Path $Root "data\runtime\api.pid"))) -and (-not (Test-Path (Join-Path $Root "data\runtime\web.pid")))
    if (-not $stopped) { throw "project processes were not fully stopped" }

    Invoke-ProjectBatch "start.bat"
    $health = $null
    for ($i = 0; $i -lt 30; $i++) {
        try { $health = Invoke-RestMethod "http://127.0.0.1:8506/api/health"; break } catch { Start-Sleep -Seconds 1 }
    }
    if (-not $health -or -not $health.success) { throw "health check failed after restart" }
    $recovered = Invoke-RestMethod ("http://127.0.0.1:8506/api/tasks/{0}/result" -f $completed.task_id)
    if (-not $recovered.success -or $recovered.data.status -ne "completed") { throw "completed task was not recovered" }
    $topics = Invoke-RestMethod "http://127.0.0.1:8506/api/hotspots"
    $topic = @($topics.data.items) | Select-Object -First 1
    if (-not $topic) { throw "no topic available to create post-restart task" }
    $payload = @{ task_name = "2A.5 post-restart persistence check"; mode = "multi_topic"; topic_ids = @($topic.id); article_count = 1 } | ConvertTo-Json -Depth 6
    $newTask = Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8506/api/tasks" -ContentType "application/json" -Body $payload
    $shortcut = Test-Path (Join-Path $Root "Hotspot Article Agent.lnk")
    $resultEvidence = [ordered]@{
        status = "RESTART_RECOVERY_PASS"
        stopped_project_processes = $stopped
        health_after_restart = $health.data
        recovered_task_id = $completed.task_id
        recovered_status = $recovered.data.status
        history_count_after_restart = @((Invoke-RestMethod "http://127.0.0.1:8506/api/tasks").data.items).Count
        created_post_restart_task = $newTask.success
        post_restart_task_id = $newTask.data.task_id
        shortcut_exists = $shortcut
    }
    $jsonText = $resultEvidence | ConvertTo-Json -Depth 8
    [System.IO.File]::WriteAllText((Join-Path $Evidence "restart_evidence.json"), $jsonText + "`n", (New-Object System.Text.UTF8Encoding($false)))
    $resultEvidence | ConvertTo-Json -Depth 8
    exit 0
} catch {
    $failure = [ordered]@{ status = "RESTART_RECOVERY_FAILED"; error = $_.Exception.Message }
    $jsonText = $failure | ConvertTo-Json
    [System.IO.File]::WriteAllText((Join-Path $Evidence "restart_evidence.json"), $jsonText + "`n", (New-Object System.Text.UTF8Encoding($false)))
    $failure | ConvertTo-Json
    exit 1
}
