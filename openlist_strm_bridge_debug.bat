@echo OFF
chcp 65001 >nul 2>nul
setlocal EnableDelayedExpansion

REM ============================================================
REM  STRM Bridge 调试控制台 (彩色版)
REM  不同操作使用不同的背景/文本颜色
REM ============================================================

REM 颜色定义 (ANSI Escape Codes)
REM 通过 prompt $E 获取真实 ESC 字符，避免 set "ESC=" 导致颜色不生效
for /F "delims=" %%A in ('echo prompt $E^| cmd') do set "ESC=%%A"
set "RESET=%ESC%[0m"
set "RED=%ESC%[91m"
set "GREEN=%ESC%[92m"
set "YELLOW=%ESC%[93m"
set "BLUE=%ESC%[94m"
set "MAGENTA=%ESC%[95m"
set "CYAN=%ESC%[96m"
set "WHITE=%ESC%[97m"
set "BG_RED=%ESC%[41m"
set "BG_GREEN=%ESC%[42m"
set "BG_YELLOW=%ESC%[43m"
set "BG_BLUE=%ESC%[44m"
set "BG_MAGENTA=%ESC%[45m"
set "BG_CYAN=%ESC%[46m"
set "BG_DARK=%ESC%[40m"
set "BOLD=%ESC%[1m"

REM 强制指定使用当前目录下的 python_embed
set pythonPath="%~dp0python_embed\python.exe"

REM 如果没有嵌入式 Python，尝试使用系统 python
if not exist %pythonPath% (
    set pythonPath=python
    where python >nul 2>nul
    if errorlevel 1 (
        echo %RED%%BOLD%[ERROR]%RESET% %RED%未找到 Python 环境！%RESET%
        echo %YELLOW%请确保 python_embed 文件夹存在，或将 python 添加到 PATH。%RESET%
        pause
        GOTO END
    )
)

:BEGIN
cls
echo.
echo %BG_CYAN%%WHITE%%BOLD%                                                            %RESET%
echo %BG_CYAN%%WHITE%%BOLD%     STRM Bridge 调试控制台                               %RESET%
echo %BG_CYAN%%WHITE%%BOLD%     OpenList STRM Monitor & Manager                      %RESET%
echo %BG_CYAN%%WHITE%%BOLD%                                                            %RESET%
echo.
echo %GREEN%[INFO]%RESET% 已加载 Python 环境: %CYAN%%pythonPath%%RESET%
echo.
echo %BG_BLUE%%WHITE%%BOLD% ==================== 操 作 菜 单 ==================== %RESET%
echo.
echo   %BOLD%%CYAN%[1]%RESET% %GREEN%在控制台运行%RESET% %YELLOW%(前台实时日志, 按 q 或 Ctrl+C 退出)%RESET%
echo   %BOLD%%CYAN%[2]%RESET% %BLUE%后台静默运行%RESET% %YELLOW%+ 添加开机自启%RESET%
echo   %BOLD%%CYAN%[3]%RESET% %RED%停止后台进程%RESET% %YELLOW%(安全模式)%RESET%
echo   %BOLD%%CYAN%[4]%RESET% %MAGENTA%打开开机自启文件夹%RESET%
echo   %BOLD%%CYAN%[5]%RESET% %BG_RED%%WHITE%清除本地数据库%RESET% %YELLOW%(重置环境)%RESET%
echo   %BOLD%%CYAN%[6]%RESET% %CYAN%打开 WebUI 管理面板%RESET% %YELLOW%(浏览器)%RESET%
echo   %BOLD%%CYAN%[7]%RESET% %WHITE%查看数据库统计%RESET%
echo   %BOLD%%CYAN%[8]%RESET% %BG_DARK%%WHITE%退出控制台%RESET%
echo.
echo %BG_BLUE%%WHITE%%BOLD% ======================================================= %RESET%
echo.

choice /N /C 12345678 /M "%CYAN%请按键盘数字键进行选择:%RESET% "
IF ERRORLEVEL 8 GOTO EXIT
IF ERRORLEVEL 7 GOTO SEVEN
IF ERRORLEVEL 6 GOTO SIX
IF ERRORLEVEL 5 GOTO FIVE
IF ERRORLEVEL 4 GOTO FOUR
IF ERRORLEVEL 3 GOTO THREE
IF ERRORLEVEL 2 GOTO TWO
IF ERRORLEVEL 1 GOTO ONE
GOTO END


:ONE
echo.
echo %BG_GREEN%%BLACK%%BOLD% [1] 在控制台运行 %RESET%
echo.
echo %GREEN%[INFO]%RESET% 正在启动监控程序...
echo %BG_DARK%%WHITE% ======================================================= %RESET%
echo.
%pythonPath% "%~dp0src\main.py"
echo.
set EXIT_CODE=%ERRORLEVEL%
if %EXIT_CODE% EQU 0 (
    echo %GREEN%[OK]%RESET% 程序已正常退出。
) else (
    echo %RED%[ERROR]%RESET% 程序异常退出，退出码: %RED%%EXIT_CODE%%RESET%
)
echo %YELLOW%即将返回主菜单...%RESET%
timeout /t 3 >nul
GOTO BEGIN


:TWO
echo.
echo %BG_BLUE%%WHITE%%BOLD% [2] 后台静默运行 + 开机自启 %RESET%
echo.
set startupVbs="%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\strm_bridge.vbs"

echo Set WshShell = CreateObject("WScript.Shell") > %startupVbs%
echo WshShell.CurrentDirectory = "%~dp0" >> %startupVbs%
if exist "%~dp0python_embed\python.exe" (
    echo WshShell.Run """%~dp0python_embed\python.exe"" ""%~dp0src\main.py""", 0, False >> %startupVbs%
) else (
    echo WshShell.Run "python ""%~dp0src\main.py""", 0, False >> %startupVbs%
)

