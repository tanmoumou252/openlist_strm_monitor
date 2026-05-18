@echo OFF
chcp 65001 >nul
:BEGIN
cls
echo =======================================================
echo          STRM 监控清理工具 - 管理控制台 
echo =======================================================
echo.

REM 强制指定使用当前目录下的 python_embed
set pythonPath="%~dp0python_embed\python.exe"

REM 检查 embed 解释器是否存在
if not exist %pythonPath% (
    echo [ERROR] 未找到内嵌 Python 环境！ 
    echo 请确保 [python_embed] 文件夹和里面的 [python.exe] 存在于当前目录下。 
    pause
    GOTO END
)

echo [INFO] 已加载本地绿色版 Python 环境。 
echo.
echo ==================== 操 作 菜 单 ====================
echo [1] 在控制台运行 (前台显示实时日志, 关闭窗口即停止) 
echo [2] 在后台静默运行并添加开机自启 
echo [3] 停止正在后台运行的监控进程 (安全模式) 
echo [4] 打开开机自启文件夹 (用于手动取消自启) 
echo [5] 清除本地数据库 (用于环境重置/重新扫描) 
echo [6] 退出控制台 
echo =======================================================
echo.

choice /N /C 123456 /M "请按键盘数字键进行选择: "
IF ERRORLEVEL 6 GOTO EXIT
IF ERRORLEVEL 5 GOTO FIVE
IF ERRORLEVEL 4 GOTO FOUR
IF ERRORLEVEL 3 GOTO THREE
IF ERRORLEVEL 2 GOTO TWO
IF ERRORLEVEL 1 GOTO ONE
GOTO END


:ONE
echo.
echo 您选择了 [1] 在控制台运行 
echo 正在启动监控程序... 
echo =======================================================
%pythonPath% "%~dp0strm_monitor.py"
echo.
echo [INFO] 监控已停止，即将返回主菜单... 
timeout /t 2 >nul
GOTO BEGIN


:TWO
echo.
echo 您选择了 [2] 后台静默运行并添加开机自启 
set startupVbs="%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\strm_monitor.vbs"

echo Set WshShell = CreateObject("WScript.Shell") > %startupVbs%
echo WshShell.CurrentDirectory = "%~dp0" >> %startupVbs%
echo WshShell.Run """%~dp0python_embed\python.exe"" ""%~dp0strm_monitor.py""", 0, False >> %startupVbs%

echo [OK] 已将自启脚本写入: %startupVbs% 
echo [OK] 正在启动后台监控... 
cscript.exe //nologo %startupVbs%
echo [OK] 程序已在后台成功运行！关闭此窗口不影响监控。 
echo.
echo 按任意键返回主菜单...
pause >nul
GOTO BEGIN


:THREE
echo.
echo 您选择了 [3] 停止后台运行的进程 
echo 正在扫描并结束 strm_monitor 的后台进程... 
wmic process where "name='python.exe' and commandline like '%%strm_monitor.py%%'" call terminate >nul 2>nul
echo [OK] 监控程序已发送停止指令！ 
echo.
echo 按任意键返回主菜单...
pause >nul
GOTO BEGIN


:FOUR
echo.
echo 您选择了 [4] 打开开机自启文件夹 
echo 正在打开... (若要取消开机自启，请删除该文件夹内的 strm_monitor.vbs) 
explorer shell:startup
echo.
echo 按任意键返回主菜单...
pause >nul
GOTO BEGIN


:FIVE
echo.
echo 您选择了 [5] 清除本地数据库 
REM 这里的路径应手动保持与 config.ini 里的 db_file 一致
set dbPath="%~dp0python_embed\strm_mapping.db"

echo 正在尝试删除旧数据库: %dbPath%
if exist %dbPath% (
    del %dbPath%
    echo [OK] 数据库已成功清除！ 
) else (
    echo [INFO] 数据库文件不存在，无需清除。 
)
echo.
echo 按任意键返回主菜单...
pause >nul
GOTO BEGIN


:EXIT
exit /b


:END
exit /b