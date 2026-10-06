@echo off
setlocal
cd /d "%~dp0"

echo Installing dependencies...
python -m pip install -r requirements.txt pyinstaller || goto :error

echo Building Smart Shutdown Pro...
pyinstaller --noconfirm --onefile --windowed ^
  --icon="assets/app_icon.ico" ^
  --add-data "assets;assets" ^
  --collect-all customtkinter ^
  --hidden-import pystray._win32 ^
  --hidden-import plyer.platforms.win.notification ^
  --name "SmartShutdownPro" ^
  src/main.py || goto :error

echo.
echo Done. Your app: dist\SmartShutdownPro.exe
goto :eof

:error
echo.
echo Build failed. Check the messages above.
exit /b 1
