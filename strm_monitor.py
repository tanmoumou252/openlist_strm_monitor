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
import shutil
import xml.etree.ElementTree as ET
from urllib.parse import urlparse, unquote, quote
from concurrent.futures import ThreadPoolExecutor
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# 程序版本号
VERSION = "v2026.05.20"

# 锁机制
db_lock = threading.Lock()
dav_write_lock = threading.Lock()
cleanup_lock = threading.Lock()
pending_cleanups = {}

# ANSI 颜色定义 (仅供 ColorConsoleHandler 涂色使用)
_C_G = "\033[92m"; _C_Y = "\033[93m"; _C_R = "\033[91m"; _C_B = "\033[94m"
_C_P = "\033[95m"; _C_C = "\033[96m"; _C_BOLD = "\033[1m"; _C_END = "\033[0m"

# ================= 辅助函数 =================

def enable_windows_ansi():
    if platform.system().lower() == 'windows':
        os.system('')
        try:
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-11)
            mode = ctypes.c_uint()
            kernel32.GetConsoleMode(handle, ctypes.byref(mode))
            kernel32.SetConsoleMode(handle, mode.value | 7)
        except: pass

class ColorConsoleHandler(logging.StreamHandler):
    """ 动态着色引擎：仅在控制台实时染色，不污染日志文件 """
    def emit(self, record):
        try:
            msg = self.format(record)
            if "[检测到删除]" in msg: print(f"{_C_P}{msg}{_C_END}")
            elif "-> 已移入回收站" in msg: print(f"{_C_G}{msg}{_C_END}")
            elif "-> 已从云盘永久删除" in msg: print(f"{_C_R}{msg}{_C_END}")
            elif "[索引更新]" in msg: print(f"{_C_C}{msg}{_C_END}")
            elif "[二次清理]" in msg: print(f"{_C_Y}{_C_BOLD}{msg}{_C_END}")
            elif "[冗余清理]" in msg: print(f"{_C_Y}{_C_BOLD}{msg}{_C_END}")
            elif "[主动刷新]" in msg: print(f"{_C_B}{msg}{_C_END}")
            elif "[系统]" in msg or "[监控启动]" in msg: print(f"{_C_B}{msg}{_C_END}")
            elif "[配置警告]" in msg or "重试" in msg: print(f"{_C_Y}{msg}{_C_END}")
            elif "[错误]" in msg: print(f"{_C_R}{_C_BOLD}{msg}{_C_END}")
            else: print(msg)
        except Exception: self.handleError(record)

def init_logging():
    level_str = _cfg.get('Log', 'level', fallback='INFO').upper()
    log_file = _cfg.get('Log', 'file', fallback='activity.log')
    max_mb = _cfg.getint('Log', 'max_size_mb', fallback=2)
    max_bytes = max_mb * 1024 * 1024
    if log_dir := os.path.dirname(log_file):
        if not os.path.exists(log_dir): os.makedirs(log_dir)
    if os.path.exists(log_file):
        if os.path.getsize(log_file) > max_bytes:
            with open(log_file, 'w', encoding='utf-8') as f: f.write(f"--- 日志重置 ---\n")
        else:
            with open(log_file, 'a', encoding='utf-8') as f:
                f.write("\n\n    /\\/\\ 程序重新启动 /\\/\\ \n\n")
    formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', '%Y-%m-%d %H:%M:%S')
    file_handler = logging.FileHandler(log_file, encoding='utf-8'); file_handler.setFormatter(formatter)
    console_handler = ColorConsoleHandler(); console_handler.setFormatter(formatter)
    root_logger = logging.getLogger(); root_logger.setLevel(level_str)
    root_logger.addHandler(file_handler); root_logger.addHandler(console_handler)
    logging.getLogger("urllib3").setLevel(logging.WARNING); logging.getLogger("requests").setLevel(logging.WARNING)
    return logging.getLogger("FileOnly")

def manual_extract_list(section_name):
    config_path = os.path.join(os.path.dirname(__file__), 'config.ini')
    result = []
    if not os.path.exists(config_path): return result
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            in_section = False
            for line in f:
                line = line.strip()
                if line.startswith(f"[{section_name}]"): in_section = True; continue
                elif line.startswith("[") and in_section: break
                if in_section and line and not line.startswith(';'): result.append(line)
    except: pass
    return result

