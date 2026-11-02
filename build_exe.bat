@echo off
chcp 65001 >nul
echo === Construction de AORUS-Open-CC.exe ===
python -m pip install --upgrade pyinstaller psutil || goto :err
python -m PyInstaller --onefile --noconsole --uac-admin --name AORUS-Open-CC --icon ui\aorus.ico ^
  --add-data "ui;ui" --add-data "gbwmi.ps1;." app.py || goto :err
echo.
echo OK : dist\AORUS-Open-CC.exe
pause
exit /b 0
:err
echo ECHEC de la construction.
pause
exit /b 1
