@echo off
rem 运行全部 pytest 测试套件（src/tests/）
rem 用法：run_tests.bat            （默认详细模式）
rem       run_tests.bat --cov      （带覆盖率报告，需 pip install pytest-cov）
rem 从项目根目录运行 pytest src/tests/（脚本位于 src/tests/，需回退两级）

setlocal
cd /d "%~dp0\..\.."

:: 1. 优先使用 PYTHON_EXE 环境变量，未配置时回退到 python
if defined PYTHON_EXE (
    set "PYTHON=%PYTHON_EXE:"=%"
) else (
    set "PYTHON=python"
)

:: 2. 检查系统变量中是否存在 python
where "%PYTHON%" >nul 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] 未在系统环境中找到 python 命令。
    echo 请确保已安装 Python 并勾选了 "Add Python to PATH"。
    exit /b 1
)

:: 3. 检查 Python 版本 >= 3.11
"%PYTHON%" -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] Python 版本过低，需要 3.11 或更高版本。
    "%PYTHON%" --version
    exit /b 1
)

:: 4. 检查 pytest 是否已安装
"%PYTHON%" -m pytest --version >nul 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] 未检测到 pytest。请先安装测试依赖：
    echo   "%PYTHON%" -m pip install -r src/tests/requirements-dev.txt
    exit /b 1
)

if "%1"=="--cov" (
    "%PYTHON%" -m pytest src/tests/ -v --cov=src --cov-report=html
) else (
    "%PYTHON%" -m pytest src/tests/ -v
)
endlocal
