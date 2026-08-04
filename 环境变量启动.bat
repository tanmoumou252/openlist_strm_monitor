@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

:: 1. 将 Python 命令指向系统变量中的全局 python
set "PYTHON=python"
set "APP=%~dp0src\webui\server.py"

:: 2. 检查系统变量中是否存在 python 
where %PYTHON% >nul 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] 未在系统环境中找到 python 命令。
    echo 请确保已安装 Python 并勾选了 "Add Python to PATH"。
    pause
    exit /b 1
)

:: 3. 检查 Python 版本 >= 3.11
"%PYTHON%" -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] Python 版本过低，需要 3.11 或更高版本。
    "%PYTHON%" --version
    echo.
    echo 请升级 Python 到 3.11+ 并确保在 PATH 中。
    pause
    exit /b 1
)

if not exist "%APP%" (
    echo [ERROR] 未找到 WebUI 服务器: %APP%
    pause
    exit /b 1
)

:: 4. 检查 pip 是否可用
"%PYTHON%" -m pip --version >nul 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] pip 未安装或不可用。
    echo.
    echo   建议执行以下命令安装 pip:
    echo     python -m ensurepip --upgrade
    echo   或参考 https://pip.pypa.io/en/stable/installation/
    echo.
    pause
    exit /b 1
)

:: 5. 检查依赖库是否完整
"%PYTHON%" -c "import watchdog; import requests; import lxml" >nul 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] 检测到缺失的依赖库。
    echo.
    echo   请先安装依赖:
    echo     python -m pip install -r requirements.txt
    echo.
    echo   如果网络不通，可先设置代理再安装:
    echo     set HTTPS_PROXY=http://127.0.0.1:7890
    echo     set HTTP_PROXY=http://127.0.0.1:7890
    echo     python -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

echo =======================================================
echo   OpenList STRM Bridge (System Python)
echo =======================================================
echo.

:: 6. 运行程序
"%PYTHON%" "%APP%"

echo.
echo [INFO] 程序已退出
pause
