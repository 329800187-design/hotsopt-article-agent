$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Launcher = Join-Path $Root "Hotspot Article Agent.exe"
$Shell = New-Object -ComObject WScript.Shell
$Icon = Join-Path $Root "ui\assets\brand.ico"
$ShortcutName = "热点图文工作台.lnk"
$Locations = @(
    (Join-Path ([Environment]::GetFolderPath("Desktop")) $ShortcutName)
)
foreach ($location in $Locations) {
    $shortcut = $Shell.CreateShortcut($location)
    $shortcut.TargetPath = $Launcher
    $shortcut.Arguments = ""
    $shortcut.WorkingDirectory = $Root
    $shortcut.IconLocation = "$Icon,0"
    $shortcut.Description = "打开热点图文工作台"
    $shortcut.Save()
}
Write-Output "shortcuts-created"
