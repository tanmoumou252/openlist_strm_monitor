# ==================== 标准库 ====================
import configparser
import ctypes
import datetime
import logging
import os
import platform
import shutil
import sqlite3
import sys
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Generator, Optional
from urllib.parse import quote, unquote, urlparse

# ==================== 第三方库 ====================
import requests
from requests.adapters import HTTPAdapter
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

# ==================== 版本信息 ====================
VERSION = "v2026.05.21"

# ANSI 颜色定义
_C_G = "\033[92m"
_C_Y = "\033[93m"
_C_R = "\033[91m"
_C_B = "\033[94m"
_C_P = "\033[95m"
_C_C = "\033[96m"
_C_BOLD = "\033[1m"
_C_END = "\033[0m"
_C_GRAY = "\033[90m"


# ==================== 日志系统 ====================

class ColorConsoleHandler(logging.StreamHandler):
    """动态着色引擎：仅在控制台实时染色，不污染日志文件"""
    
    COLOR_MAP = {
        'DEBUG': _C_GRAY,
        'INFO': _C_B,
        'WARNING': _C_Y,
        'ERROR': _C_R,
        'CRITICAL': _C_R + _C_BOLD,
    }
    
    def emit(self, record):
        try:
            msg = self.format(record)
            if "[检测到删除]" in msg:
                print(f"{_C_P}{msg}{_C_END}")
            elif "-> 已移入回收站" in msg:
                print(f"{_C_G}{msg}{_C_END}")
            elif "-> 已从云盘永久删除" in msg:
                print(f"{_C_R}{msg}{_C_END}")
            elif "[索引更新]" in msg:
                print(f"{_C_C}{msg}{_C_END}")
            elif "[二次清理]" in msg:
                print(f"{_C_Y}{_C_BOLD}{msg}{_C_END}")
            elif "[冗余清理]" in msg:
                print(f"{_C_Y}{_C_BOLD}{msg}{_C_END}")
            elif "[主动刷新]" in msg:
                print(f"{_C_B}{msg}{_C_END}")
            elif "[系统]" in msg or "[监控启动]" in msg:
                print(f"{_C_B}{msg}{_C_END}")
            elif "[配置警告]" in msg or "重试" in msg:
                print(f"{_C_Y}{msg}{_C_END}")
            elif "[错误]" in msg:
                print(f"{_C_R}{_C_BOLD}{msg}{_C_END}")
            elif "[OpenList提示]" in msg:
                print(f"{_C_Y}{msg}{_C_END}")
            else:
                print(msg)
        except (OSError, IOError):
            self.handleError(record)


def enable_windows_ansi() -> None:
    """在 Windows 上启用 ANSI 颜色支持"""
    if platform.system().lower() == 'windows':
        try:
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
            mode = ctypes.c_uint()
            kernel32.GetConsoleMode(handle, ctypes.byref(mode))
            # 启用 ANSI 转义序列处理
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        except (AttributeError, OSError):
            pass


# ==================== 配置管理 ====================