def load_config():
    config = configparser.ConfigParser(); config.optionxform = str 
    config_path = os.path.join(os.path.dirname(__file__), 'config.ini')
    if not os.path.exists(config_path): print(f"[错误] 找不到 config.ini"); sys.exit(1)
    with open(config_path, 'r', encoding='utf-8') as f: lines = f.readlines()
    safe_lines = []; skip = False
    for line in lines:
        if line.strip() in ("[MonitorFolders]", "[RefreshPaths]"): skip = True
        elif line.strip().startswith("[") and skip: skip = False
        if not skip: safe_lines.append(line)
    config.read_string("".join(safe_lines))
    return config

# ================= 加载配置与初始化 =================

_cfg = load_config(); init_logging()
MONITOR_FOLDERS = [os.path.abspath(os.path.normpath(f)) for f in manual_extract_list('MonitorFolders')]
REFRESH_PATHS   = manual_extract_list('RefreshPaths')
DB_FILE          = _cfg.get('Local', 'db_file')
WEBDAV_HOST      = _cfg.get('WebDAV', 'host').rstrip('/')
WEBDAV_USER      = _cfg.get('WebDAV', 'user')
WEBDAV_PWD       = _cfg.get('WebDAV', 'password')
WEBDAV_ACTION    = _cfg.get('Setting', 'action').upper()
TRASH_DIR_NAME   = _cfg.get('Setting', 'trash_dir_name')
REFRESH_INTERVAL = _cfg.getint('Setting', 'webdav_refresh_interval', fallback=0)
REFRESH_DEPTH    = _cfg.getint('Setting', 'webdav_refresh_depth', fallback=3)
GHOST_PROTECT_SEC= 60

def log_i(msg): logging.info(msg)
def log_f(msg): logging.getLogger("FileOnly").info(msg)
def log_d(msg): logging.debug(msg)
def log_e(msg, exc=False): logging.error(msg, exc_info=exc)

def check_log_size():
    log_file = _cfg.get('Log', 'file', fallback='activity.log')
    max_mb = _cfg.getint('Log', 'max_size_mb', fallback=2)
    if os.path.exists(log_file) and os.path.getsize(log_file) > max_mb * 1024 * 1024:
        with open(log_file, 'w', encoding='utf-8') as f: f.write("--- 运行期间日志已重置 ---\n")

# ================= WebDAV 客户端 =================

class OpenlistWebDAV:
    def __init__(self, host, user, pwd):
        self.host = host; self.auth = (user, pwd)
        self.session = requests.Session(); self.session.auth = self.auth
        adapter = requests.adapters.HTTPAdapter(pool_connections=50, pool_maxsize=50)
        self.session.mount('http://', adapter); self.session.mount('https://', adapter)

    def _url(self, path):
        return self.host + quote('/' + path.lstrip('/'), safe='/')

    def check_exists(self, path):
        try:
            res = self.session.request("PROPFIND", self._url(path), headers={"Depth": "0"}, timeout=10)
            return res.status_code in (200, 207)
        except: return False

    def list_contents(self, path):
        results = {"folders": [], "files": []}
        try:
            res = self.session.request("PROPFIND", self._url(path), headers={"Depth": "1"}, timeout=30)
            if res.status_code in (200, 207):
                root = ET.fromstring(res.text); ns = {'d': 'DAV:'}
                for resp in root.findall('d:response', ns):
                    href = unquote(resp.find('d:href', ns).text)
                    rel_path = href[len(urlparse(self.host).path):].rstrip('/') if href.lower().startswith(urlparse(self.host).path.lower()) else urlparse(href).path.rstrip('/')
                    if not rel_path or rel_path.lower() == path.rstrip('/').lower(): continue
                    is_dir = False
                    pstat = resp.find('d:propstat', ns)
                    if pstat is not None:
                        p = pstat.find('d:prop', ns); rt = p.find('d:resourcetype', ns) if p is not None else None
                        if rt is not None and rt.find('d:collection', ns) is not None: is_dir = True
                    if is_dir: results["folders"].append(rel_path)
                    else: results["files"].append(rel_path)
                return results
            elif res.status_code == 404: return "404_NOT_FOUND"
        except: pass
        return "ERROR"

    def move(self, src_path, dst_path):
        headers = {"Destination": self._url(dst_path), "Overwrite": "T"}
        for attempt in range(3):
            try:
                res = self.session.request("MOVE", self._url(src_path), headers=headers, timeout=20)
                if res.status_code in (201, 204, 207): return
                err = f"HTTP {res.status_code}"
                if res.status_code == 423: err = "HTTP 423 (资源锁定)"; time.sleep(2)
                if attempt < 2: time.sleep(1.5)
                else: raise Exception(f"{err}: {res.text}")
            except Exception as e:
                if attempt == 2: raise e
                time.sleep(2)

    def delete(self, path):
        for attempt in range(3):
            try:
                res = requests.request("DELETE", self._url(path), auth=self.auth, timeout=15)
                if res.status_code in (200, 202, 204): return
                err = "HTTP 423 (资源锁定)" if res.status_code == 423 else f"HTTP {res.status_code}"
                if res.status_code == 423: time.sleep(2)
                if attempt < 2: time.sleep(1.5)
                else: raise Exception(f"{err}: {res.text}")
            except Exception as e:
                if attempt == 2: raise e
                time.sleep(2)

    def makedirs(self, path):
        parts = [p for p in path.strip('/').split('/') if p]; curr = ""
        for p in parts:
            curr += "/" + p
            if not self.check_exists(curr):
                res = self.session.request("MKCOL", self._url(curr))
                if res.status_code in (201, 207):
                    log_d(f"创建云盘目录: {curr}"); time.sleep(0.3)

