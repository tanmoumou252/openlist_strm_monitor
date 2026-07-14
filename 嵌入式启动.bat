@echo off
chcp 65001 >nul
setlocal

cd /d "%~dp0"

set "PYTHON=%~dp0src\python_embed\python.exe"
set "APP=%~dp0src\webui\server.py"

if not exist "%PYTHON%" (
    echo [ERROR] 未找到嵌入式 Python: %PYTHON%
    pause
    exit /b 1
)

if not exist "%APP%" (
    echo [ERROR] 未找到 WebUI 服务器: %APP%
    pause
    exit /b 1
)

:: 3. 检查 pip 是否可用
"%PYTHON%" -m pip --version >nul 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] pip 未安装或不可用。
    echo.
    echo   建议执行以下命令安装 pip:
    echo     "%PYTHON%" -m ensurepip --upgrade
    echo   或参考 https://pip.pypa.io/en/stable/installation/
    echo.
    pause
    exit /b 1
)

:: 4. 检查依赖库是否完整
"%PYTHON%" -c "import watchdog; import requests; import lxml; import pyotp" >nul 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] 检测到缺失的依赖库。
    echo.
    echo   请先安装依赖:
    echo     "%PYTHON%" -m pip install -r requirements.txt
    echo.
    echo   如果网络不通，可先设置代理再安装:
    echo     set HTTPS_PROXY=http://127.0.0.1:7890
    echo     set HTTP_PROXY=http://127.0.0.1:7890
    echo     "%PYTHON%" -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

echo =======================================================
echo   OpenList STRM Bridge
echo =======================================================
echo.

"%PYTHON%" "%APP%"

echo.
echo [INFO] 程序已退出
pause
