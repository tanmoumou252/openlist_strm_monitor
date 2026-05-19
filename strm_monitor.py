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
import xml.etree.ElementTree as ET
from urllib.parse import urlparse, unquote, quote
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# 程序版本号
VERSION = "v2026.05.19"

dav_lock = threading.Lock()

# ANSI 颜色代码定义 (仅供 UI 和 ConsoleHandler 使用)
C_GREEN = "\033[92m"
C_YELLOW = "\033[93m"
C_RED = "\033[91m"
C_BLUE = "\033[94m"
C_PURPLE = "\033[95m"
C_CYAN = "\033[96m"
C_BOLD = "\033[1m"
C_END = "\033[0m"

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
    """ 智能染色处理器：仅在控制台输出时根据关键字上色，不影响日志文件 """
    def emit(self, record):
        try:
            msg = self.format(record)
            if "[检测到删除]" in msg: print(f"{C_PURPLE}{msg}{C_END}")
            elif "-> 已移入回收站" in msg: print(f"{C_GREEN}{msg}{C_END}")
            elif "-> 已从云盘永久删除" in msg: print(f"{C_RED}{msg}{C_END}")
            elif "[索引更新]" in msg: print(f"{C_CYAN}{msg}{C_END}")
            elif "[幽灵拦截]" in msg: print(f"{C_YELLOW}{msg}{C_END}")
            elif "[主动刷新]" in msg: print(f"{C_BLUE}{msg}{C_END}")
            elif "[配置警告]" in msg: print(f"{C_YELLOW}{C_BOLD}{msg}{C_END}")
            elif "重试" in msg: print(f"{C_YELLOW}{msg}{C_END}")
            elif "[错误]" in msg or "失败" in msg: print(f"{C_RED}{C_BOLD}{msg}{C_END}")
            elif "[监控启动]" in msg or "[系统]" in msg: print(f"{C_BLUE}{msg}{C_END}")
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
            with open(log_file, 'w', encoding='utf-8') as f: f.write(f"--- 日志已重置 ---\n")
        else:
            with open(log_file, 'a', encoding='utf-8') as f:
                f.write("\n\n    /\\/\\/\\ 程序重新启动 /\\/\\/\\ \n\n")
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

# ================= 加载配置 =================

_cfg = load_config()
file_logger = init_logging()

MONITOR_FOLDERS = manual_extract_list('MonitorFolders')
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

    def _url(self, path):
        return self.host + quote('/' + path.lstrip('/'), safe='/')

    def check_exists(self, path):
        try:
            res = self.session.request("PROPFIND", self._url(path), headers={"Depth": "0"}, timeout=10)
            return res.status_code in (200, 207)
        except: return False

    def list_subfolders(self, path):
        subfolders = []
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
                    if is_dir: subfolders.append(rel_path)
            elif res.status_code == 404: return None
        except: pass
        return subfolders

    def move(self, src_path, dst_path):
        headers = {"Destination": self._url(dst_path), "Overwrite": "T"}
        for attempt in range(3):
            try:
                res = self.session.request("MOVE", self._url(src_path), headers=headers, timeout=20)
                if res.status_code in (201, 204, 207): return
                err = f"HTTP {res.status_code}"; 
                if res.status_code == 423: err = "HTTP 423 (资源锁定)"
                if attempt < 2: log_i(f" -> [重试 {attempt+1}] WebDAV {err}..."); time.sleep(2)
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
                if attempt < 2: log_i(f" -> [重试 {attempt+1}] WebDAV {err}..."); time.sleep(2)
                else: raise Exception(f"{err}: {res.text}")
            except Exception as e:
                if attempt == 2: raise e
                time.sleep(2)

    def makedirs(self, path):
        parts = [p for p in path.strip('/').split('/') if p]
        current_path = ""; created = False
        for part in parts:
            current_path += "/" + part
            if not self.check_exists(current_path):
                res = self.session.request("MKCOL", self._url(current_path))
                if res.status_code in (201, 207):
                    log_d(f"创建云盘目录: {current_path}"); created = True; time.sleep(0.3)
        if created: time.sleep(0.7)