class MonitorConfig:
    """监控器配置管理类"""
    
    def __init__(self, config_path: Optional[str] = None):
        if config_path is None:
            config_path = os.path.join(os.path.dirname(__file__), 'config.ini')
        
        self.config_path = config_path
        
        if not os.path.exists(config_path):
            raise FileNotFoundError(f"找不到配置文件: {config_path}")
        
        # 创建解析器
        self._config = configparser.ConfigParser()
        self._config.optionxform = str
        
        # 读取配置（使用自定义解析器处理特殊 section）
        self._read_config_safe(config_path)
        
        # 解析特殊 section（每行一个路径的格式）
        self._monitor_folders = self._parse_line_based_section('MonitorFolders')
        self._refresh_paths = self._parse_line_based_section('RefreshPaths')
    
    def _read_config_safe(self, config_path: str) -> None:
        """
        安全读取配置，跳过基于行的特殊 section
        
        这些 section 使用 _parse_line_based_section 手动解析
        """
        # 特殊 section 列表（这些 section 使用每行一个路径的格式）
        line_based_sections = {'MonitorFolders', 'RefreshPaths'}
        
        with open(config_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        
        # 过滤掉特殊 section，只保留标准 key=value 格式的 section
        filtered_lines = []
        skip_section = False
        
        for line in lines:
            stripped = line.strip()
            
            # 检测 section 开始
            if stripped.startswith('[') and stripped.endswith(']'):
                section_name = stripped[1:-1]
                # 如果是特殊 section，标记跳过
                if section_name in line_based_sections:
                    skip_section = True
                    continue
                else:
                    skip_section = False
            
            # 只保留非特殊 section 的行
            if not skip_section:
                filtered_lines.append(line)
        
        # 读取过滤后的配置
        self._config.read_string(''.join(filtered_lines), source=config_path)
    
    def _parse_line_based_section(self, section_name: str) -> list[str]:
        """
        解析基于行的 section（每行一个值，没有键值对）
        """
        paths = []
        
        with open(self.config_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        
        in_target_section = False
        
        for line in lines:
            stripped = line.strip()
            
            # 检测目标 section 开始
            if stripped == f"[{section_name}]":
                in_target_section = True
                continue
            
            # 检测其他 section 开始（结束当前 section）
            if stripped.startswith('[') and stripped.endswith(']'):
                in_target_section = False
                continue
            
            # 收集当前 section 的非空、非注释行
            if in_target_section and stripped and not stripped.startswith(';'):
                paths.append(stripped)
        
        return paths

    
    def _get(self, section: str, key: str, fallback=None):
        """获取标准配置项"""
        if self._config.has_option(section, key):
            return self._config.get(section, key)
        return fallback
    
    def _getint(self, section: str, key: str, fallback: int = 0) -> int:
        """获取整数配置项"""
        if self._config.has_option(section, key):
            return self._config.getint(section, key)
        return fallback
    
    def _getboolean(self, section: str, key: str, fallback: bool = False) -> bool:
        """获取布尔配置项"""
        if self._config.has_option(section, key):
            return self._config.getboolean(section, key)
        return fallback
    
    # ========== 属性访问 ==========
    
    @property
    def monitor_folders(self) -> list[str]:
        """获取监控文件夹列表"""
        return self._monitor_folders.copy()
    
    @property
    def refresh_paths(self) -> list[str]:
        """获取刷新路径列表"""
        return self._refresh_paths.copy()
    
    @property
    def db_file(self) -> str:
        return self._get('Local', 'db_file', './python_embed/strm_mapping.db')
    
    @property
    def webdav_host(self) -> str:
        return self._get('WebDAV', 'host', '')
    
    @property
    def webdav_user(self) -> str:
        return self._get('WebDAV', 'user', '')
    
    @property
    def webdav_password(self) -> str:
        return self._get('WebDAV', 'password', '')

    @property
    def webdav_action(self) -> str:
        return self._get('Setting', 'action', 'MOVE').upper()
        
    @property
    def trash_dir_name(self) -> str:
        return self._get('Setting', 'trash_dir_name', 'strm_回收站')
    
    @property
    def webdav_refresh_interval(self) -> int:
        return self._getint('Setting', 'webdav_refresh_interval', 60)
    
    @property
    def webdav_refresh_depth(self) -> int:
        return self._getint('Setting', 'webdav_refresh_depth', 4)
    
    @property
    def log_level(self) -> str:
        return self._get('Log', 'level', 'INFO')
    
    @property
    def log_file(self) -> str:
        return self._get('Log', 'file', './activity.log')
    
    @property
    def log_max_size_mb(self) -> int:
        return self._getint('Log', 'max_size_mb', 2)

# ==================== 数据库上下文管理器 ====================

@contextmanager
def get_db_connection(db_path: str) -> Generator[sqlite3.Connection, None, None]:
    """数据库连接上下文管理器"""
    conn = sqlite3.connect(db_path)
    try:
        yield conn
    finally:
        conn.close()

# ==================== WebDAV 客户端 ====================

class OpenlistWebDAV:
    """线程安全的 WebDAV 客户端"""
    
    DEFAULT_TIMEOUT = 30
    MOVE_TIMEOUT = 20
    DELETE_TIMEOUT = 15
    
    def __init__(self, host: str, user: str, pwd: str):
        self.host = host.rstrip('/')
        self.auth = (user, pwd)
        self._local = threading.local()
        
        self._adapter = HTTPAdapter(
            pool_connections=50,
            pool_maxsize=50,
            max_retries=3
        )
    
    @property
    def session(self) -> requests.Session:
        """获取当前线程的 Session（线程隔离）"""
        if not hasattr(self._local, 'session'):
            sess = requests.Session()
            sess.auth = self.auth
            sess.mount('http://', self._adapter)
            sess.mount('https://', self._adapter)
            self._local.session = sess
        return self._local.session
    
    def _url(self, path: str) -> str:
        """构建完整的 WebDAV URL"""
        return self.host + quote('/' + path.lstrip('/'), safe='/')
    
    def check_exists(self, path: str) -> bool:
        """检查路径是否存在"""
        try:
            res = self.session.request(
                "PROPFIND",
                self._url(path),
                headers={"Depth": "0"},
                timeout=10
            )
            return res.status_code in (200, 207)
        except requests.exceptions.RequestException:
            return False
    
    def list_contents(self, path: str) -> Union[Dict[str, List[str]], str]:
        """列出目录内容"""
        results = {"folders": [], "files": []}
        try:
            res = self.session.request(
                "PROPFIND",
                self._url(path),
                headers={"Depth": "1"},
                timeout=30
            )
            if res.status_code in (200, 207):
                root = ET.fromstring(res.text)
                ns = {'d': 'DAV:'}
                for resp in root.findall('d:response', ns):
                    href_elem = resp.find('d:href', ns)
                    if href_elem is None or href_elem.text is None:
                        continue
                    href = unquote(href_elem.text)
                    rel_path = href[len(urlparse(self.host).path):].rstrip('/') if href.lower().startswith(urlparse(self.host).path.lower()) else urlparse(href).path.rstrip('/')
                    if not rel_path or rel_path.lower() == path.rstrip('/').lower():
                        continue
                    is_dir = False
                    pstat = resp.find('d:propstat', ns)
                    if pstat is not None:
                        p = pstat.find('d:prop', ns)
                        rt = p.find('d:resourcetype', ns) if p is not None else None
                        if rt is not None and rt.find('d:collection', ns) is not None:
                            is_dir = True
                    if is_dir:
                        results["folders"].append(rel_path)
                    else:
                        results["files"].append(rel_path)
                return results
            elif res.status_code == 404:
                return "404_NOT_FOUND"
        except requests.exceptions.RequestException:
            pass
        return "ERROR"
    
    def move(self, src_path: str, dst_path: str) -> None:
        """移动/重命名 WebDAV 文件"""
        headers = {"Destination": self._url(dst_path), "Overwrite": "T"}
        for attempt in range(3):
            try:
                res = self.session.request(
                    "MOVE",
                    self._url(src_path),
                    headers=headers,
                    timeout=self.MOVE_TIMEOUT
                )
                if res.status_code in (201, 204, 207):
                    return
                err = f"HTTP {res.status_code}"
                if res.status_code == 423:
                    err = "HTTP 423 (资源锁定)"
                    time.sleep(2)
                if attempt < 2:
                    time.sleep(1.5)
                else:
                    raise requests.exceptions.HTTPError(f"{err}: {res.text}")
            except requests.exceptions.RequestException as e:
                if attempt == 2:
                    raise e
                time.sleep(2)
    
    def delete(self, path: str) -> None:
        """删除 WebDAV 文件"""
        for attempt in range(3):
            try:
                res = self.session.request(
                    "DELETE",
                    self._url(path),
                    timeout=self.DELETE_TIMEOUT
                )
                if res.status_code in (200, 202, 204):
                    return
                err = "HTTP 423 (资源锁定)" if res.status_code == 423 else f"HTTP {res.status_code}"
                if res.status_code == 423:
                    time.sleep(2)
                if attempt < 2:
                    time.sleep(1.5)
                else:
                    raise requests.exceptions.HTTPError(f"{err}: {res.text}")
            except requests.exceptions.RequestException as e:
                if attempt == 2:
                    raise e
                time.sleep(2)
    
    def makedirs(self, path: str) -> None:
        """递归创建 WebDAV 目录"""
        parts = [p for p in path.strip('/').split('/') if p]
        curr = ""
        for p in parts:
            curr += "/" + p
            if not self.check_exists(curr):
                res = self.session.request("MKCOL", self._url(curr))
                if res.status_code in (201, 207):
                    time.sleep(0.3)


# ==================== 应用主类 ====================

class MonitorApp:
    """文件监控应用主类"""
    
    def __init__(self, config_path: Optional[str] = None):
        # 加载配置
        self.config = MonitorConfig(config_path)
        
        # 初始化日志
        self._init_logging()

        # 初始化 WebDAV 客户端
        self.client = OpenlistWebDAV(
            self.config.webdav_host,
            self.config.webdav_user,
            self.config.webdav_password
        )
        
        # 锁
        self.db_lock = threading.Lock()
        self.dav_write_lock = threading.Lock()
        self.cleanup_lock = threading.Lock()
        
        # 待清理任务
        self.pending_cleanups: dict[str, threading.Timer] = {}
        
        # 常量
        self.INDEX_RETRY_COUNT = 5
        self.INDEX_RETRY_INTERVAL = 0.5
        self.GHOST_PROTECT_SEC = 60
        
        # 运行时状态
        self._running = False
        self._observer: Optional[Observer] = None
        self._refresh_timer: Optional[threading.Timer] = None
        self._heartbeat_timer: Optional[threading.Timer] = None
        
        log_i(f"[启动] 版本: {VERSION}")
        log_i(f"[配置] 监控目录: {self.config.monitor_folders}")
    
    def _init_logging(self) -> None:
        """初始化日志系统"""
        # ... 现有代码 ...
        self._file_only_logger = logging.getLogger("FileOnly")
        self._file_only_logger.propagate = False
        self._file_only_logger.addHandler(file_handler)
    
    def log_i(self, msg: str) -> None:
        """记录 INFO 级别日志"""
        logging.info(msg)
    
    def log_f(self, msg: str) -> None:
        """记录到文件（不输出到控制台）"""
        self._file_only_logger.info(msg)
    
    def log_d(self, msg: str) -> None:
        """记录 DEBUG 级别日志"""
        logging.debug(msg)
    
    def log_e(self, msg: str) -> None:
        """记录 ERROR 级别日志"""
        logging.error(msg)

    
    def _init_logging(self) -> None:
        """初始化日志系统"""
        log_file = self.config.log_file
        max_mb = self.config.log_max_size_mb
        max_bytes = max_mb * 1024 * 1024
        
        # 确保日志目录存在
        if log_dir := os.path.dirname(log_file):
            if not os.path.exists(log_dir):
                os.makedirs(log_dir)
        
        # 日志轮转
        if os.path.exists(log_file):
            if os.path.getsize(log_file) > max_bytes:
                with open(log_file, 'w', encoding='utf-8') as f:
                    f.write("--- 日志重置 ---\n")
            else:
                with open(log_file, 'a', encoding='utf-8') as f:
                    f.write("\n\n    /\\\\/\\\\ 程序重新启动 /\\\\/\\\\ \n\n")
        
        formatter = logging.Formatter(
            '%(asctime)s [%(levelname)s] %(message)s',
            '%Y-%m-%d %H:%M:%S'
        )
        
        # ========== 清除已有 handler 防止重复 ==========
        root_logger = logging.getLogger()
        root_logger.handlers.clear()
        root_logger.setLevel(self.config.log_level)

        # 文件 handler
        file_handler = logging.FileHandler(log_file, encoding='utf-8')
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)

        # 控制台 handler
        console_handler = ColorConsoleHandler()
        console_handler.setFormatter(formatter)
        root_logger.addHandler(console_handler)

        # ========== 修复：FileOnly logger 设置 propagate=False ==========
        file_only_logger = logging.getLogger("FileOnly")
        file_only_logger.handlers.clear()
        file_only_logger.setLevel(self.config.log_level)
        file_only_logger.propagate = False  # 阻止传播到 root
        file_only_logger.addHandler(file_handler)

        logging.getLogger("urllib3").setLevel(logging.WARNING)
        logging.getLogger("requests").setLevel(logging.WARNING)

        self.file_only_logger = file_only_logger

    
    # ========== 生命周期管理 ==========
    
    def start(self) -> None:
        """启动监控服务"""
        if self._running:
            return
        
        self._running = True
        log_i("[启动] 正在初始化...")
        
        # 初始化数据库
        self.init_db()
        
        # 启动控制台心跳
        self._start_heartbeat()
        
        # 打印启动信息
        print("\n" + "="*60)
        print(f"{_C_B}{_C_BOLD}   Openlist Strm Monitor {VERSION}{_C_END}")
        print("="*60)
        mode_text = f"{_C_G}[ 安全回收: MOVE ]" if self.config.webdav_action == "MOVE" else f"{_C_R}[ 永久删除: DELETE ]"
        print(f"当前模式: {mode_text}{_C_END}\n"+"="*60 + "\n")
        
        # 全量同步
        self.scan_existing_files()
        self.sync_folders_from_strms()
        
        # 启动刷新线程
        threading.Thread(target=self.webdav_refresh_worker, daemon=True).start()
        
        # 启动文件监控
        self._start_observer()
        
        log_i("[启动] 服务已启动")
    
    def stop(self) -> None:
        """停止监控服务"""
        self._running = False
        
        # 停止心跳
        self._cancel_heartbeat()
        
        # 停止文件监控
        if self._observer:
            self._observer.stop()
            self._observer.join()
            self._observer = None
        
        # 取消定时器
        if self._refresh_timer:
            self._refresh_timer.cancel()
            self._refresh_timer = None
        
        log_i("[停止] 服务已停止")
    
    def run(self) -> None:
        """运行应用（阻塞模式）"""
        try:
            # 启动监控
            try:
                self.start()
            except Exception as e:
                log_e(f"[启动] 监控启动失败: {type(e).__name__}: {e}")
                return
            
            # 交互模式
            active = sum(1 for f in self.config.monitor_folders if os.path.exists(f))
            print(f"\n[系统] 成功启动 {active} 个监控任务。")
            
            if sys.stdin and sys.stdin.isatty():
                print(f"\n{_C_Y}[提示]{_C_END} 输入 'q' 并回车退出监控\n")
                while self._running:
                    try:
                        user_input = input().strip().lower()
                    except (EOFError, KeyboardInterrupt):
                        break
                    except OSError as e:
                        log_d(f"[输入] IO 错误: {e}")
                        time.sleep(0.5)
                        continue
                    except UnicodeDecodeError as e:
                        log_d(f"[输入] 编码错误: {e}")
                        continue
                    
                    if user_input == 'q':
                        break
            else:
                while self._running:
                    time.sleep(1)
                    
        except Exception as e:
            # 捕获未预料的异常
            log_e(f"[运行] 未预料的错误: {type(e).__name__}: {e}")
        finally:
            print("\n[系统] 正在安全停止监控线程...")
            try:
                self.stop()
            except Exception as e:
                log_e(f"[停止] 停止监控时出错: {type(e).__name__}: {e}")
            
            try:
                log_f(f"程序已安全停止 - {VERSION}")
                log_f("==================================================\n\n\n")
            except Exception:
                pass

    
    # ========== 心跳 ==========
    
    def _start_heartbeat(self) -> None:
        """启动控制台心跳"""
        self._console_heartbeat()
    
    def _console_heartbeat(self) -> None:
        """控制台心跳"""
        try:
            now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            print(f"{_C_B}{now} {_C_GRAY}[TIME]{_C_END}")
        except (OSError, IOError):
            # 控制台 I/O 错误，静默忽略即可
            pass
        except Exception:
            # 其他未知异常，记录日志
            logging.exception("心跳定时器异常")
        
        # 重新调度
        self._heartbeat_timer = threading.Timer(10, self._console_heartbeat)
        self._heartbeat_timer.daemon = True
        self._heartbeat_timer.start()
    
    def _cancel_heartbeat(self) -> None:
        """停止心跳"""
        if self._heartbeat_timer:
            self._heartbeat_timer.cancel()
            self._heartbeat_timer = None
    
    # ========== 数据库操作 ==========
    
    def init_db(self) -> None:
        """初始化数据库"""
        db_file = self.config.db_file
        if db_dir := os.path.dirname(db_file):
            if not os.path.exists(db_dir):
                os.makedirs(db_dir)
        
        with self.db_lock, get_db_connection(db_file) as conn:
            cursor = conn.cursor()
            cursor.execute(
                'CREATE TABLE IF NOT EXISTS strm_files '
                '(local_path TEXT PRIMARY KEY COLLATE NOCASE, webdav_path TEXT)'
            )
            cursor.execute(
                'CREATE TABLE IF NOT EXISTS ghost_files '
                '(webdav_path TEXT PRIMARY KEY, expire_time REAL)'
            )
            cursor.execute(
                'CREATE TABLE IF NOT EXISTS known_folders '
                '(folder_path TEXT PRIMARY KEY)'
            )
            conn.commit()
    
    def save_known_folder(self, path: str) -> None:
        """保存已知文件夹路径到数据库"""
        if not path or path == "/" or self.config.trash_dir_name in path:
            return
        
        try:
            with self.db_lock, get_db_connection(self.config.db_file) as conn:
                cursor = conn.cursor()
                parts = [p for p in path.strip('/').split('/') if p]
                curr = ""
                for p in parts:
                    curr += "/" + p
                    if self.config.trash_dir_name not in curr:
                        cursor.execute(
                            'INSERT OR IGNORE INTO known_folders VALUES (?)',
                            (curr,)
                        )
                conn.commit()
        except sqlite3.Error as e:
            log_e(f"[索引存疑] 导入数据库错误: {e}")
    
    def remove_known_folder(self, path: str) -> None:
        """从数据库中移除已知文件夹及其子文件夹"""
        try:
            with self.db_lock, get_db_connection(self.config.db_file) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    'DELETE FROM known_folders WHERE folder_path = ?',
                    (path,)
                )
                cursor.execute(
                    'DELETE FROM known_folders WHERE folder_path LIKE ?',
                    (path + "/%",)
                )
                conn.commit()
        except sqlite3.Error as e:
            log_e(f"[索引存疑] 数据库清理指定项错误: {e}")
    
    # ========== 业务逻辑 ==========
    
    def is_in_scope(self, path: str) -> bool:
        """检查路径是否在刷新范围内"""
        refresh_paths = self.config.refresh_paths
        if not refresh_paths:
            return False
        p_norm = path.rstrip('/')
        for rp in refresh_paths:
            rp_norm = rp.rstrip('/')
            if p_norm == rp_norm or p_norm.startswith(rp_norm + "/"):
                return True
        return False
    
    def parse_strm_content(self, content: str) -> str:
        """解析 STRM 文件内容"""
        content = content.strip()
        if not content.startswith('http'):
            return content
        path = unquote(urlparse(content).path)
        if path.startswith('/d/'):
            return '/' + path[3:]
        return path
    
    def remove_empty_local_dirs(self) -> None:
        """移除本地空目录"""
        for root_folder in self.config.monitor_folders:
            if not os.path.exists(root_folder):
                continue
            for root, dirs, files in os.walk(root_folder, topdown=False):
                if root.lower() == root_folder.lower():
                    continue
                if not os.listdir(root):
                    try:
                        os.rmdir(root)
                        log_f(f"[清理] 移除本地空目录: {root}")
                    except OSError:
                        pass
    
    def sync_folders_from_strms(self) -> None:
        """从 STRM 索引中同步已知目录"""
        log_i("正在从 strm 索引中同步已知目录...")
        try:
            with self.db_lock, get_db_connection(self.config.db_file) as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT webdav_path FROM strm_files')
                rows = cursor.fetchall()
            
            count = 0
            for row in rows:
                if self.is_in_scope(row[0]):
                    parent = "/" + "/".join(row[0].strip('/').split('/')[:-1])
                    self.save_known_folder(parent)
                    count += 1
            log_i(f"同步完毕，已补全清单内 {count} 条路径索引。")
        except Exception as e:
            log_e(f"同步目录结构失败: {e}")
    
    # ========== 事件处理 ==========
    
    def _start_observer(self) -> None:
        """启动文件系统监控"""
        self._observer = Observer()
        event_handler = StrmMonitorHandler(self)
        
        active = 0
        for folder in self.config.monitor_folders:
            if os.path.exists(folder):
                self._observer.schedule(event_handler, folder, recursive=True)
                active += 1
                log_i(f"[监控启动] {folder}")
        
        if active == 0:
            log_e("没有有效的监控路径")
            sys.exit(1)
        
        self._observer.start()
    
    def handle_file_event(self, local_path: str, event_type: str) -> None:
        """处理文件创建/修改事件"""
        if local_path.endswith('.strm'):
            self.add_to_db(local_path)
    
    def handle_deletion(self, local_path: str) -> None:
        """处理文件删除事件"""
        if not local_path.endswith('.strm'):
            return
        
        db_file = self.config.db_file
        
        with self.db_lock, get_db_connection(db_file) as conn:
            cursor = conn.cursor()
            cursor.execute(
                'SELECT webdav_path FROM strm_files WHERE local_path = ?',
                (local_path,)
            )
            result = cursor.fetchone()
            if not result:
                return
            
            webdav_path = result[0]
            cursor.execute(
                'INSERT OR REPLACE INTO ghost_files VALUES (?, ?)',
                (webdav_path, time.time() + self.GHOST_PROTECT_SEC)
            )
            cursor.execute(
                'DELETE FROM strm_files WHERE local_path = ?',
                (local_path,)
            )
            conn.commit()
            log_i(f"[检测到删除] {os.path.basename(local_path)}")
            
            # WebDAV 操作
            tfile = None
            with self.dav_write_lock:
                try:
                    if self.client.check_exists(webdav_path):
                        if self.config.webdav_action == "DELETE":
                            self.client.delete(webdav_path)
                            log_i(f" -> 已从云盘永久删除: {webdav_path}")
                        elif self.config.webdav_action == "MOVE":
                            parts = webdav_path.strip('/').split('/')
                            tdir = f"/{parts[0]}/{self.config.trash_dir_name}/{'/'.join(parts[1:-1])}".rstrip('/')
                            tfile = f"{tdir}/{parts[-1]}"
                            self.client.makedirs(tdir)
                            self.client.move(webdav_path, tfile)
                            log_i(f" -> 已移入回收站: {tfile}")
                        else:
                            log_d(f" -> 云盘文件已不存在")
                    else:
                        log_d(f" -> 云盘文件已不存在")
                except Exception as e:
                    error_msg = str(e)
                    if "HTTP 500" in error_msg or "Internal Server Error" in error_msg:
                        if self.config.webdav_action == "MOVE" and tfile:
                            parent_dir = "/" + "/".join(webdav_path.strip('/').split('/')[:-1])
                            log_i(f" -> [OpenList] 服务端返回 500 错误，可能是缓存未同步")
                            time.sleep(3)                            
                        else:
                            log_e(f" -> 操作失败: {e}")
                    else:
                        log_e(f" -> 联动操作失败: {e}")
            
            self.remove_empty_local_dirs()
            self.check_log_size()
    
    def add_to_db(self, local_path: str, is_watchdog: bool = True) -> None:
        """添加 STRM 文件到数据库"""
        db_file = self.config.db_file
        
        try:
            if is_watchdog:
                for attempt in range(self.INDEX_RETRY_COUNT):
                    if os.path.exists(local_path):
                        break
                    log_d(f"[索引等待] 文件暂不可读，第{attempt+1}/{self.INDEX_RETRY_COUNT}次检查")
                    time.sleep(self.INDEX_RETRY_INTERVAL)
                else:
                    log_d(f"[索引失效] 文件已移除，终止索引入库: {local_path}")
                    return
            
            if not os.path.exists(local_path):
                log_d(f"[索引失效] 文件已移除，终止索引入库: {local_path}")
                return
            
            with open(local_path, 'r', encoding='utf-8') as f:
                webdav_path = self.parse_strm_content(f.read())
            
            if not webdav_path:
                log_d(f"[索引存疑] 无效的 webdav 路径: {local_path}")
                return
            
            with self.db_lock, get_db_connection(db_file) as conn:
                cursor = conn.cursor()
                now = time.time()
                
                # 清理过期 ghost
                cursor.execute('DELETE FROM ghost_files WHERE expire_time <= ?', (now,))
                
                # 检查 ghost 保护
                cursor.execute(
                    'SELECT expire_time FROM ghost_files WHERE webdav_path = ?',
                    (webdav_path,)
                )
                ghost = cursor.fetchone()
                if ghost and ghost[0] > now:
                    log_i(f"[二次清理] 自动拦截: {os.path.basename(local_path)}")
                    conn.commit()
                    try:
                        os.remove(local_path)
                    except OSError:
                        pass
                    return
                
                # 检查是否已存在
                cursor.execute(
                    'SELECT webdav_path FROM strm_files WHERE local_path = ?',
                    (local_path,)
                )
                row = cursor.fetchone()
                if row and row[0] == webdav_path:
                    return
                
                # 插入或更新
                cursor.execute(
                    'INSERT OR REPLACE INTO strm_files VALUES (?, ?)',
                    (local_path, webdav_path)
                )
                conn.commit()
            
            log_i(f"[索引更新] {os.path.basename(local_path)} -> {webdav_path}")
            parent_dir = "/" + "/".join(webdav_path.strip('/').split('/')[:-1])
            self.save_known_folder(parent_dir)
            self.trigger_delayed_cleanup(parent_dir)
            
        except OSError as e:
            log_e(f"[索引失败] 文件操作错误: {e}")
        except sqlite3.Error as e:
            log_e(f"[索引失败] 数据库错误: {e}")
        except Exception as e:
            log_e(f"[索引失败] 未预期错误: {e}")
    
    # ========== 冗余清理 ==========
    
    def handle_cascade_delete(self, webdav_folder_path: str) -> None:
        """级联删除处理"""
        if any(webdav_folder_path.rstrip('/') == rp.rstrip('/') for rp in self.config.refresh_paths):
            log_e(f"[配置警告] 云端根路径访问失败: {webdav_folder_path}，已拦截。")
            return
        
        targets = []
        
        # ========== 数据库操作 ==========
        try:
            with self.db_lock, get_db_connection(self.config.db_file) as conn:
                cursor = conn.cursor()
                targets = self._fetch_and_delete_strm(cursor, webdav_folder_path, conn)
        except sqlite3.Error as e:
            log_e(f"[级联删除] 数据库错误: {e}")
            return  # 数据库失败，不再继续文件操作
        
        # ========== 文件系统操作 ==========
        try:
            for local_path, _ in targets:
                self._safe_remove_file(local_path)
            
            self.remove_empty_local_dirs()
        except OSError as e:
            log_e(f"[级联删除] 文件操作错误: {e}")
        except Exception as e:
            log_e(f"[级联删除] 未预料的错误: {type(e).__name__}: {e}")

    def _fetch_and_delete_strm(self, cursor, webdav_folder_path: str, conn) -> list:
        """从数据库获取并删除相关记录"""
        cursor.execute(
            'SELECT local_path, webdav_path FROM strm_files WHERE webdav_path LIKE ?',
            (webdav_folder_path + "/%",)
        )
        targets = cursor.fetchall()
        
        for _, wp in targets:
            cursor.execute(
                'INSERT OR REPLACE INTO ghost_files VALUES (?, ?)',
                (wp, time.time() + self.GHOST_PROTECT_SEC)
            )
        
        cursor.execute('DELETE FROM strm_files WHERE webdav_path LIKE ?', (webdav_folder_path + "/%",))
        cursor.execute('DELETE FROM known_folders WHERE folder_path = ?', (webdav_folder_path,))
        cursor.execute('DELETE FROM known_folders WHERE folder_path LIKE ?', (webdav_folder_path + "/%",))
        conn.commit()
        
        return targets

    def _safe_remove_file(self, file_path: str) -> None:
        """安全删除文件"""
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
                log_i(f"[冗余清理] 移除失效索引: {os.path.basename(file_path)}")
            except OSError:
                pass

    
    def cleanup_zombie_strms(self, parent_webdav_folder: str, remote_files: list) -> None:
        """清理失效的 STRM 索引"""
        try:
            zombie_files = self._find_zombie_files(parent_webdav_folder, remote_files)
            
            for local_path, webdav_path in zombie_files:
                self._remove_strm_entry(local_path, webdav_path)
            
            self.remove_empty_local_dirs()
            
        except sqlite3.Error as e:
            log_e(f"[级联清理] 数据库错误: {e}")
        except OSError as e:
            log_e(f"[级联清理] 文件操作错误: {e}")
        except Exception as e:
            log_e(f"[级联清理] 未预料的错误: {type(e).__name__}: {e}")

    def _find_zombie_files(self, parent_webdav_folder: str, remote_files: list) -> list:
        """查找失效的 STRM 文件"""
        with self.db_lock, get_db_connection(self.config.db_file) as conn:
            cursor = conn.cursor()
            cursor.execute(
                'SELECT local_path, webdav_path FROM strm_files WHERE webdav_path LIKE ?',
                (parent_webdav_folder + "/%",)
            )
            db_entries = cursor.fetchall()
        
        zombie_files = []
        for local_path, webdav_path in db_entries:
            # 跳过子目录
            if "/" in webdav_path[len(parent_webdav_folder)+1:]:
                continue
            
            if webdav_path not in remote_files:
                zombie_files.append((local_path, webdav_path))
        
        return zombie_files

    def _remove_strm_entry(self, local_path: str, webdav_path: str) -> None:
        """删除单个 STRM 条目（数据库 + 文件）"""
        log_i(f"[冗余清理] 移除本地失效索引: {os.path.basename(local_path)}")
        
        # 删除数据库记录
        with self.db_lock, get_db_connection(self.config.db_file) as conn:
            cursor = conn.cursor()
            cursor.execute(
                'DELETE FROM strm_files WHERE local_path = ?',
                (local_path,)
            )
            conn.commit()
        
        # 删除本地文件
        if os.path.exists(local_path):
            try:
                os.remove(local_path)
            except OSError:
                pass
  
    # ========== 刷新引擎 ==========
    
    def trigger_delayed_cleanup(self, webdav_folder: str) -> None:
        """触发延迟清理"""
        with self.cleanup_lock:
            if webdav_folder in self.pending_cleanups:
                self.pending_cleanups[webdav_folder].cancel()
            t = threading.Timer(10.0, self.execute_targeted_cleanup, args=[webdav_folder])
            self.pending_cleanups[webdav_folder] = t
            t.start()
    
    def execute_targeted_cleanup(self, folder_path: str) -> None:
        """执行定向清理"""
        with self.dav_write_lock:
            log_f(f"[冗余校验] 检查变动: {folder_path}")
            results = self.client.list_contents(folder_path)
            if results == "404_NOT_FOUND":
                self.handle_cascade_delete(folder_path)
            elif isinstance(results, dict):
                if results["files"]:
                    self.cleanup_zombie_strms(folder_path, results["files"])
                self.remove_empty_local_dirs()
        
        with self.cleanup_lock:
            if folder_path in self.pending_cleanups:
                del self.pending_cleanups[folder_path]
    
    def refresh_folder_task(self, webdav_path: str, depth: int = 0, known_set: set = None) -> None:
        """刷新文件夹任务"""
        if not self.is_in_scope(webdav_path):
            return
        
        results = self.client.list_contents(webdav_path)
        if results == "404_NOT_FOUND":
            self.handle_cascade_delete(webdav_path)
            return
        if not isinstance(results, dict):
            return
        
        if results["files"]:
            self.cleanup_zombie_strms(webdav_path, results["files"])
        
        for sd in results["folders"]:
            if self.config.trash_dir_name in sd:
                continue
            if known_set is not None and sd not in known_set:
                log_i(f"[主动刷新] 嗅探到新目录: {sd}")
                self.save_known_folder(sd)
                known_set.add(sd)
                self.refresh_folder_task(sd, depth - 1, known_set)
    
    def execute_refresh_cycle(self) -> None:
        """执行刷新周期"""
        log_i(f"[主动刷新] 正在执行并发体检与嗅探...")
    
        with self.db_lock, get_db_connection(self.config.db_file) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT folder_path FROM known_folders')
            known_set = {row[0] for row in cursor.fetchall()}
    
        for rp in self.config.refresh_paths:
            self.save_known_folder(rp)
            known_set.add(rp)
        try:
            results = self.client.list_contents(rp)
            if isinstance(results, dict) and results["folders"]:
                for sd in results["folders"]:
                    if self.config.trash_dir_name in sd:
                        continue
                    if sd not in known_set:
                        log_i(f"[主动刷新] 发现新目录: {sd}")
                        self.save_known_folder(sd)
                        known_set.add(sd)
        except Exception as e:
            log_e(f"[主动刷新] 扫描根目录失败 {rp}: {e}")

        changed = True
        while changed:
            changed = False
            current_targets = [p for p in known_set if self.is_in_scope(p)]
            for target in current_targets:
                try:
                    results = self.client.list_contents(target)
                    if isinstance(results, dict) and results["folders"]:
                        for sd in results["folders"]:
                            if self.config.trash_dir_name in sd:
                                continue
                            if sd not in known_set:
                                log_i(f"[主动刷新] 嗅探到新目录: {sd}")
                                self.save_known_folder(sd)
                                known_set.add(sd)
                                changed = True
                except Exception:
                    continue

        targets = [p for p in known_set if self.is_in_scope(p)]
    
        # 使用 executor.map 替代 executor.submit + as_completed
        with ThreadPoolExecutor(max_workers=10) as executor:
            executor.map(
                lambda p: self.refresh_folder_task(p, self.config.refresh_depth, known_set),
                targets
            )
    
        log_i(f"[主动刷新] 指令发送完毕，等待 15s 待工具同步...")
        time.sleep(15)
        self.scan_existing_files()
        log_i(f"[主动刷新] 本轮自愈同步完毕。")
    
    def webdav_refresh_worker(self) -> None:
        """WebDAV 刷新工作线程"""
        if self.config.webdav_refresh_interval <= 0 or not self.config.refresh_paths:
            return
        
        self.execute_refresh_cycle()
        
        while self._running:
            time.sleep(self.config.webdav_refresh_interval * 60)
            self.execute_refresh_cycle()
    
    # ========== 全量同步 ==========
    
    def scan_existing_files(self, target_folder: str = None) -> None:
        """扫描现有文件"""
        start_t = time.time()
        new_updates = []
        count_skip = 0
        
        try:
            db_records = self._load_db_records()
            folders = [target_folder] if target_folder else self.config.monitor_folders
            
            for folder in folders:
                new_updates, count_skip = self._scan_folder(
                    folder, db_records, new_updates, count_skip
                )
            
            self._batch_update_db(new_updates)
            
            if not target_folder:
                log_i(f"全量同步完成！耗时: {time.time()-start_t:.2f}s "
                      f"(跳过: {count_skip}, 新增: {len(new_updates)})")
                      
        except sqlite3.Error as e:
            log_e(f"[全量同步] 数据库错误: {e}")
        except OSError as e:
            log_e(f"[全量同步] 文件系统错误: {e}")
        except Exception as e:
            log_e(f"[全量同步] 未预料的错误: {type(e).__name__}: {e}")
        finally:
            self.remove_empty_local_dirs()

    def _load_db_records(self) -> dict:
        """加载数据库已有记录"""
        with self.db_lock, get_db_connection(self.config.db_file) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT local_path, webdav_path FROM strm_files')
            return dict(cursor.fetchall())

    def _scan_folder(self, folder: str, db_records: dict, 
                     new_updates: list, count_skip: int) -> tuple:
        """扫描单个文件夹"""
        if not os.path.exists(folder):
            return new_updates, count_skip
        
        for root, _, files in os.walk(folder):
            for file in files:
                if not file.endswith('.strm'):
                    continue
                
                lp = os.path.abspath(os.path.normpath(os.path.join(root, file)))
                
                if lp in db_records:
                    count_skip += 1
                    continue
                
                wp = self._parse_strm_file(lp)
                if wp:
                    new_updates.append((lp, wp))
        
        return new_updates, count_skip

    def _parse_strm_file(self, local_path: str) -> Optional[str]:
        """解析单个 STRM 文件"""
        try:
            with open(local_path, 'r', encoding='utf-8') as f:
                content = f.read()
            return self.parse_strm_content(content)
        except OSError as e:
            log_d(f"[扫描] 文件读取失败: {local_path} - {e}")
        except UnicodeDecodeError as e:
            log_d(f"[扫描] 文件编码错误: {local_path} - {e}")
        except Exception as e:
            log_e(f"[扫描] 未预料的错误: {local_path} - {type(e).__name__}: {e}")
        return None

    def _batch_update_db(self, new_updates: list) -> None:
        """批量更新数据库"""
        if not new_updates:
            return
            
        with self.db_lock, get_db_connection(self.config.db_file) as conn:
            cursor = conn.cursor()
            cursor.executemany(
                'INSERT OR REPLACE INTO strm_files VALUES (?, ?)',
                new_updates
            )
            conn.commit()

    def check_log_size(self) -> None:
        """检查日志大小"""
        log_file = self.config.log_file
        max_mb = self.config.log_max_size_mb
        if os.path.exists(log_file) and os.path.getsize(log_file) > max_mb * 1024 * 1024:
            with open(log_file, 'w', encoding='utf-8') as f:
                f.write("--- 运行期间日志已重置 ---\n")


