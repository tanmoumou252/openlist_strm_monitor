import os
import sqlite3
import time
import sys
import requests
import configparser
import logging
import platform
import threading
import ctypes
from urllib.parse import urlparse, unquote, quote
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# 程序版本号
VERSION = "v2026.05.18"

# 全局操作锁：防止并发操作导致 WebDAV 423 错误
dav_lock = threading.Lock()

# ANSI 颜色代码定义
C_GREEN = "\033[92m"
C_YELLOW = "\033[93m"
C_RED = "\033[91m"
C_BLUE = "\033[94m"
C_PURPLE = "\033[95m"
C_CYAN = "\033[96m"
C_BOLD = "\033[1m"
C_END = "\033[0m"

# ================= 启用 Windows ANSI 支持 =================

def enable_windows_ansi():
    """ 强制开启 Windows 控制台对 ANSI 颜色代码的支持 """
    if platform.system().lower() == 'windows':
        os.system('')
        try:
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-11)
            mode = ctypes.c_uint()
            kernel32.GetConsoleMode(handle, ctypes.byref(mode))
            kernel32.SetConsoleMode(handle, mode.value | 7)
        except:
            pass

# ================= 日志系统重构 (分离颜色与文件) =================

class ColorConsoleHandler(logging.StreamHandler):
    """ 自定义处理器：仅在控制台根据关键字输出带颜色的日志 """
    def emit(self, record):
        try:
            msg = self.format(record)
            if "[检测到删除]" in msg:
                print(f"{C_PURPLE}{msg}{C_END}")
            elif "-> 已移入回收站" in msg:
                print(f"{C_GREEN}{msg}{C_END}")
            elif "-> 云盘视频已同步删除" in msg:
                print(f"{C_RED}{msg}{C_END}")
            elif "[索引更新]" in msg:
                print(f"{C_CYAN}{msg}{C_END}")
            elif "重试" in msg or "警告" in msg:
                print(f"{C_YELLOW}{msg}{C_END}")
            elif "[错误]" in msg or "失败" in msg:
                print(f"{C_RED}{C_BOLD}{msg}{C_END}")
            elif "[监控启动]" in msg:
                print(f"{C_BLUE}{msg}{C_END}")
            else:
                print(msg)
        except Exception:
            self.handleError(record)

def init_logging():
    level_str = _cfg.get('Log', 'level', fallback='INFO').upper()
    log_file = _cfg.get('Log', 'file', fallback='activity.log')
    max_mb = _cfg.getint('Log', 'max_size_mb', fallback=2)
    max_bytes = max_mb * 1024 * 1024
    
    log_dir = os.path.dirname(log_file)
    if log_dir and not os.path.exists(log_dir):
        os.makedirs(log_dir)

    if os.path.exists(log_file) and os.path.getsize(log_file) > max_bytes:
        with open(log_file, 'w', encoding='utf-8') as f:
            f.write(f"--- 日志超过 {max_mb}MB，已自动重置 ---\n")

    formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', '%Y-%m-%d %H:%M:%S')

    file_handler = logging.FileHandler(log_file, encoding='utf-8')
    file_handler.setFormatter(formatter)

    console_handler = ColorConsoleHandler()
    console_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(level_str)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)

    file_only_logger = logging.getLogger("FileOnly")
    file_only_logger.setLevel(level_str)
    file_only_logger.addHandler(file_handler)
    file_only_logger.propagate = False

    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)

    return file_only_logger

# ================= 配置加载逻辑 =================

def load_config():
    config = configparser.ConfigParser()
    config_path = os.path.join(os.path.dirname(__file__), 'config.ini')
    if not os.path.exists(config_path):
        print(f"[错误] 找不到配置文件: {config_path}")
        sys.exit(1)
    config.read(config_path, encoding='utf-8')
    return config

_cfg = load_config()
file_logger = init_logging()

MONITOR_FOLDERS = [f.strip() for f in _cfg.get('Local', 'monitor_folders').split(',') if f.strip()]
DB_FILE         = _cfg.get('Local', 'db_file')
WEBDAV_HOST     = _cfg.get('WebDAV', 'host').rstrip('/')
WEBDAV_USER     = _cfg.get('WebDAV', 'user')
WEBDAV_PWD      = _cfg.get('WebDAV', 'password')
WEBDAV_ACTION   = _cfg.get('Setting', 'action').upper()
TRASH_DIR_NAME  = _cfg.get('Setting', 'trash_dir_name')

def log_i(msg): logging.info(msg)
def log_f(msg): file_logger.info(msg)
def log_d(msg): logging.debug(msg)
def log_e(msg, exc=False): logging.error(msg, exc_info=exc)

