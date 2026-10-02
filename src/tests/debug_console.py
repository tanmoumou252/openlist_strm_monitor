from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

if os.name == "nt":
    import msvcrt


# 文件位于 src/scripts/，需要 parent.parent.parent 才能到项目根目录
ROOT_DIR = Path(__file__).resolve().parent.parent.parent
MAIN_PY = ROOT_DIR / "src" / "main.py"
DB_PATH = ROOT_DIR / "bridge.db"
WEBUI_URL = "http://127.0.0.1:8579"


def set_color(code: str) -> None:
    if os.name == "nt":
        os.system(f"color {code}")


def clear_screen() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def pause(message: str = "按回车键返回主菜单...") -> None:
    input(f"\n{message}")


def read_menu_choice() -> str:
    if os.name == "nt":
        while True:
            ch = msvcrt.getwch()
            if ch in "12345678":
                print(ch)
                return ch
    return input().strip()


def print_header(title: str = "OpenList STRM Bridge", subtitle: str = "调试控制台 / 实时监控") -> None:
    print()
    print(" " + "=" * 97)
    print(f"   {title}")
    print(f"   {subtitle}")
    print(" " + "=" * 97)
    print()


def show_menu(python_exe: str) -> None:
    clear_screen()
    # 颜色只应用一次：清屏后立即重置为默认白底黑字，标题和菜单通过 ANSI 着色。
    set_color("07")
    print_header()
    print("   Python 环境:")
    print(f"   {python_exe}")
    print()
    print(" " + "-" * 97)
    print("   操 作 菜 单")
    print(" " + "-" * 97)
    print()
    print("   [1] 在控制台运行              前台实时日志，按 q 或 Ctrl+C 退出")
    print("   [2] 后台静默运行              启动并添加开机自启")
    print("   [3] 停止后台进程              安全结束监控进程")
    print("   [4] 打开自启文件夹            管理 strm_bridge.vbs")
    print("   [5] 清除本地数据库            重置 bridge.db / wal / shm")
    print(f"   [6] 打开 WebUI 管理面板       {WEBUI_URL}")
    print("   [7] 查看数据库统计            各表记录数和 B 区状态分布")
    print("   [8] 退出")
    print()
    print(" " + "-" * 97)
    print()


def run_console(python_exe: str) -> None:
    # 先切回默认白色，再输出本页标题和后续程序日志，避免残留主菜单水蓝色。
    set_color("07")
    print()
    print(" " + "=" * 97)
    print("   [1] 在控制台运行")
    print(" " + "=" * 97)
    print()
    print("[INFO] 正在启动监控程序...\n")

    result = subprocess.run([python_exe, str(MAIN_PY)], cwd=str(ROOT_DIR))
    exit_code = result.returncode

    print()
    if exit_code == 0:
        print("[OK] 程序已正常退出。")
    else:
        set_color("0C")
        print(f"[ERROR] 程序异常退出，退出码: {exit_code}")

    print("\n3 秒后返回主菜单...")
    time.sleep(3)


def run_background(python_exe: str) -> None:
    clear_screen()
    set_color("09")
    print_header(subtitle="[2] 后台静默运行 + 开机自启")

    startup_dir = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    startup_dir.mkdir(parents=True, exist_ok=True)
    startup_vbs = startup_dir / "strm_bridge.vbs"

    with startup_vbs.open("w", encoding="utf-8") as f:
        f.write('Set WshShell = CreateObject("WScript.Shell")\n')
        f.write(f'WshShell.CurrentDirectory = "{ROOT_DIR}"\n')
        f.write(f'WshShell.Run """{python_exe}"" ""{MAIN_PY}""", 0, False\n')

    print("[OK] 自启脚本写入:")
    print(f"     {startup_vbs}")
    print()
    print("[INFO] 正在启动后台监控...")
    subprocess.run(["cscript.exe", "//nologo", str(startup_vbs)], cwd=str(ROOT_DIR), shell=False)
    print()
    print("[OK] 程序已在后台运行！关闭此窗口不影响监控。")
    print("提示: 使用菜单 [3] 可停止它。")
    pause()