client = OpenlistWebDAV(WEBDAV_HOST, WEBDAV_USER, WEBDAV_PWD)

# ================= 业务逻辑 =================

def is_in_scope(path):
    if not REFRESH_PATHS: return False
    p_norm = path.rstrip('/')
    for rp in REFRESH_PATHS:
        rp_norm = rp.rstrip('/')
        if p_norm == rp_norm or p_norm.startswith(rp_norm + "/"): return True
    return False

def init_db():
    if db_dir := os.path.dirname(DB_FILE):
        if not os.path.exists(db_dir): os.makedirs(db_dir)
    with db_lock:
        conn = sqlite3.connect(DB_FILE); cursor = conn.cursor()
        cursor.execute('CREATE TABLE IF NOT EXISTS strm_files (local_path TEXT PRIMARY KEY COLLATE NOCASE, webdav_path TEXT)')
        cursor.execute('CREATE TABLE IF NOT EXISTS ghost_files (webdav_path TEXT PRIMARY KEY, expire_time REAL)')
        cursor.execute('CREATE TABLE IF NOT EXISTS known_folders (folder_path TEXT PRIMARY KEY)')
        conn.commit(); conn.close()

def save_known_folder(path):
    if not path or path == "/" or TRASH_DIR_NAME in path: return
    try:
        with db_lock:
            conn = sqlite3.connect(DB_FILE); cursor = conn.cursor()
            parts = [p for p in path.strip('/').split('/') if p]; curr = ""
            for p in parts:
                curr += "/" + p
                if TRASH_DIR_NAME not in curr: cursor.execute('INSERT OR IGNORE INTO known_folders VALUES (?)', (curr,))
            conn.commit(); conn.close()
    except: pass

def remove_known_folder(path):
    try:
        with db_lock:
            conn = sqlite3.connect(DB_FILE); cursor = conn.cursor()
            cursor.execute('DELETE FROM known_folders WHERE folder_path = ?', (path,))
            cursor.execute('DELETE FROM known_folders WHERE folder_path LIKE ?', (path + "/%",))
            conn.commit(); conn.close()
    except: pass

def remove_empty_local_dirs():
    for root_folder in MONITOR_FOLDERS:
        if not os.path.exists(root_folder): continue
        for root, dirs, files in os.walk(root_folder, topdown=False):
            if root.lower() == root_folder.lower(): continue
            if not os.listdir(root):
                try: os.rmdir(root); log_f(f"[清理] 移除本地空目录: {root}")
                except: pass

