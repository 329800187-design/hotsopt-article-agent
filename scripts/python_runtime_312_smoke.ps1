$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$temporary = Join-Path $env:TEMP ("python-runtime-312-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $temporary | Out-Null
$fake = Join-Path $temporary "fake_py.cmd"
@"
@echo off
if "%1"=="-3.13" exit /b 1
if "%1"=="-3.12" (
  echo %* | findstr /c:"sys.executable" >nul && echo C:\fake\python312\python.exe
  echo %* | findstr /c:"platform.python_version" >nul && echo 3.12.9
  exit /b 0
)
exit /b 1
"@ | Set-Content -LiteralPath $fake -Encoding ASCII
try {
    . (Join-Path $Root "scripts\python_runtime.ps1")
    $selected = Find-CompatiblePython -LauncherPath $fake
    if ([version]$selected.Version -ne [version]"3.12.9") { throw "3.12 fallback failed: $($selected.Version)" }
    Write-Host ("Python 3.12-only selector: PASS (" + $selected.Version + ")")
} finally {
    Remove-Item -LiteralPath $temporary -Recurse -Force -ErrorAction SilentlyContinue
}
