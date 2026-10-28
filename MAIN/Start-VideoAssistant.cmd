@echo off
powershell.exe -NoProfile -File "%~dp0scripts\open-video-assistant.ps1" -Open
if errorlevel 1 (
  echo Video Assistant could not start. The error above was preserved.
  pause
  exit /b 1
)
