$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Package = Get-ChildItem -LiteralPath $Root -Filter "hotspot-article-agent-rc1-3-3-r1-windows.zip" -File | Select-Object -First 1
if (-not $Package) { $Package = Get-ChildItem -LiteralPath $Root -Filter "hotspot-article-agent-rc1-3-3-windows.zip" -File | Select-Object -First 1 }
if (-not $Package) { $Package = Get-ChildItem -LiteralPath $Root -Filter "hotspot-article-agent-rc1-3-2-windows.zip" -File | Select-Object -First 1 }
if (-not $Package) { $Package = Get-ChildItem -LiteralPath $Root -Filter "hotspot-article-agent-rc1-3-1-windows.zip" -File | Select-Object -First 1 }
if (-not $Package) { $Package = Get-ChildItem -LiteralPath $Root -Filter "hotspot-article-agent-rc1-3-windows.zip" -File | Select-Object -First 1 }
if (-not $Package) { throw "RC1.3/RC1.3.1/RC1.3.2/RC1.3.3/RC1.3.3-R1 Windows package not found" }
$Sandbox = Join-Path ([IO.Path]::GetTempPath()) ("rc1-3-portable-" + [guid]::NewGuid().ToString("N"))
$ProgramDir = Join-Path $Sandbox "ProgramDir"
$LocalAppData = Join-Path $Sandbox "LocalAppData"
$ProductName = -join ([char[]](0x70ed,0x70b9,0x56fe,0x6587,0x5de5,0x4f5c,0x53f0))
New-Item -ItemType Directory -Force -Path $ProgramDir,$LocalAppData | Out-Null
Expand-Archive -LiteralPath $Package.FullName -DestinationPath $ProgramDir -Force
Get-ChildItem -LiteralPath $ProgramDir -Recurse -File | ForEach-Object { $_.IsReadOnly = $true }
$env:PATH = "$env:SystemRoot\System32;$env:SystemRoot"
$env:PYTHONHOME = $null
$env:PYTHONPATH = $null
$env:PYTHONDONTWRITEBYTECODE = "1"
$env:LOCALAPPDATA = $LocalAppData
$env:HOTSPOT_DATA_ROOT = Join-Path $LocalAppData $ProductName
$env:HOTSPOT_PROGRAM_DIR = $ProgramDir
$env:HOTSPOT_LOCAL_API_TOKEN = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes([guid]::NewGuid().ToString("N")))
$Headers = @{"X-Hotspot-Token"=$env:HOTSPOT_LOCAL_API_TOKEN}
$Python = Join-Path $ProgramDir "runtime\python.exe"
if (-not (Test-Path -LiteralPath $Python)) { throw "runtime/python.exe is missing" }
& $Python -c 'import sys; assert sys.version_info >= (3, 12); import fastapi, streamlit, PIL, socksio; print(sys.executable); print(sys.version)'
if ($LASTEXITCODE -ne 0) { throw "bundled runtime import failed" }
$api = Start-Process -FilePath $Python -ArgumentList @("-m", "uvicorn", "api:app", "--host", "127.0.0.1", "--port", "8517") -WorkingDirectory $ProgramDir -WindowStyle Hidden -PassThru
$web = Start-Process -FilePath $Python -ArgumentList @("-m", "streamlit", "run", (Join-Path $ProgramDir "app.py"), "--server.address", "127.0.0.1", "--server.headless", "true", "--server.port", "8518", "--browser.gatherUsageStats", "false") -WorkingDirectory $ProgramDir -WindowStyle Hidden -PassThru
try {
    $healthy = $false
    for ($i = 0; $i -lt 30; $i++) { try { if ((Invoke-WebRequest "http://127.0.0.1:8517/api/health" -UseBasicParsing -TimeoutSec 2 -Headers $Headers).StatusCode -eq 200) { $healthy = $true; break } } catch { Start-Sleep -Seconds 1 } }
    if (-not $healthy) { throw "portable API health check failed" }
    $manual = Invoke-RestMethod "http://127.0.0.1:8517/api/topics/manual" -Method Post -ContentType "application/json" -Headers $Headers -Body '{"title":"portable smoke topic","summary":"RC1.3 smoke"}'
    if (-not $manual.success) { throw "manual topic creation failed" }
    if (-not $manual.data.id) { throw "manual topic did not persist" }
    $SmokeScript = Join-Path $Sandbox "write_settings_and_export.py"
    $env:RC132_SAMPLE_VALUE = "rc132-sample-" + [guid]::NewGuid().ToString("N")
    @'
import os
import sys

sys.path.insert(0, os.environ["HOTSPOT_PROGRAM_DIR"])

from modules.config_store import save_settings, settings_path
from modules.credential_store import credential_path
from modules.app_paths import exports_root

sample_value = os.environ["RC132_SAMPLE_VALUE"]
save_settings({
    "app_mode": "production",
    "demo_mode": False,
    "text_profile": {"name": "RC1.3 smoke", "api_key": sample_value},
    "image_profile": {"name": "RC1.3 smoke", "api_key": sample_value},
})
settings_raw = settings_path().read_text(encoding="utf-8")
credential_raw = credential_path().read_bytes()
assert sample_value not in settings_raw
assert sample_value.encode("utf-8") not in credential_raw
path = exports_root() / "rc1-3-portable-smoke.txt"
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text("portable smoke", encoding="utf-8")
print(path)
'@ | Set-Content -LiteralPath $SmokeScript -Encoding ASCII
    & $Python $SmokeScript
    if ($LASTEXITCODE -ne 0) { throw "settings or export write failed" }
    $RoundtripScript = Join-Path $Sandbox "read_settings_roundtrip.py"
    @'
import os
import sys

sys.path.insert(0, os.environ["HOTSPOT_PROGRAM_DIR"])

from modules.config_store import load_settings, save_settings

settings = load_settings()
assert settings["text_profile"]["api_key"] == os.environ["RC132_SAMPLE_VALUE"]
assert settings["image_profile"]["api_key"] == os.environ["RC132_SAMPLE_VALUE"]
settings["text_profile"].update({"clear_api_key": True, "api_key": ""})
settings["image_profile"].update({"clear_api_key": True, "api_key": ""})
save_settings(settings)
'@ | Set-Content -LiteralPath $RoundtripScript -Encoding ASCII
    & $Python $RoundtripScript
    if ($LASTEXITCODE -ne 0) { throw "DPAPI credential roundtrip failed" }
    Write-Output "PORTABLE_LOCALAPPDATA_PASS"
} finally {
    if ($web) { Stop-Process -Id $web.Id -Force -ErrorAction SilentlyContinue }
    if ($api) { Stop-Process -Id $api.Id -Force -ErrorAction SilentlyContinue }
}
$userRoot = Join-Path $LocalAppData $ProductName
foreach ($expected in @("config\settings.json", "data\hotspot_agent.db", "exports\rc1-3-portable-smoke.txt", "logs")) {
    if (-not (Test-Path -LiteralPath (Join-Path $userRoot $expected))) { throw "LocalAppData data missing: $expected" }
}
foreach ($forbidden in @("data", "logs", "user-data", ".venv", "settings.json", "hotspot_agent.db", "exports", "runtime\api.pid", "runtime\web.pid")) {
    if (Test-Path -LiteralPath (Join-Path $ProgramDir $forbidden)) { throw "program directory contains user data: $forbidden" }
}
Write-Output "PORTABLE_LOCALAPPDATA_PASS"


