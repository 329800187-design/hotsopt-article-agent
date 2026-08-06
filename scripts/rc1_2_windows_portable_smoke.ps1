$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Package = Get-ChildItem -LiteralPath $Root -Filter "hotspot-article-agent-rc1-2-windows.zip" -File | Select-Object -First 1
if (-not $Package) { throw "找不到 RC1.2 Windows 运行包" }
$Sandbox = Join-Path ([IO.Path]::GetTempPath()) ("rc1-2-portable-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $Sandbox | Out-Null
Expand-Archive -LiteralPath $Package.FullName -DestinationPath $Sandbox -Force
$env:PATH = "$env:SystemRoot\System32;$env:SystemRoot"
$env:PYTHONHOME = $null
$env:PYTHONPATH = $null
$env:PYTHONDONTWRITEBYTECODE = "1"
$env:HOTSPOT_DATA_ROOT = Join-Path $Sandbox "user-data"
$Python = Join-Path $Sandbox "runtime\python.exe"
if (-not (Test-Path -LiteralPath $Python)) { throw "运行包缺少 runtime/python.exe" }
& $Python -c 'import sys; assert sys.version_info >= (3, 12); assert sys.executable.lower().find(chr(114)+chr(117)+chr(110)+chr(116)+chr(105)+chr(109)+chr(101)) >= 0; import fastapi, streamlit, PIL; print(sys.version)'
if ($LASTEXITCODE -ne 0) { throw "内置 Runtime 导入失败" }
$api = Start-Process -FilePath $Python -ArgumentList @("-m", "uvicorn", "api:app", "--host", "127.0.0.1", "--port", "8516") -WorkingDirectory $Sandbox -WindowStyle Hidden -PassThru
$web = Start-Process -FilePath $Python -ArgumentList @("-m", "streamlit", "run", (Join-Path $Sandbox "app.py"), "--server.address", "127.0.0.1", "--server.headless", "true", "--server.port", "8515", "--browser.gatherUsageStats", "false") -WorkingDirectory $Sandbox -WindowStyle Hidden -PassThru
try {
    $healthy = $false
    for ($i = 0; $i -lt 30; $i++) { try { if ((Invoke-WebRequest "http://127.0.0.1:8516/api/health" -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200) { $healthy = $true; break } } catch { Start-Sleep -Seconds 1 } }
    if (-not $healthy) { throw "便携包 API 健康检查失败" }
    $manual = Invoke-RestMethod "http://127.0.0.1:8516/api/topics/manual" -Method Post -ContentType "application/json" -Body '{"title":"便携包烟测话题","summary":"仅用于 RC1.2 本地烟测"}'
    if (-not $manual.success) { throw "手动话题创建失败" }
    $task = Invoke-RestMethod "http://127.0.0.1:8516/api/tasks" -Method Post -ContentType "application/json" -Body (ConvertTo-Json @{task_name="便携包烟测";mode="multi_topic";topic_ids=@($manual.data.id);article_count=1})
    if (-not $task.success) { throw "任务创建失败" }
    Write-Output "PORTABLE_API_TASK_PASS"
    Write-Output "PORTABLE_STREAMLIT_PID=$($web.Id)"
} finally {
    if ($web) { Stop-Process -Id $web.Id -Force -ErrorAction SilentlyContinue }
    if ($api) { Stop-Process -Id $api.Id -Force -ErrorAction SilentlyContinue }
}
$forbidden = @("data", "logs", ".venv") | Where-Object { Test-Path (Join-Path $Sandbox $_) }
if ($forbidden) { throw ("程序目录出现用户数据目录：" + ($forbidden -join ",")) }
Write-Output "PORTABLE_SMOKE_PASS"
