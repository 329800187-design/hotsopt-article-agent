function Find-CompatiblePython([string]$LauncherPath = "") {
    $launcher = if ($LauncherPath) { Get-Command $LauncherPath -ErrorAction SilentlyContinue } else { Get-Command py.exe -ErrorAction SilentlyContinue }
    $attempts = @()
    if ($launcher) {
        foreach ($minor in 13, 12, 11) {
            $selector = "-3.$minor"
            $previousErrorAction = $ErrorActionPreference
            $ErrorActionPreference = "Continue"
            & $launcher.Source $selector -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" 2>$null
            $candidateExitCode = $LASTEXITCODE
            $ErrorActionPreference = $previousErrorAction
            if ($candidateExitCode -eq 0) {
                $executable = (& $launcher.Source $selector -c "import sys; print(sys.executable)" 2>$null | Select-Object -Last 1).Trim()
                if ($executable) { return [PSCustomObject]@{ Path = $launcher.Source; Args = @($selector); Executable = $executable; Version = (& $launcher.Source $selector -c "import platform; print(platform.python_version())") } }
            }
            $attempts += "py.exe $selector"
        }
    }
    $python = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($python) {
        & $python.Source -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) {
            return [PSCustomObject]@{ Path = $python.Source; Args = @(); Executable = (& $python.Source -c "import sys; print(sys.executable)"); Version = (& $python.Source -c "import platform; print(platform.python_version())") }
        }
        $attempts += "python.exe"
    }
    throw ("No compatible Python 3.11+ interpreter found. Checked: " + ($attempts -join ", "))
}