def check_log_size():
    log_file = _cfg.get('Log', 'file', fallback='activity.log')
    max_mb = _cfg.getint('Log', 'max_size_mb', fallback=2)
    if os.path.exists(log_file) and os.path.getsize(log_file) > max_mb * 1024 * 1024:
        with open(log_file, 'w', encoding='utf-8') as f:
            f.write("--- 运行期间日志已达上限，执行自动重置 ---\n")

# ================= WebDAV 客户端 =================

class OpenlistWebDAV:
    def __init__(self, host, user, pwd):
        self.host = host
        self.auth = (user, pwd)

    def _url(self, path):
        return self.host + quote('/' + path.lstrip('/'), safe='/')

    def check_exists(self, path):
        try:
            res = requests.request("PROPFIND", self._url(path), auth=self.auth, headers={"Depth": "0"}, timeout=10)
            return res.status_code in (200, 207)
        except: return False

    def mkdir(self, path):
        res = requests.request("MKCOL", self._url(path), auth=self.auth)
        return res.status_code in (201, 207, 405)

    def makedirs(self, path):
        parts = [p for p in path.strip('/').split('/') if p]
        current_path = ""
        created = False
        for part in parts:
            current_path += "/" + part
            if not self.check_exists(current_path):
                if self.mkdir(current_path):
                    log_d(f"创建云盘目录: {current_path}")
                    created = True
                    time.sleep(0.3)
        if created: time.sleep(0.7)

    def move(self, src_path, dst_path):
        headers = {"Destination": self._url(dst_path), "Overwrite": "T"}
        for attempt in range(3):
            try:
                res = requests.request("MOVE", self._url(src_path), auth=self.auth, headers=headers, timeout=20)
                if res.status_code in (201, 204, 207): return
                err_msg = f"HTTP {res.status_code}"
                if res.status_code == 423: err_msg = "HTTP 423 (资源锁定)"
                if attempt < 2:
                    log_i(f" -> [重试 {attempt+1}] WebDAV {err_msg}，正在稍后重试...")
                    time.sleep(2)
                else: raise Exception(f"{err_msg}: {res.text}")
            except Exception as e:
                if attempt == 2: raise e
                time.sleep(2)

    def delete(self, path):
        for attempt in range(3):
            try:
                res = requests.request("DELETE", self._url(path), auth=self.auth, timeout=15)
                if res.status_code in (200, 202, 204): return
                err_msg = "HTTP 423 (资源锁定)" if res.status_code == 423 else f"HTTP {res.status_code}"
                if attempt < 2:
                    log_i(f" -> [重试 {attempt+1}] WebDAV {err_msg}，正在稍后重试...")
                    time.sleep(2)
                else: raise Exception(f"{err_msg}: {res.text}")
            except Exception as e:
                if attempt == 2: raise e
                time.sleep(2)

client = OpenlistWebDAV(WEBDAV_HOST, WEBDAV_USER, WEBDAV_PWD)

# ================= 业务逻辑 =================

def init_db():
    db_dir = os.path.dirname(DB_FILE)
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir)
    conn = sqlite3.connect(DB_FILE)
    conn.cursor().execute('CREATE TABLE IF NOT EXISTS strm_files (local_path TEXT PRIMARY KEY, webdav_path TEXT)')
    conn.commit()
    conn.close()

def parse_strm_content(content):
    content = content.strip()
    if not content.startswith('http'): return content
    path = unquote(urlparse(content).path) 
    return path[2:] if path.startswith('/d/') else path

def get_dynamic_trash_path(webdav_path):
    parts = webdav_path.strip('/').split('/')
    if len(parts) >= 2:
        drive_name = parts[0]
        relative_path = "/".join(parts[1:-1])
        filename = parts[-1]
        target_dir = f"/{drive_name}/{TRASH_DIR_NAME}/{relative_path}".rstrip('/')
        return target_dir, f"{target_dir}/{filename}"
    return None, None

