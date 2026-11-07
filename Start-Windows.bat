@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 -c "import wx" 2>nul || py -3 -m pip install wxPython
  py -3 "%~dp0claude_history_windows.py"
  goto done
)
where python >nul 2>nul
if %errorlevel%==0 (
  python -c "import wx" 2>nul || python -m pip install wxPython
  python "%~dp0claude_history_windows.py"
  goto done
)
echo Python 3.10+ was not found.
echo Install Python from https://www.python.org/downloads/windows/
echo Enable "Add python.exe to PATH" during installation.
pause
:done
if errorlevel 1 pause
