@echo off
rem Starts the guardrail test chatbot on Windows: double-click this file, or type start.bat in
rem Command Prompt in this folder. It runs start.ps1, which says what it does, with PowerShell,
rem allowing that one script to run whatever this computer's policy for scripts is.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
rem A window opened by a double-click closes when the script ends: keep it open when it ends with
rem a message to read.
if errorlevel 1 pause