client = OpenlistWebDAV(WEBDAV_HOST, WEBDAV_USER, WEBDAV_PWD)

# ================= 业务逻辑 =================

def init_db():
    if db_dir := os.path.dirname(DB_FILE):
        if not os.path.exists(db_dir): os.makedirs(db_dir)
    conn = sqlite3.connect(DB_FILE); cursor = conn.cursor()
    cursor.execute('CREATE TABLE IF NOT EXISTS strm_files (local_path TEXT PRIMARY KEY, webdav_path TEXT)')
    cursor.execute('CREATE TABLE IF NOT EXISTS ghost_files (webdav_path TEXT PRIMARY KEY, expire_time REAL)')
    cursor.execute('CREATE TABLE IF NOT EXISTS known_folders (folder_path TEXT PRIMARY KEY)')
    conn.commit(); conn.close()

def save_known_folder(path):
    if not path or path == "/": return
    try:
        conn = sqlite3.connect(DB_FILE); cursor = conn.cursor()
        parts = [p for p in path.strip('/').split('/') if p]
        for i in range(len(parts)):
            p = "/" + "/".join(parts[:i+1])
            cursor.execute('INSERT OR IGNORE INTO known_folders VALUES (?)', (p,))
        conn.commit(); conn.close()
    except: pass

def remove_known_folder(path):
    try:
        conn = sqlite3.connect(DB_FILE); cursor = conn.cursor()
        cursor.execute('DELETE FROM known_folders WHERE folder_path = ?', (path,))
        conn.commit(); conn.close()
    except: pass

def sync_folders_from_strms():
    log_i("正在从 strm 索引中同步已知目录...")
    try:
        conn = sqlite3.connect(DB_FILE); cursor = conn.cursor()
        cursor.execute('SELECT webdav_path FROM strm_files')
        rows = cursor.fetchall(); count = 0
        for row in rows:
            wpath = row[0]
            if any(wpath.startswith(rp) for rp in REFRESH_PATHS):
                parent = "/" + "/".join(wpath.strip('/').split('/')[:-1])
                parts = [p for p in parent.strip('/').split('/') if p]
                for i in range(len(parts)):
                    p = "/" + "/".join(parts[:i+1])
                    cursor.execute('INSERT OR IGNORE INTO known_folders VALUES (?)', (p,))
                count += 1
        conn.commit(); conn.close()
        log_i(f"同步完毕，已补全配置中启用刷新路径的 {count} 层目录结构。")
    except Exception as e: log_e(f"同步目录结构失败: {e}")

def parse_strm_content(content):
    content = content.strip()
    if not content.startswith('http'): return content
    path = unquote(urlparse(content).path) 
    return path[2:] if path.startswith('/d/') else path

def get_dynamic_trash_path(webdav_path):
    parts = webdav_path.strip('/').split('/')
    if len(parts) >= 2:
        drive_name, relative_path, filename = parts[0], "/".join(parts[1:-1]), parts[-1]
        target_dir = f"/{drive_name}/{TRASH_DIR_NAME}/{relative_path}".rstrip('/')
        return target_dir, f"{target_dir}/{filename}"
    return None, None

