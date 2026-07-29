@echo off
chcp 65001 >nul
title CB Holder System Flask
set "PYEXE=C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
set "WORKDIR=H:\trybuddy\yonghui\cb_holder_system"
set "PATH=C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts;C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12;C:\Windows\System32;C:\Windows"
set "FLASK_DEBUG=1"
cd /d "%WORKDIR%"
echo Starting Flask (auto-reload mode) ...
echo Open: http://127.0.0.1:5000
echo Close this window to stop.
"%PYEXE%" app.py
pause