echo %GREEN%[OK]%RESET% 自启脚本写入: %CYAN%%startupVbs%%RESET%
echo %GREEN%[OK]%RESET% 正在启动后台监控...
cscript.exe //nologo %startupVbs%
echo.
echo %GREEN%%BOLD%[OK] 程序已在后台运行！关闭此窗口不影响监控。%RESET%
echo %YELLOW%提示: 停止程序请使用菜单 [3]%RESET%
echo.
echo 按任意键返回主菜单...
pause >nul
GOTO BEGIN


:THREE
echo.
echo %BG_RED%%WHITE%%BOLD% [3] 停止后台进程 %RESET%
echo.
echo %YELLOW%[INFO]%RESET% 正在扫描并结束 strm_bridge 后台进程...
wmic process where "name='python.exe' and commandline like '%%main.py%%'" call terminate >nul 2>nul
wmic process where "name='pythonw.exe' and commandline like '%%main.py%%'" call terminate >nul 2>nul
echo %GREEN%[OK]%RESET% 已发送停止指令！
echo %YELLOW%提示: 如果进程仍在运行，请在任务管理器中手动结束 python.exe%RESET%
echo.
echo 按任意键返回主菜单...
pause >nul
GOTO BEGIN


:FOUR
echo.
echo %BG_MAGENTA%%WHITE%%BOLD% [4] 打开开机自启文件夹 %RESET%
echo.
echo %YELLOW%[INFO]%RESET% 正在打开启动文件夹...
echo %YELLOW%提示: 删除 strm_bridge.vbs 即可取消开机自启%RESET%
explorer shell:startup
echo.
echo 按任意键返回主菜单...
pause >nul
GOTO BEGIN


:FIVE
echo.
echo %BG_RED%%WHITE%%BOLD% [5] 清除本地数据库 %RESET%
echo.
echo %RED%%BOLD%⚠ 警告: 此操作将删除数据库，所有记录将丢失！%RESET%
echo.
set /p CONFIRM="%YELLOW%确定要继续吗? (输入 YES 确认): %RESET%"
if /I not "!CONFIRM!"=="YES" (
    echo %GREEN%[INFO]%RESET% 已取消操作。
    echo 按任意键返回主菜单...
    pause >nul
    GOTO BEGIN
)

REM 尝试从 config.toml 读取 db_file 路径（默认 bridge.db）
set dbPath="%~dp0bridge.db"

echo %YELLOW%[INFO]%RESET% 正在删除数据库: %dbPath%
if exist %dbPath% (
    del /F %dbPath% 2>nul
    if exist %dbPath% (
        echo %RED%[ERROR]%RESET% 删除失败！数据库可能被占用。
        echo %YELLOW%提示: 请先停止监控程序，再清除数据库。%RESET%
    ) else (
        echo %GREEN%[OK]%RESET% 数据库已成功清除！
        REM 同时删除 WAL 和 SHM 文件
        if exist "%~dp0bridge.db-wal" del /F "%~dp0bridge.db-wal" 2>nul
        if exist "%~dp0bridge.db-shm" del /F "%~dp0bridge.db-shm" 2>nul
    )
) else (
    echo %CYAN%[INFO]%RESET% 数据库文件不存在，无需清除。
)
echo.
echo 按任意键返回主菜单...
pause >nul
GOTO BEGIN


:SIX
echo.
echo %BG_CYAN%%BLACK%%BOLD% [6] 打开 WebUI 管理面板 %RESET%
echo.
echo %CYAN%[INFO]%RESET% 正在打开浏览器...
REM 默认端口 8579，可修改
start "" "http://127.0.0.1:8579"
echo %GREEN%[OK]%RESET% 已打开: %CYAN%http://127.0.0.1:8579%RESET%
echo %YELLOW%提示: 如果 WebUI 未启用，请在 config.toml 的 [webui] 中设置 enabled = true%RESET%
echo.
echo 按任意键返回主菜单...
pause >nul
GOTO BEGIN


:SEVEN
echo.
echo %BG_YELLOW%%BLACK%%BOLD% [7] 数据库统计信息 %RESET%
echo.
echo %CYAN%[INFO]%RESET% 正在查询数据库统计...
echo.
%pythonPath% -c "import sqlite3,os; db=r'%~dp0bridge.db'; print('\033[93m数据库文件:\033[0m', db); print('\033[93m文件大小:\033[0m', round(os.path.getsize(db)/1024/1024,2) if os.path.exists(db) else '不存在', 'MB'); conn=sqlite3.connect(db); tables=['a_strm_files','b_strm_files','c_ghost_files','strm_identity','ghost_protection','known_folders','subtitles','strm_media_boundary']; print(); [print(f'\033[96m{t:<25}\033[0m \033[92m{conn.execute(f\"SELECT COUNT(*) FROM {t}\").fetchone()[0]:>8}\033[0m 条') for t in tables]; print(); bs=conn.execute('SELECT status,COUNT(*) FROM b_strm_files GROUP BY status').fetchall(); [print(f'\033[93m  B区 {r[0] or \"unknown\":<15}\033[0m \033[92m{r[1]:>8}\033[0m 条') for r in bs]; conn.close()" 2>nul
if errorlevel 1 (
    echo %RED%[ERROR]%RESET% 查询失败！请确保数据库文件存在。
)
echo.
echo 按任意键返回主菜单...
pause >nul
GOTO BEGIN


:EXIT
echo.
echo %GREEN%[OK]%RESET% 再见！
exit /b 0


:END
exit /b 1