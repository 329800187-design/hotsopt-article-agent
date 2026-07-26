$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location -LiteralPath $Root
. (Join-Path $Root "scripts\python_runtime.ps1")
$Venv = Join-Path $Root ".venv"
$VenvPython = Join-Path $Venv "Scripts\python.exe"
if (-not (Test-Path -LiteralPath $VenvPython)) {
    $pythonSpec = Find-CompatiblePython
    Write-Host ("Creating venv with Python " + $pythonSpec.Version + " at " + $pythonSpec.Executable)
    & $pythonSpec.Path @($pythonSpec.Args) -m venv $Venv
    if ($LASTEXITCODE -ne 0) { throw ("Failed to create venv with " + $pythonSpec.Version + " at " + $pythonSpec.Executable) }
}
& $VenvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed" }
& $VenvPython -m pip install -r (Join-Path $Root "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "dependency installation failed" }
Write-Host "Installation complete."