class StrmMonitorHandler(FileSystemEventHandler):
    def on_created(self, event):
        if not event.is_directory and event.src_path.endswith('.strm'): self.add_to_db(event.src_path)
    def on_modified(self, event):
        if not event.is_directory and event.src_path.endswith('.strm'): self.add_to_db(event.src_path)
    def on_deleted(self, event):
        if not event.is_directory and event.src_path.endswith('.strm'):
            threading.Thread(target=self.handle_deletion, args=(event.src_path,)).start()

    def add_to_db(self, local_path, is_watchdog=True):
        with dav_lock:
            try:
                if is_watchdog: time.sleep(0.5) 
                with open(local_path, 'r', encoding='utf-8') as f: content = f.read()
                webdav_path = parse_strm_content(content)
                if webdav_path:
                    parent_folder = "/" + "/".join(webdav_path.strip('/').split('/')[:-1])
                    save_known_folder(parent_folder)
                    conn = sqlite3.connect(DB_FILE); cursor = conn.cursor()
                    now = time.time()
                    cursor.execute('DELETE FROM ghost_files WHERE expire_time <= ?', (now,))
                    cursor.execute('SELECT expire_time FROM ghost_files WHERE webdav_path = ?', (webdav_path,))
                    ghost = cursor.fetchone()
                    if ghost and ghost[0] > now:
                        log_i(f"[幽灵拦截] 工具重复生成刚删除的文件，已自动阻断: {os.path.basename(local_path)}")
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
            except: pass

    def handle_deletion(self, local_path):
        with dav_lock:
            conn = sqlite3.connect(DB_FILE); cursor = conn.cursor()
            cursor.execute('SELECT webdav_path FROM strm_files WHERE local_path = ?', (local_path,))
            result = cursor.fetchone()
            if result:
                webdav_path = result[0]
                log_i(f"[检测到删除] {os.path.basename(local_path)}")
                try:
                    if client.check_exists(webdav_path):
                        if WEBDAV_ACTION == "DELETE": client.delete(webdav_path); log_i(f" -> 已从云盘永久删除")
                        elif WEBDAV_ACTION == "MOVE":
                            tdir, tfile = get_dynamic_trash_path(webdav_path)
                            if tdir: client.makedirs(tdir); client.move(webdav_path, tfile); log_i(f" -> 已移入回收站")
                        cursor.execute('INSERT OR REPLACE INTO ghost_files VALUES (?, ?)', (webdav_path, time.time() + GHOST_PROTECT_SEC))
                    else: log_d(f" -> 云盘文件已不存在")
                    cursor.execute('DELETE FROM strm_files WHERE local_path = ?', (local_path,))
                    conn.commit(); check_log_size()
                except Exception as e: log_e(f" -> 联动操作失败: {e}")
            conn.close()

def scan_existing_files():
    log_i("正在快速同步本地 strm 索引...")
    start_t = time.time(); conn = sqlite3.connect(DB_FILE); cursor = conn.cursor()
    cursor.execute('DELETE FROM ghost_files WHERE expire_time <= ?', (time.time(),))
    cursor.execute('SELECT local_path, webdav_path FROM strm_files'); db_records = dict(cursor.fetchall())
    new_updates = []; count_skip = 0
    for folder in MONITOR_FOLDERS:
        if not os.path.exists(folder): log_e(f"配置路径不存在: {folder}"); continue
        for root, dirs, files in os.walk(folder):
            for file in files:
                if file.endswith('.strm'):
                    lp = os.path.join(root, file)
                    if lp in db_records: count_skip += 1; continue
                    try:
                        with open(lp, 'r', encoding='utf-8') as f: wp = parse_strm_content(f.read())
                        if wp: new_updates.append((lp, wp))
                    except: continue
    if new_updates: cursor.executemany('INSERT OR REPLACE INTO strm_files VALUES (?, ?)', new_updates); conn.commit()
    conn.close()
    log_i(f"同步完成！耗时: {time.time()-start_t:.2f}s (跳过: {count_skip}, 新增: {len(new_updates)})")

# ================= WebDAV 主动探针刷新 =================

