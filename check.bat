@echo off
REM Keep this file ASCII-only: cmd can mis-parse UTF-8 lines and run fragments.
REM Environment self-check: run before starting the app on a new PC / on site.
REM Checks packages, ffmpeg/ffprobe/go2rtc, config.ini, 8 PTZ cameras, go2rtc restream.
cd /d "%~dp0"
if exist .venv\Scripts\python.exe (
    set "PY=.venv\Scripts\python.exe"
) else if defined CONDA_PREFIX (
    set "PY=python"
) else (
    set "PY=py -3.14"
)
%PY% check_env.py
pause
