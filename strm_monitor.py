import os
import sqlite3
import time
import sys
import requests
import configparser
from urllib.parse import urlparse, unquote, quote
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# ================= 配置加载逻辑 =================

def load_config():
    config = configparser.ConfigParser()
    config_path = os.path.join(os.path.dirname(__file__), 'config.ini')
    
    if not os.path.exists(config_path):
        print(f"[错误] 找不到配置文件: {config_path}")
        print("请参考模板创建 config.ini 文件。")
        input("按回车键退出...")
        sys.exit(1)

    # 使用 utf-8 编码读取，防止中文路径乱码
    config.read(config_path, encoding='utf-8')
    return config

# 实例化配置
_cfg = load_config()

# 映射变量
folders_str = _cfg.get('Local', 'monitor_folders')
MONITOR_FOLDERS = [f.strip() for f in folders_str.split(',') if f.strip()]
DB_FILE        = _cfg.get('Local', 'db_file')

WEBDAV_HOST    = _cfg.get('WebDAV', 'host')
WEBDAV_USER    = _cfg.get('WebDAV', 'user')
WEBDAV_PWD     = _cfg.get('WebDAV', 'password')

WEBDAV_ACTION  = _cfg.get('Setting', 'action').upper()
TRASH_DIR_NAME = _cfg.get('Setting', 'trash_dir_name')

# ============================================

class OpenlistWebDAV:
        # 更轻量、更稳定的纯 requests WebDAV 客户端
    def __init__(self, host, user, pwd):
        self.host = host.rstrip('/')
        self.auth = (user, pwd)

    def _url(self, path):
        # quote 必须加上 safe='/' 参数，否则斜杠被编码会导致 500 错误
        if not path.startswith('/'):
            path = '/' + path
        return self.host + quote(path, safe='/')

    def check_exists(self, path):
        """检查文件/文件夹是否存在"""
        res = requests.request("PROPFIND", self._url(path), auth=self.auth, headers={"Depth": "0"})
        return res.status_code in (200, 207)

    def mkdir(self, path):
        """创建单层文件夹"""
        res = requests.request("MKCOL", self._url(path), auth=self.auth)
        # 201:创建成功; 405:已存在; 207:多状态响应(AList常用)
        return res.status_code in (201, 207, 405)

    def makedirs(self, path):
        """递归创建多层文件夹"""
        parts = [p for p in path.strip('/').split('/') if p]
        current_path = ""
        for part in parts:
            current_path += "/" + part
            if not self.check_exists(current_path):
                success = self.mkdir(current_path)
                if success:
                    print(f"       [创建目录] {current_path}")
                    # 给云盘一点点反应时间
                    time.sleep(0.2)
                else:
                    print(f"       [警告] 创建目录失败: {current_path}")

    def move(self, src_path, dst_path):
        """移动文件"""
        # Destination 必须是完整的 URL
        headers = {
            "Destination": self._url(dst_path),
            "Overwrite": "T"
        }
        res = requests.request("MOVE", self._url(src_path), auth=self.auth, headers=headers)
        if res.status_code not in (201, 204, 207):
            # 如果还是 500，打印出详细信息
            raise Exception(f"移动失败 [{res.status_code}]: {res.text}")

    def delete(self, path):
        """删除文件"""
        res = requests.request("DELETE", self._url(path), auth=self.auth)
        if res.status_code not in (200, 202, 204):
            raise Exception(f"删除失败 [{res.status_code}]: {res.text}")