def sync_folders_from_strms():
    log_i("正在从 strm 索引中同步已知目录...")
    try:
        with db_lock:
            conn = sqlite3.connect(DB_FILE); cursor = conn.cursor()
            cursor.execute('SELECT webdav_path FROM strm_files'); rows = cursor.fetchall()
        count = 0
        for row in rows:
            if is_in_scope(row[0]):
                parent = "/" + "/".join(row[0].strip('/').split('/')[:-1])
                save_known_folder(parent); count += 1
        log_i(f"同步完毕，已补全清单内 {count} 条路径索引。")
    except Exception as e: log_e(f"同步目录结构失败: {e}")

def parse_strm_content(content):
    content = content.strip()
    if not content.startswith('http'): return content
    path = unquote(urlparse(content).path) 
    return path[2:] if path.startswith('/d/') else path

class StrmMonitorHandler(FileSystemEventHandler):
    def on_created(self, event):
        if not event.is_directory and event.src_path.endswith('.strm'): 
            self.add_to_db(os.path.abspath(os.path.normpath(event.src_path)))
    def on_modified(self, event):
        if not event.is_directory and event.src_path.endswith('.strm'): 
            self.add_to_db(os.path.abspath(os.path.normpath(event.src_path)))
    def on_deleted(self, event):
        if not event.is_directory and event.src_path.endswith('.strm'):
            threading.Thread(target=self.handle_deletion, args=(os.path.abspath(os.path.normpath(event.src_path)),)).start()

    def add_to_db(self, local_path, is_watchdog=True):
        try:
            if is_watchdog: time.sleep(0.5) 
            with open(local_path, 'r', encoding='utf-8') as f: content = f.read()
            webdav_path = parse_strm_content(content)
            if webdav_path:
                with db_lock:
                    conn = sqlite3.connect(DB_FILE); cursor = conn.cursor()
                    now = time.time()
                    cursor.execute('SELECT expire_time FROM ghost_files WHERE webdav_path = ?', (webdav_path,))
                    ghost = cursor.fetchone()
                    if ghost and ghost[0] > now:
                        log_i(f"[二次清理] 自动拦截并抹除刚生成的重复索引: {os.path.basename(local_path)}")
                        conn.close()
                        try: os.remove(local_path)
                        except: pass
                        return
                    cursor.execute('SELECT webdav_path FROM strm_files WHERE local_path = ?', (local_path,))
                    row = cursor.fetchone()
                    if row and row[0] == webdav_path: conn.close(); return
                    cursor.execute('INSERT OR REPLACE INTO strm_files VALUES (?, ?)', (local_path, webdav_path))
                    conn.commit(); conn.close()
                log_i(f"[索引更新] {os.path.basename(local_path)} -> {webdav_path}")
                save_known_folder("/" + "/".join(webdav_path.strip('/').split('/')[:-1]))
                trigger_delayed_cleanup("/" + "/".join(webdav_path.strip('/').split('/')[:-1]))
        except: pass

    def handle_deletion(self, local_path):
        with db_lock:
            conn = sqlite3.connect(DB_FILE); cursor = conn.cursor()
            cursor.execute('SELECT webdav_path FROM strm_files WHERE local_path = ?', (local_path,))
            result = cursor.fetchone()
            if not result: conn.close(); return 
            webdav_path = result[0]
            cursor.execute('INSERT OR REPLACE INTO ghost_files VALUES (?, ?)', (webdav_path, time.time() + GHOST_PROTECT_SEC))
            cursor.execute('DELETE FROM strm_files WHERE local_path = ?', (local_path,))
            conn.commit(); conn.close()

        log_i(f"[检测到删除] {os.path.basename(local_path)}")
        with dav_write_lock:
            try:
                if client.check_exists(webdav_path):
                    if WEBDAV_ACTION == "DELETE":
                        client.delete(webdav_path); log_i(f" -> 已从云盘永久删除: {webdav_path}")
                    elif WEBDAV_ACTION == "MOVE":
                        parts = webdav_path.strip('/').split('/')
                        tdir = f"/{parts[0]}/{TRASH_DIR_NAME}/{'/'.join(parts[1:-1])}".rstrip('/')
                        tfile = f"{tdir}/{parts[-1]}"
                        client.makedirs(tdir); client.move(webdav_path, tfile); log_i(f" -> 已移入回收站: {tfile}")
                else: log_d(f" -> 云盘文件已不存在")
                remove_empty_local_dirs(); check_log_size()
            except Exception as e: log_e(f" -> 联动操作失败: {e}")

