@echo off
cd /d "%~dp0"
py -3 -m pip install --upgrade pyinstaller wxPython
if errorlevel 1 goto failed
py -3 -m PyInstaller --clean --noconfirm --onefile --windowed --name ClaudeHistoryTransfer claude_history_windows.py
if errorlevel 1 goto failed
echo EXE has been created at dist\ClaudeHistoryTransfer.exe
pause
exit /b 0
:failed
echo Build failed. Check Python and pip.
pause
exit /b 1
