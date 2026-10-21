@echo off
rem Double-click: turns on the Windows ssh-agent service (asks for administrator approval).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0enable-ssh-agent.ps1"
