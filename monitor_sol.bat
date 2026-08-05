@echo off
chcp 65001 >nul
cd /d "%~dp0"
python fast_monitor.py --coin sol --auto-target
pause