def scan_existing_files(target_folder=None):
    start_t = time.time(); conn = sqlite3.connect(DB_FILE); cursor = conn.cursor()
    cursor.execute('SELECT local_path, webdav_path FROM strm_files'); db_records = dict(cursor.fetchall())
    new_updates = []; count_skip = 0
    folders = [target_folder] if target_folder else MONITOR_FOLDERS
    for folder in folders:
        if not os.path.exists(folder): continue
        for root, _, files in os.walk(folder):
            for file in files:
                if file.endswith('.strm'):
                    lp = os.path.abspath(os.path.normpath(os.path.join(root, file)))
                    if lp in db_records: count_skip += 1; continue
                    try:
                        with open(lp, 'r', encoding='utf-8') as f: wp = parse_strm_content(f.read())
                        if wp: new_updates.append((lp, wp))
                    except: continue
    if new_updates: cursor.executemany('INSERT OR REPLACE INTO strm_files VALUES (?, ?)', new_updates); conn.commit()
    conn.close()
    if not target_folder: log_i(f"全量同步完成！耗时: {time.time()-start_t:.2f}s (跳过: {count_skip}, 新增: {len(new_updates)})")
    remove_empty_local_dirs()

# ================= 冗余清理 (纯本地且非破坏性) =================

def handle_cascade_delete(webdav_folder_path):
    if any(webdav_folder_path.rstrip('/') == rp.rstrip('/') for rp in REFRESH_PATHS):
        log_e(f"[配置警告] 云端根路径访问失败: {webdav_folder_path}，已拦截。"); return
    try:
        with db_lock:
            conn = sqlite3.connect(DB_FILE); cursor = conn.cursor()
            cursor.execute('SELECT local_path, webdav_path FROM strm_files WHERE webdav_path LIKE ?', (webdav_folder_path + "/%",))
            targets = cursor.fetchall()
            for _, wp in targets: cursor.execute('INSERT OR REPLACE INTO ghost_files VALUES (?, ?)', (wp, time.time() + GHOST_PROTECT_SEC))
            cursor.execute('DELETE FROM strm_files WHERE webdav_path LIKE ?', (webdav_folder_path + "/%",))
            cursor.execute('DELETE FROM known_folders WHERE folder_path = ?', (webdav_folder_path,))
            cursor.execute('DELETE FROM known_folders WHERE folder_path LIKE ?', (webdav_folder_path + "/%",))
            conn.commit(); conn.close()
        for item in targets:
            if os.path.exists(item[0]):
                try: os.remove(item[0]); log_i(f"[冗余清理] 移除失效索引: {os.path.basename(item[0])}")
                except: pass
        remove_empty_local_dirs()
    except Exception as e: log_d(f"级联清理失败: {e}")

def cleanup_zombie_strms(parent_webdav_folder, remote_files):
    try:
        with db_lock:
            conn = sqlite3.connect(DB_FILE); cursor = conn.cursor()
            cursor.execute('SELECT local_path, webdav_path FROM strm_files WHERE webdav_path LIKE ?', (parent_webdav_folder + "/%",))
            db_entries = cursor.fetchall(); conn.close()
        for local_path, webdav_path in db_entries:
            if "/" in webdav_path[len(parent_webdav_folder)+1:]: continue
            if webdav_path not in remote_files:
                log_i(f"[冗余清理] 移除本地失效索引: {os.path.basename(local_path)}")
                with db_lock:
                    conn = sqlite3.connect(DB_FILE); cursor = conn.cursor()
                    cursor.execute('INSERT OR REPLACE INTO ghost_files VALUES (?, ?)', (webdav_path, time.time() + GHOST_PROTECT_SEC))
                    cursor.execute('DELETE FROM strm_files WHERE local_path = ?', (local_path,)); conn.commit(); conn.close()
                if os.path.exists(local_path):
                    try: os.remove(local_path)
                    except: pass
    except: pass

# ================= 刷新引擎 =================