# ==================== 文件事件处理器 ====================

class StrmMonitorHandler(FileSystemEventHandler):
    """STRM 文件监控处理器"""
    
    def __init__(self, app: MonitorApp):
        self.app = app
    
    def on_created(self, event):
        if not event.is_directory and event.src_path.endswith('.strm'):
            self.app.add_to_db(os.path.abspath(os.path.normpath(event.src_path)))
    
    def on_modified(self, event):
        if not event.is_directory and event.src_path.endswith('.strm'):
            self.app.add_to_db(os.path.abspath(os.path.normpath(event.src_path)))
    
    def on_deleted(self, event):
        if not event.is_directory and event.src_path.endswith('.strm'):
            t = threading.Thread(
                target=self.app.handle_deletion,
                args=(os.path.abspath(os.path.normpath(event.src_path)),),
                daemon=True  # 设置为守护线程
            )
            t.start()



# ==================== 日志辅助函数 ====================

def log_i(msg: str) -> None:
    """记录信息日志"""
    logging.info(msg)


def log_f(msg: str) -> None:
    """记录文件日志"""
    logging.getLogger("FileOnly").info(msg)


def log_d(msg: str) -> None:
    """记录调试日志"""
    logging.debug(msg)


def log_e(msg: str, exc: bool = False) -> None:
    """记录错误日志"""
    logging.error(msg, exc_info=exc)


# ==================== 入口点 ====================

def main():
    """应用入口点"""
    enable_windows_ansi()
    
    app = MonitorApp()
    app.run()


if __name__ == "__main__":
    main()
