@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Material library UI: http://127.0.0.1:8766
echo Press Ctrl+C to stop.
python pipeline.py ui
pause
