@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

:: 1. 将 Python 命令指向系统变量中的全局 python
set "PYTHON=python"
set "APP=%~dp0main.py"

:: 2. 检查系统变量中是否存在 python 
where %PYTHON% >nul 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] 未在系统环境中找到 python 命令。
    echo 请确保已安装 Python 并勾选了 "Add Python to PATH"。
    pause
    exit /b 1
)

if not exist "%APP%" (
    echo [ERROR] 未找到主程序: %APP%
    pause
    exit /b 1
)

echo =======================================================
echo   OpenList STRM Bridge - Console Run (System Python)
echo =======================================================
echo.

:: 3. 运行程序
"%PYTHON%" "%APP%"

echo.
echo [INFO] 程序已退出
pause
