@echo off
cd /d "%~dp0"
where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw "%~dp0txt_splitter.py"
    exit /b 0
)
where python >nul 2>nul
if %errorlevel%==0 (
    python "%~dp0txt_splitter.py"
    exit /b 0
)
echo Python not found. Install Python 3.9+ and check "Add Python to PATH".
pause