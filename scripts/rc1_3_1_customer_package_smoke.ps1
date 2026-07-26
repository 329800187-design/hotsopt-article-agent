$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Candidates = @(
    "hotspot-article-agent-rc1-3-3-r1-windows.zip",
    "hotspot-article-agent-rc1-3-3-windows.zip",
    "hotspot-article-agent-rc1-3-2-windows.zip",
    "hotspot-article-agent-rc1-3-1-windows.zip",
    "hotspot-article-agent-rc1-3-windows.zip"
)
$ZipPath = $null
foreach ($Candidate in $Candidates) {
    $Path = Join-Path $Root $Candidate
    if (Test-Path -LiteralPath $Path) {
        $ZipPath = $Path
        break
    }
}
if (-not $ZipPath) {
    throw "Windows package not found: $($Candidates -join ', ')"
}

$TempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("hotspot-rc131-customer-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $TempRoot | Out-Null
try {
    Expand-Archive -LiteralPath $ZipPath -DestinationPath $TempRoot -Force
    $forbidden = @(
        "tests",
        "pytest.ini",
        "requirements-dev.txt",
        "TECH_AUDIT.md",
        "STATUS.md",
        "install.bat",
        ".gitignore",
        "config\settings.json",
        "config\credentials.dat",
        "runtime\Lib\idlelib",
        "runtime\Lib\lib2to3",
        "runtime\Lib\turtledemo",
        "runtime\Lib\venv",
        "scripts\package_rc1.py",
        "scripts\phase1_smoke_test.py"
    )
    foreach ($relative in $forbidden) {
        if (Test-Path -LiteralPath (Join-Path $TempRoot $relative)) {
            throw "Forbidden customer package entry: $relative"
        }
    }
    $required = @(
        "runtime\python.exe",
        "api.py",
        "app.py",
        "launcher.ps1",
        "start.bat",
        "RC1_WINDOWS_README.md",
        "requirements-runtime.txt",
        "modules\credential_store.py",
        "ui\rc1_app.py"
    )
    foreach ($relative in $required) {
        if (-not (Test-Path -LiteralPath (Join-Path $TempRoot $relative))) {
            throw "Missing customer package entry: $relative"
        }
    }
    $pyc = Get-ChildItem -LiteralPath $TempRoot -Recurse -File -Filter *.pyc -ErrorAction SilentlyContinue
    if ($pyc.Count -gt 0) {
        throw "Customer package contains .pyc files"
    }
    $settings = Get-ChildItem -LiteralPath $TempRoot -Recurse -File -Filter settings.json -ErrorAction SilentlyContinue
    if ($settings.Count -gt 0) {
        throw "Customer package contains settings.json"
    }
    $bypassHits = Get-ChildItem -LiteralPath $TempRoot -Recurse -File -Include *.py,*.ps1,*.bat,*.md -ErrorAction SilentlyContinue | Select-String -Pattern "HOTSPOT_ALLOW_UNAUTHENTICATED_TEST_API" -SimpleMatch
    if ($bypassHits) {
        throw "Customer package contains unauthenticated test API bypass switch"
    }
    & (Join-Path $TempRoot "runtime\python.exe") -c "import sys; import streamlit, fastapi, httpx, PIL, docx, socksio; print(sys.version)"
    if ($LASTEXITCODE -ne 0) {
        throw "Bundled runtime import check failed"
    }
    Write-Host "CUSTOMER_PACKAGE_SMOKE_PASS"
} finally {
    Remove-Item -LiteralPath $TempRoot -Recurse -Force -ErrorAction SilentlyContinue
}
