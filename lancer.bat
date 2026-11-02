@echo off
cd /d "%~dp0"
python -m pip install -q psutil
python app.py
