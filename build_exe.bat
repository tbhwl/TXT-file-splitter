@echo off
setlocal
cd /d "%~dp0"

echo ============================================
echo   TXT 分割 / 合并工具 - 打包 exe
echo ============================================
echo.

where python >nul 2>nul
if errorlevel 1 goto nopython

python -m pip show pyinstaller >nul 2>nul
if errorlevel 1 goto installpyi
echo [1/3] PyInstaller 已安装
goto checkdnd

:installpyi
echo [1/3] 未安装 PyInstaller，正在安装...
python -m pip install pyinstaller
if errorlevel 1 goto pyifail

:checkdnd
set DND_FLAGS=
python -c "import tkinterdnd2" >nul 2>nul
if errorlevel 1 goto nodnd
set DND_FLAGS=--hidden-import tkinterdnd2 --collect-data tkinterdnd2
echo [2/3] 检测到 tkinterdnd2，启用拖拽依赖
goto build

:nodnd
echo [2/3] 未安装 tkinterdnd2，生成不含拖拽的版本
echo       如需拖拽，先执行: pip install tkinterdnd2
goto build

:build
echo [3/3] 开始打包...
python -m PyInstaller --noconfirm --clean --onefile --noconsole --name "TXT分割工具" %DND_FLAGS% txt_splitter.py
if errorlevel 1 goto buildfail
echo.
echo 完成: dist\TXT分割工具.exe
pause
exit /b 0

:nopython
echo [错误] 没有找到 Python，请先安装 Python 3.9 或以上版本。
pause
exit /b 1

:pyifail
echo [错误] PyInstaller 安装失败，请检查网络后重试: pip install pyinstaller
pause
exit /b 1

:buildfail
echo [错误] 打包失败。若提示 PyInstaller 不支持当前 Python，请改用 Python 3.11-3.13。
pause
exit /b 1