def trigger_delayed_cleanup(webdav_folder):
    with cleanup_lock:
        if webdav_folder in pending_cleanups: pending_cleanups[webdav_folder].cancel()
        t = threading.Timer(10.0, execute_targeted_cleanup, args=[webdav_folder])
        pending_cleanups[webdav_folder] = t; t.start()

def execute_targeted_cleanup(folder_path):
    with dav_write_lock:
        log_f(f"[冗余校验] 检查变动: {folder_path}")
        results = client.list_contents(folder_path)
        if results == "404_NOT_FOUND": handle_cascade_delete(folder_path)
        elif isinstance(results, dict):
            if results["files"]: cleanup_zombie_strms(folder_path, results["files"])
            remove_empty_local_dirs()
    with cleanup_lock:
        if folder_path in pending_cleanups: del pending_cleanups[folder_path]

def refresh_folder_task(webdav_path, depth=0, known_set=None):
    if not is_in_scope(webdav_path): return
    results = client.list_contents(webdav_path)
    if results == "404_NOT_FOUND": handle_cascade_delete(webdav_path); return
    if not isinstance(results, dict): return
    if results["files"]: cleanup_zombie_strms(webdav_path, results["files"])
    for sd in results["folders"]:
        if TRASH_DIR_NAME in sd: continue
        if known_set is not None and sd not in known_set:
            log_i(f"[主动刷新] 嗅探到新目录: {sd}")
            save_known_folder(sd); known_set.add(sd)
            refresh_folder_task(sd, depth - 1, known_set)

def execute_refresh_cycle():
    log_i(f"[主动刷新] 正在执行并发体检与嗅探...")
    with db_lock:
        conn = sqlite3.connect(DB_FILE); cursor = conn.cursor()
        cursor.execute('SELECT folder_path FROM known_folders'); known_set = {row[0] for row in cursor.fetchall()}; conn.close()
    for rp in REFRESH_PATHS: save_known_folder(rp); known_set.add(rp)
    targets = [p for p in known_set if is_in_scope(p)]
    with ThreadPoolExecutor(max_workers=10) as executor:
        executor.map(lambda p: refresh_folder_task(p, REFRESH_DEPTH, known_set), targets)
    log_i(f"[主动刷新] 指令发送完毕，等待 15s 待工具同步...")
    time.sleep(15); scan_existing_files()
    log_i(f"[主动刷新] 本轮自愈同步完毕。")

def webdav_refresh_worker():
    if REFRESH_INTERVAL <= 0 or not REFRESH_PATHS: return
    execute_refresh_cycle()
    while True:
        time.sleep(REFRESH_INTERVAL * 60); execute_refresh_cycle()

# ================= 主程序启动 =================

if __name__ == "__main__":
    enable_windows_ansi(); init_db()
    print("\n" + "="*60)
    print(f"{_C_B}{_C_BOLD}   Openlist Strm Monitor {VERSION}{_C_END}")
    print("="*60)
    mode_text = f"{_C_G}[ 安全回收: MOVE ]" if WEBDAV_ACTION == "MOVE" else f"{_C_R}[ 永久删除: DELETE ]"
    print(f"当前模式: {mode_text}{_C_END}\n"+"="*60 + "\n")

    scan_existing_files(); sync_folders_from_strms()
    threading.Thread(target=webdav_refresh_worker, daemon=True).start()
    
    event_handler = StrmMonitorHandler()
    observer = Observer(); active = 0
    for folder in MONITOR_FOLDERS:
        if os.path.exists(folder):
            observer.schedule(event_handler, folder, recursive=True); active += 1
            log_i(f"[监控启动] {folder}")
    if active == 0: log_e("没有有效的监控路径"); sys.exit(1)
    observer.start()
    print(f"\n[系统] 成功启动 {active} 个监控任务。")
    try:
        if sys.stdin and sys.stdin.isatty():
            print(f"\n{_C_Y}[提示]{_C_END} 输入 'q' 并回车退出监控\n")
            while True:
                if input().strip().lower() == 'q': break
        else:
            while True: time.sleep(1)
    except (KeyboardInterrupt, EOFError): pass
    print("\n[系统] 正在安全停止监控线程...")
    observer.stop(); observer.join()
    log_f(f"程序已安全停止 - {VERSION}")
    log_f("==================================================\n\n\n")