def webdav_refresh_worker():
    if REFRESH_INTERVAL <= 0 or not REFRESH_PATHS: return
    
    while True:
        time.sleep(REFRESH_INTERVAL * 60)
        log_i(f"[主动刷新] 正在探测新资源并唤醒云盘...")
        
        conn = sqlite3.connect(DB_FILE); cursor = conn.cursor()
        cursor.execute('SELECT folder_path FROM known_folders'); known_set = {row[0] for row in cursor.fetchall()}; conn.close()

        def probe_and_expand_new(path, depth):
            if depth <= 0: return
            with dav_lock:
                log_f(f"[主动刷新] 探测路径: {path}")
                subdirs = client.list_subfolders(path)
            if subdirs is None:
                remove_known_folder(path); return
            
            for sd in subdirs:
                if sd not in known_set:
                    log_i(f"[主动刷新] 嗅探到新目录: {sd}")
                    save_known_folder(sd); known_set.add(sd)
                    probe_and_expand_new(sd, depth - 1)

        for root_path in REFRESH_PATHS:
            probe_and_expand_new(root_path, REFRESH_DEPTH)
        
        log_i(f"[主动刷新] 本轮嗅探完毕。")

# ================= 主程序启动 =================

if __name__ == "__main__":
    enable_windows_ansi(); init_db()
    print("\n" + "="*60)
    print(f"{C_BLUE}{C_BOLD}   Openlist Strm Monitor {VERSION}{C_END}")
    print("="*60)
    if WEBDAV_ACTION == "MOVE":
        print(f"当前模式: {C_GREEN}{C_BOLD}[ 安全回收: MOVE ]{C_END}")
        print(f"操作说明: 联动时将视频带层级结构转移至云盘 {C_YELLOW}{TRASH_DIR_NAME}{C_END} 目录。")
    else:
        print(f"当前模式: {C_RED}{C_BOLD}[ 永久删除: DELETE ]{C_END}")
        print(f"操作说明: {C_RED}警告！{C_END}联动时将直接从云盘永久删除原始视频。")
    print("="*60 + "\n")

    refresh_status_msg = "未启用"
    if REFRESH_INTERVAL > 0 and not REFRESH_PATHS: log_e(f"[配置警告] 已设刷新间隔但未配置路径！")
    elif REFRESH_INTERVAL <= 0 and REFRESH_PATHS: log_e(f"[配置警告] 已设刷新路径但间隔为 0！")
    elif REFRESH_INTERVAL > 0 and REFRESH_PATHS: refresh_status_msg = f"启用 ({REFRESH_INTERVAL} 分钟, 深度: {REFRESH_DEPTH})"

    log_f("==================================================")
    log_f(f"程序启动 - Openlist Strm Monitor {VERSION}")
    log_f(f"运行模式: {WEBDAV_ACTION} | 回收站: {TRASH_DIR_NAME}")
    log_f(f"系统环境: {platform.platform()} (Python {sys.version.split()[0]})")
    log_f(f"数据库路径: {os.path.abspath(DB_FILE)}")
    log_f(f"监控目录: \n    " + '\n    '.join(MONITOR_FOLDERS))
    log_f(f"主动刷新: {refresh_status_msg}")
    if REFRESH_INTERVAL > 0: log_f(f"刷新列表: \n    " + '\n    '.join(REFRESH_PATHS))
    log_f("==================================================")

    scan_existing_files()
    sync_folders_from_strms()
    threading.Thread(target=webdav_refresh_worker, daemon=True).start()
    
    event_handler = StrmMonitorHandler()
    observer = Observer(); active = 0
    for folder in MONITOR_FOLDERS:
        if os.path.exists(folder):
            observer.schedule(event_handler, folder, recursive=True)
            log_i(f"[监控启动] {folder}"); active += 1
    if active == 0: log_e("没有有效的监控路径"); sys.exit(1)
    observer.start()
    print(f"\n[系统] 成功启动 {active} 个监控任务。")
    try:
        if sys.stdin and sys.stdin.isatty():
            print(f"\n{C_YELLOW}[提示]{C_END} 输入 'q' 并回车停止监控并返回菜单") 
            while True:
                if input().strip().lower() == 'q': break
        else:
            while True: time.sleep(1)
    except (KeyboardInterrupt, EOFError): pass

    print("\n[系统] 正在安全停止监控线程，请稍候...")
    observer.stop(); observer.join()
    log_f(f"程序已安全停止 - {VERSION}")
    log_f("==================================================\n\n\n")