@echo off
setlocal
cd /d %~dp0
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\stop_project.ps1"
echo Hotspot Article Agent stop check complete.