# 初始化自定义客户端
client = OpenlistWebDAV(WEBDAV_HOST, WEBDAV_USER, WEBDAV_PWD)

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS strm_files (
            local_path TEXT PRIMARY KEY,
            webdav_path TEXT
        )
    ''')
    conn.commit()
    conn.close()

def parse_strm_content(content):
    content = content.strip()
    if not content.startswith('http'):
        return content
    parsed = urlparse(content)
    path = unquote(parsed.path) 
    if path.startswith('/d/'):
        path = path[2:] 
    return path

def get_dynamic_trash_path(webdav_path):
    """
    输入: /天翼云盘1TB/电影/A/1.mp4
    输出: (/天翼云盘1TB/回收站/电影/A, /天翼云盘1TB/回收站/电影/A/1.mp4)
    """
    parts = webdav_path.strip('/').split('/')
    if len(parts) >= 2:
        drive_name = parts[0]      # 盘符名: 天翼云盘1TB
        relative_folders = parts[1:-1] # 中间的路径: ['电影', 'A']
        filename = parts[-1]       # 文件名: 1.mp4
        
        # 构造目标文件夹路径: /天翼云盘1TB/回收站/电影/A
        relative_path_str = "/".join(relative_folders)
        if relative_path_str:
            target_dir = f"/{drive_name}/{TRASH_DIR_NAME}/{relative_path_str}"
        else:
            target_dir = f"/{drive_name}/{TRASH_DIR_NAME}"
            
        target_file = f"{target_dir}/{filename}"
        return target_dir, target_file
    
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
            self.handle_deletion(event.src_path)

    def add_to_db(self, local_path, is_watchdog=True): # 增加 is_watchdog 参数
        try:
            # 只有当它是实时监控触发的(is_watchdog=True)，才执行这0.5秒延迟
            if is_watchdog:
                time.sleep(0.5) 
            
            with open(local_path, 'r', encoding='utf-8') as f:
                content = f.read()
            webdav_path = parse_strm_content(content)
            
            if webdav_path:
                conn = sqlite3.connect(DB_FILE)
                cursor = conn.cursor()
                
                # 增加一个去重判断，如果数据库内容没变，就不打印日志，保持控制台干净
                cursor.execute('SELECT webdav_path FROM strm_files WHERE local_path = ?', (local_path,))
                row = cursor.fetchone()
                if row and row[0] == webdav_path:
                    conn.close()
                    return
                
                cursor.execute('INSERT OR REPLACE INTO strm_files (local_path, webdav_path) VALUES (?, ?)', (local_path, webdav_path))
                conn.commit()
                conn.close()
                print(f"[录入/更新] {local_path}")
        except Exception as e:
            pass

    def handle_deletion(self, local_path):
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute('SELECT webdav_path FROM strm_files WHERE local_path = ?', (local_path,))
        result = cursor.fetchone()
        
        if result:
            webdav_path = result[0]
            print(f"\n[触发] 删除了 strm: {local_path}")
            try:
                if client.check_exists(webdav_path):
                    if WEBDAV_ACTION == "DELETE":
                        client.delete(webdav_path)
                        print(f"       [成功] 已删除: {webdav_path}")
                    
                    elif WEBDAV_ACTION == "MOVE":
                        target_dir, target_file = get_dynamic_trash_path(webdav_path)
                        if target_dir:
                            # 递归创建回收站及其内部的子目录
                            client.makedirs(target_dir)
                            # 执行移动
                            client.move(webdav_path, target_file)
                            print(f"       [成功] 已移入回收站(保留路径): {target_file}")
                        else:
                            print("       [失败] 路径解析异常")
                else:
                    print(f"       [跳过] WebDAV 文件已不存在")

                cursor.execute('DELETE FROM strm_files WHERE local_path = ?', (local_path,))
                conn.commit()
            except Exception as e:
                print(f"       [错误] WebDAV 操作失败: {e}")
        conn.close()

def scan_existing_files():
    print("正在快速同步本地 strm 索引...")
    start_time = time.time()
    
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    # 先把数据库里的路径拿出来做成字典，查询速度极快
    cursor.execute('SELECT local_path, webdav_path FROM strm_files')
    db_records = dict(cursor.fetchall())
    
    new_updates = []
    count_skip = 0
    
    # --- 文件夹遍历循环 ---
    for folder in MONITOR_FOLDERS:
        if not os.path.exists(folder):
            print(f"[跳过] 路径不存在: {folder}")
            continue
        
        print(f" -> 正在扫描: {folder}")
        for root, dirs, files in os.walk(folder):
            for file in files:
                if file.endswith('.strm'):
                    local_path = os.path.join(root, file)
                    # 如果数据库里已经有了，直接跳过，不读硬盘文件
                    if local_path in db_records:
                        count_skip += 1
                        continue
                    try:
                        with open(local_path, 'r', encoding='utf-8') as f:
                            content = f.read()
                        webdav_path = parse_strm_content(content)
                        if webdav_path:
                            new_updates.append((local_path, webdav_path))
                    except:
                        continue
    # 批量入库
    if new_updates:
        cursor.executemany('INSERT OR REPLACE INTO strm_files VALUES (?, ?)', new_updates)
        conn.commit()
    conn.close()
    print(f"同步完成！耗时: {time.time() - start_time:.2f}秒 (跳过: {count_skip}, 新增: {len(new_updates)})")

if __name__ == "__main__":
    init_db()
    scan_existing_files()
    
    event_handler = StrmMonitorHandler()
    observer = Observer()
    
    # --- 为每个文件夹注册监控 ---
    active_count = 0
    for folder in MONITOR_FOLDERS:
        if os.path.exists(folder):
            # 每个路径都 schedule 一下，同一个 event_handler 可以复用
            observer.schedule(event_handler, folder, recursive=True)
            print(f"[监控中] {folder}")
            active_count += 1
        else:
            print(f"[警告] 无法启动监控，路径不存在: {folder}")
            
    if active_count == 0:
        print("[错误] 没有有效的监控路径，程序退出。")
        sys.exit(1)
        
    observer.start()
    print(f"\n共启动 {active_count} 个监控任务。")
    
    try:
        # 检测是否在控制台前台运行
        if sys.stdin and sys.stdin.isatty():
            print("等待触发删除操作...(输入 'q' 并按回车键停止监控并返回菜单)\n")
            while True:
                cmd = input()
                if cmd.strip().lower() == 'q':
                    break
        else:
            # 后台静默模式，无需输入
            while True:
                time.sleep(1)
                
    except (KeyboardInterrupt, EOFError):
        # 兼容偶尔按错 Ctrl+C 的情况
        pass

    print("\n正在安全停止监控线程...")
    observer.stop()
    observer.join()