def stop_background() -> None:
    clear_screen()
    set_color("0C")
    print_header(subtitle="[3] 停止后台进程")
    print("[INFO] 正在扫描并结束 python main.py 进程...")

    commands = [
        'wmic process where "name=\'python.exe\' and commandline like \'%main.py%\'" call terminate',
        'wmic process where "name=\'pythonw.exe\' and commandline like \'%main.py%\'" call terminate',
    ]
    for cmd in commands:
        subprocess.run(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    print("[OK] 已发送停止指令。")
    print("提示: 如果进程仍在运行，请在任务管理器中手动结束 python.exe。")
    pause()


def open_startup_folder() -> None:
    clear_screen()
    set_color("0D")
    print_header(subtitle="[4] 打开开机自启文件夹")
    print("[INFO] 正在打开 Windows 启动文件夹...")
    print("提示: 删除 strm_bridge.vbs 即可取消开机自启。")
    subprocess.Popen(["explorer", "shell:startup"])
    pause()


def clear_database() -> None:
    clear_screen()
    set_color("0C")
    print_header(subtitle="[5] 清除本地数据库")
    print("警告: 此操作将删除数据库，所有本地记录将丢失！")
    confirm = input("\n请输入 YES 确认: ").strip()
    if confirm.upper() != "YES":
        print("\n[INFO] 已取消操作。")
        pause()
        return

    print("\n[INFO] 正在删除数据库:")
    print(f"       {DB_PATH}")

    files = [DB_PATH, Path(str(DB_PATH) + "-wal"), Path(str(DB_PATH) + "-shm")]
    deleted_any = False
    failed = []
    for file in files:
        if file.exists():
            try:
                file.unlink()
                deleted_any = True
            except OSError:
                failed.append(file)

    if failed:
        print("\n[ERROR] 删除失败！数据库可能被占用。")
        for file in failed:
            print(f"       {file}")
        print("提示: 请先停止监控程序，再清除数据库。")
    elif deleted_any:
        print("\n[OK] 数据库已成功清除。")
    else:
        print("\n[INFO] 数据库文件不存在，无需清除。")

    pause()


def open_webui() -> None:
    clear_screen()
    set_color("0B")
    print_header(subtitle="[6] 打开 WebUI 管理面板")
    print("[INFO] 正在打开浏览器...")
    webbrowser.open(WEBUI_URL)
    print(f"[OK] 已打开: {WEBUI_URL}")
    print("提示: 如果 WebUI 未启用，请在 config.toml 的 [webui] 中设置 enabled = true。")
    pause()


def show_db_stats() -> None:
    clear_screen()
    set_color("0E")
    print_header(subtitle="[7] 数据库统计信息")
    print("[INFO] 正在查询数据库统计...\n")

    if not DB_PATH.exists():
        print("[ERROR] 数据库文件不存在。")
        pause()
        return

    tables = [
        "a_strm_files",
        "b_strm_files",
        "c_ghost_files",
        "strm_identity",
        "ghost_protection",
        "known_folders",
        "subtitles",
        "strm_media_boundary",
    ]

    try:
        size_mb = DB_PATH.stat().st_size / 1024 / 1024
        print(f"数据库文件: {DB_PATH}")
        print(f"文件大小: {size_mb:.2f} MB\n")

        conn = sqlite3.connect(str(DB_PATH))
        try:
            for table in tables:
                try:
                    count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    print(f"{table:<25} {count:>8} 条")
                except sqlite3.Error:
                    print(f"{table:<25} {'-':>8}")

            print()
            try:
                rows = conn.execute("SELECT status, COUNT(*) FROM b_strm_files GROUP BY status").fetchall()
                for status, count in rows:
                    print(f"B区 {status or 'unknown':<15} {count:>8} 条")
            except sqlite3.Error:
                pass
        finally:
            conn.close()
    except Exception as exc:
        set_color("0C")
        print(f"[ERROR] 查询失败: {exc}")

    pause()


def main() -> int:
    python_exe = sys.executable

    while True:
        show_menu(python_exe)
        print("请选择 1-8: ", end="", flush=True)
        choice = read_menu_choice()

        if choice == "1":
            run_console(python_exe)
        elif choice == "2":
            run_background(python_exe)
        elif choice == "3":
            stop_background()
        elif choice == "4":
            open_startup_folder()
        elif choice == "5":
            clear_database()
        elif choice == "6":
            open_webui()
        elif choice == "7":
            show_db_stats()
        elif choice == "8":
            print("\n[OK] 再见！")
            return 0
        else:
            print("\n请输入 1-8 之间的数字。")
            time.sleep(1)


if __name__ == "__main__":
    raise SystemExit(main())