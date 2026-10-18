@echo off
cd /d "%~dp0"
py -3 scripts\asset_server.py start --open
if errorlevel 1 pause
