@echo off
chcp 65001 >nul
setlocal

cd /d "%~dp0"

set "PYTHON=%~dp0src\python_embed\python.exe"
set "APP=%~dp0src\main.py"

if not exist "%PYTHON%" (
    echo [ERROR] 未找到嵌入式 Python: %PYTHON%
    pause
    exit /b 1
)

if not exist "%APP%" (
    echo [ERROR] 未找到主程序: %APP%
    pause
    exit /b 1
)

echo =======================================================
echo   OpenList STRM Bridge - Console Run
echo =======================================================
echo.

"%PYTHON%" "%APP%"

echo.
echo [INFO] 程序已退出
pause