class StrmMonitorHandler(FileSystemEventHandler):
    def on_created(self, event):
        if not event.is_directory and event.src_path.endswith('.strm'):
            self.add_to_db(event.src_path)
    def on_modified(self, event):
        if not event.is_directory and event.src_path.endswith('.strm'):
            self.add_to_db(event.src_path)
    def on_deleted(self, event):
        if not event.is_directory and event.src_path.endswith('.strm'):
            threading.Thread(target=self.handle_deletion, args=(event.src_path,)).start()

    def add_to_db(self, local_path, is_watchdog=True):
        with dav_lock:
            try:
                if is_watchdog: time.sleep(0.5) 
                with open(local_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                webdav_path = parse_strm_content(content)
                if webdav_path:
                    conn = sqlite3.connect(DB_FILE)
                    cursor = conn.cursor()
                    cursor.execute('SELECT webdav_path FROM strm_files WHERE local_path = ?', (local_path,))
                    row = cursor.fetchone()
                    if row and row[0] == webdav_path:
                        conn.close()
                        return
                    cursor.execute('INSERT OR REPLACE INTO strm_files VALUES (?, ?)', (local_path, webdav_path))
                    conn.commit()
                    conn.close()
                    log_i(f"[索引更新] {os.path.basename(local_path)} -> {webdav_path}")
            except: pass

    def handle_deletion(self, local_path):
        with dav_lock:
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute('SELECT webdav_path FROM strm_files WHERE local_path = ?', (local_path,))
            result = cursor.fetchone()
            if result:
                webdav_path = result[0]
                log_i(f"[检测到删除] {os.path.basename(local_path)}")
                try:
                    if client.check_exists(webdav_path):
                        if WEBDAV_ACTION == "DELETE":
                            client.delete(webdav_path)
                            log_i(f" -> 云盘视频已同步删除")
                        elif WEBDAV_ACTION == "MOVE":
                            target_dir, target_file = get_dynamic_trash_path(webdav_path)
                            if target_dir:
                                client.makedirs(target_dir)
                                client.move(webdav_path, target_file)
                                log_i(f" -> 已移入回收站: {target_file}")
                    else:
                        log_d(f" -> 云盘文件已不存在")
                    cursor.execute('DELETE FROM strm_files WHERE local_path = ?', (local_path,))
                    conn.commit()
                    check_log_size()
                except Exception as e:
                    log_e(f" -> 联动操作失败: {e}")
            conn.close()

def scan_existing_files():
    log_i("正在快速同步本地 strm 索引...")
    start_t = time.time()
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('SELECT local_path, webdav_path FROM strm_files')
    db_records = dict(cursor.fetchall())
    new_updates = []
    count_skip = 0
    for folder in MONITOR_FOLDERS:
        if not os.path.exists(folder):
            log_e(f"配置路径不存在: {folder}")
            continue
        print(f" -> 正在扫描: {folder}")
        for root, dirs, files in os.walk(folder):
            for file in files:
                if file.endswith('.strm'):
                    local_path = os.path.join(root, file)
                    if local_path in db_records:
                        count_skip += 1
                        continue
                    try:
                        with open(local_path, 'r', encoding='utf-8') as f:
                            content = f.read()
                        wp = parse_strm_content(content)
                        if wp: new_updates.append((local_path, wp))
                    except: continue
    if new_updates:
        cursor.executemany('INSERT OR REPLACE INTO strm_files VALUES (?, ?)', new_updates)
        conn.commit()
    conn.close()
    log_i(f"同步完成！耗时: {time.time()-start_t:.2f}s (跳过: {count_skip}, 新增: {len(new_updates)})")

# ================= 主程序启动 =================

if __name__ == "__main__":
    enable_windows_ansi()
    init_db()
    
    print("\n" + "="*60)
    print(f"{C_BLUE}{C_BOLD}   openlist strm monitor {VERSION}{C_END}")
    print("="*60)
    if WEBDAV_ACTION == "MOVE":
        print(f"当前模式: {C_GREEN}{C_BOLD}[ 安全回收: MOVE ]{C_END}")
        print(f"操作说明: 联动时将视频带层级结构的转移至云盘 {C_YELLOW}{TRASH_DIR_NAME}{C_END} 目录。")
    else:
        print(f"当前模式: {C_RED}{C_BOLD}[ 永久删除: DELETE ]{C_END}")
        print(f"操作说明: {C_RED}警告！{C_END}联动时将直接从云盘永久删除原始视频。")
    print("="*60 + "\n")

    log_f("==================================================")
    log_f(f"程序启动 - Openlist Strm Monitor {VERSION}")
    log_f(f"运行模式: {WEBDAV_ACTION} | 回收站: {TRASH_DIR_NAME}")
    log_f(f"系统环境: {platform.platform()} (Python {sys.version.split()[0]})")
    log_f(f"数据库路径: {os.path.abspath(DB_FILE)}")
    log_f(f"监控目录: {', '.join(MONITOR_FOLDERS)}")
    log_f("==================================================")

    scan_existing_files()
    
    event_handler = StrmMonitorHandler()
    observer = Observer()
    active_count = 0
    for folder in MONITOR_FOLDERS:
        if os.path.exists(folder):
            observer.schedule(event_handler, folder, recursive=True)
            log_i(f"[监控启动] {folder}")
            active_count += 1
            
    if active_count == 0:
        log_e("没有有效的监控路径，程序退出。")
        sys.exit(1)
        
    observer.start()
    print(f"\n[系统] 成功启动 {active_count} 个监控任务。")
    try:
        if sys.stdin and sys.stdin.isatty():
            print(f"\n{C_YELLOW}[提示]{C_END} 输入 'q' 并回车可停止监控并返回菜单") 
            while True:
                if input().strip().lower() == 'q': break
        else:
            while True: time.sleep(1)
    except (KeyboardInterrupt, EOFError): pass

    print("\n[系统] 正在安全停止监控线程，请稍候...")
    observer.stop()
    observer.join()